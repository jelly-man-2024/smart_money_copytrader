"""Address-scoped ABI adapters; unknown methods are never guessed into swaps."""
from __future__ import annotations

from eth_abi import decode, encode
from eth_utils import keccak

from .models import Signal, Transaction, address
from . import registry as R

PACKED_OPS = "(address,uint256,bytes,bytes,bytes32,uint256,bytes32,bytes,bytes)[]"
CALLS = "(address,uint256,bytes)[]"
RELAY_CALLS = "(address,bool,uint256,bytes)[]"
POOL_KEY = "(address,address,uint24,int24,address)"
V4_PATH_KEY = "(address,uint24,int24,address,bytes)"
KYBER_SWAP_DESCRIPTION = (
    "(address,address,address[],uint256[],address[],uint256[],address,uint256,uint256,uint256,bytes)"
)
KYBER_SWAP_EXECUTION = f"(address,address,bytes,{KYBER_SWAP_DESCRIPTION},bytes)"


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def side(token_in: str, token_out: str,
         quote_assets: frozenset[str] = R.QUOTE_ASSETS) -> str:
    if token_in in quote_assets and token_out not in quote_assets:
        return "BUY"
    if token_out in quote_assets and token_in not in quote_assets:
        return "SELL"
    return "TOKEN_SWAP"


def v3_hops(data: bytes, exact_in: bool) -> list[dict]:
    if len(data) < 43 or (len(data) - 20) % 23:
        raise ValueError("invalid V3 path")
    tokens = [address("0x" + data[:20].hex())]
    fees = []
    offset = 20
    while offset < len(data):
        fees.append(int.from_bytes(data[offset:offset + 3], "big"))
        tokens.append(address("0x" + data[offset + 3:offset + 23].hex()))
        offset += 23
    hops = [{"token_in": a, "token_out": b, "fee": fee}
            for a, b, fee in zip(tokens, tokens[1:], fees)]
    if exact_in:
        return hops
    return [{"token_in": hop["token_out"], "token_out": hop["token_in"], "fee": hop["fee"]}
            for hop in reversed(hops)]


def v3_path(data: bytes, exact_in: bool) -> tuple[str, str]:
    hops = v3_hops(data, exact_in)
    return hops[0]["token_in"], hops[-1]["token_out"]


class Decoder:
    def __init__(self, watchlist: dict, delegations: dict[str, str] | None = None,
                 chain_id: int = R.CHAIN_ID):
        self.watchlist = watchlist
        self.delegations = delegations or {}
        self.chain = R.chain_for(chain_id)

    def decode(self, tx: Transaction) -> list[Signal]:
        C = self.chain
        if tx.chain_id != C.chain_id:
            return []
        result: list[Signal] = []
        budget = [0]

        def emit(wallet, mode, path, to, data, behavior, op=None, **fields):
            signal = Signal(
                tx_hash=tx.hash, wallet=wallet, mode=mode, behavior=behavior,
                path=path, contract=to, selector="0x" + data[:4].hex(), fresh=tx.fresh,
                chain_id=tx.chain_id,
                userop_index=op[0] if op else None, userop_nonce=str(op[1]) if op else None,
                **fields,
            )
            result.append(signal)
            return signal

        def swap(wallet, mode, path, to, data, op, token_in, token_out,
                 amount, limit, recipient, protocol, exact_in=True, **extra):
            token_in, token_out = address(token_in), address(token_out)
            item = emit(
                wallet, mode, path, to, data, side(token_in, token_out, C.quote_assets), op,
                token_in=token_in, token_out=token_out,
                amount_in_raw=str(amount) if exact_in else None,
                amount_out_raw=None if exact_in else str(amount), amount_limit_raw=str(limit),
                recipient=address(recipient) if recipient else None, protocol=protocol, exact_in=exact_in, **extra,
            )
            item.reasons.append("decoded_intent_not_execution")
            if amount == 0 or amount >= 2**255:
                item.reasons.append("dynamic_or_zero_amount_requires_execution_state")
            return item

        def walk(wallet, mode, to, value, data, path, op=None, depth=0, caller=None):
            budget[0] += 1
            if budget[0] > 512 or depth > 12:
                raise ValueError("call graph limit")
            to = address(to) if to else None
            sel, args = data[:4], data[4:]

            def child(dest, amount, body, suffix):
                walk(wallet, mode, dest, amount, body, path + "/" + suffix, op, depth + 1, to)

            try:
                implementation = self.delegations.get(wallet)
                if to == wallet and implementation:
                    calls = None
                    if implementation == C.simple_account:
                        if sel == bytes.fromhex("b61d27f6"):
                            calls = [decode(["address", "uint256", "bytes"], args)]
                        elif sel == bytes.fromhex("34fcd5be"):
                            calls = decode([CALLS], args)[0]
                    elif implementation == C.metamask_account and sel == bytes.fromhex("e9ae5c53"):
                        execution_mode, body = decode(["bytes32", "bytes"], args)
                        if execution_mode == bytes(32) and len(body) >= 52:
                            calls = [("0x" + body[:20].hex(), int.from_bytes(body[20:52], "big"), body[52:])]
                        elif execution_mode == b"\x01" + bytes(31):
                            calls = decode([CALLS], body)[0]
                    if calls is not None:
                        if len(calls) > 256:
                            raise ValueError("account batch limit")
                        for i, (dest, amount, body) in enumerate(calls):
                            child(dest, amount, body, str(i))
                        return

                if to == C.relay_proxy and sel == bytes.fromhex("f9e4bab4"):
                    tokens, amounts, calls, refund, nft, metadata = decode(
                        ["address[]", "uint256[]", RELAY_CALLS, "address", "address", "bytes"], args)
                    if len(tokens) != len(amounts) or len(calls) > 256:
                        raise ValueError("invalid Relay batch")
                    for i, (dest, allow_failure, amount, body) in enumerate(calls):
                        # Relay Router executes these calls, not the proxy itself.
                        walk(wallet, mode, dest, amount, body, path + f"/relay/{i}", op, depth + 1, C.relay_router)
                    return
                if to == C.relay_router and sel == bytes.fromhex("cd6e13f7"):
                    calls, refund, nft, metadata = decode([RELAY_CALLS, "address", "address", "bytes"], args)
                    if len(calls) > 256:
                        raise ValueError("Relay batch limit")
                    for i, (dest, allow_failure, amount, body) in enumerate(calls):
                        child(dest, amount, body, f"relay/{i}")
                    return
                if to == C.relay_router and sel == bytes.fromhex("73b7bb2f"):
                    tokens, targets, payloads, minimums = decode(
                        ["address[]", "address[]", "bytes[]", "uint256[]"], args)
                    if (not len(tokens) == len(targets) == len(payloads) == len(minimums)
                            or len(tokens) > 256):
                        raise ValueError("invalid Relay cleanup arrays")
                    for i, (token, target, payload, minimum) in enumerate(zip(
                            tokens, targets, payloads, minimums)):
                        start = len(result)
                        child(target, 0, payload, f"cleanup/{i}")
                        for item in result[start:]:
                            item.evidence.update({
                                "source_orchestrator": "relay",
                                "relay_cleanup_token": address(token),
                                "relay_cleanup_minimum_raw": str(minimum),
                            })
                    return

                if to == C.depository:
                    if sel == bytes.fromhex("e8017952"):
                        depositor, token, amount, order = decode(["address", "address", "uint256", "bytes32"], args)
                        amount_source = "calldata"
                    elif sel == bytes.fromhex("5a1ee3ac"):
                        depositor, token, order = decode(["address", "address", "bytes32"], args)
                        amount, amount_source = None, "full_allowance_receipt_event"
                    elif sel == bytes.fromhex("49290c1c"):
                        depositor, order = decode(["address", "bytes32"], args)
                        token, amount = R.NATIVE, value
                        amount_source = "call_value"
                    else:
                        raise ValueError("unsupported depository method")
                    emit(wallet, mode, path, to, data, "INTENT_DEPOSIT", op, token_in=token,
                         amount_in_raw=str(amount) if amount is not None else None, recipient=depositor,
                         evidence={"order_id": "0x" + order.hex(),
                                   "deposit_amount_source": amount_source},
                         reasons=["deposit_is_not_a_destination_purchase"])
                    return

                if to == C.zero_x_allowance_holder and sel == bytes.fromhex("2213bc0b"):
                    operator, token, amount, target, payload = decode(
                        ["address", "address", "uint256", "address", "bytes"], args)
                    emit(wallet, mode, path, to, data, "AGGREGATOR_SWAP_INTENT", op,
                         token_in=token, amount_in_raw=str(amount), recipient=target,
                         protocol="0x", evidence={
                             "source_aggregator": "0x",
                             "allowance_holder_operator": address(operator),
                             "allowance_holder_target": address(target),
                             "operator_payload_selector": "0x" + payload[:4].hex(),
                         }, reasons=["decoded_aggregator_intent_not_execution"])
                    return

                if (to == C.kyber_router
                        and sel == bytes.fromhex("e21fd0e9")):
                    execution = decode([KYBER_SWAP_EXECUTION], args)[0]
                    call_target, approve_target, target_data, desc, client_data = execution
                    (src_token, dst_token, src_receivers, src_amounts, fee_receivers,
                     fee_amounts, dst_receiver, amount, minimum, flags, permit) = desc
                    if (len(src_receivers) != len(src_amounts)
                            or len(fee_receivers) != len(fee_amounts)
                            or max(len(src_receivers), len(fee_receivers)) > 256
                            or amount <= 0 or minimum <= 0):
                        raise ValueError("invalid Kyber swap description")
                    emit(wallet, mode, path, to, data, "AGGREGATOR_SWAP_INTENT", op,
                         token_in=src_token, token_out=dst_token,
                         amount_in_raw=str(amount), amount_limit_raw=str(minimum),
                         recipient=dst_receiver if int(dst_receiver, 16) else C.relay_router,
                         protocol="kyber", evidence={
                             "source_aggregator": "kyber",
                             "kyber_call_target": address(call_target),
                             "kyber_approve_target": address(approve_target),
                             "kyber_target_data_selector": "0x" + target_data[:4].hex(),
                             "kyber_flags": str(flags),
                         }, reasons=["decoded_aggregator_intent_not_execution"])
                    return

                if to == C.ripe_claim and sel == bytes.fromhex("815a4392"):
                    recipient, flag = decode(["address", "bool"], args)
                    emit(wallet, mode, path, to, data, "CLAIM", op, recipient=recipient)
                    return

                if to == C.weth:
                    if sel == bytes.fromhex("d0e30db0") and not args and value > 0:
                        emit(wallet, mode, path, to, data, "WRAP_NATIVE", op,
                             token_in=R.NATIVE, token_out=C.weth,
                             amount_in_raw=str(value), recipient=wallet,
                             reasons=["wrap_is_asset_conversion_not_purchase"])
                        return
                    if sel == bytes.fromhex("2e1a7d4d"):
                        amount = decode(["uint256"], args)[0]
                        emit(wallet, mode, path, to, data, "UNWRAP_WETH", op,
                             token_in=C.weth, token_out=R.NATIVE,
                             amount_in_raw=str(amount), recipient=wallet,
                             reasons=["unwrap_is_asset_conversion_not_sale"])
                        return

                if to == C.v3_router and sel in {bytes.fromhex("ac9650d8"), bytes.fromhex("5ae401dc")}:
                    calls = decode(["bytes[]"], args)[0] if sel.hex() == "ac9650d8" else decode(["uint256", "bytes[]"], args)[1]
                    if len(calls) > 256:
                        raise ValueError("router batch limit")
                    for i, body in enumerate(calls):
                        child(to, value, body, f"multicall/{i}")
                    return

                if to == C.v2_router:
                    methods = {
                        "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)": (True, False, False),
                        "swapTokensForExactTokens(uint256,uint256,address[],address,uint256)": (False, False, False),
                        "swapExactTokensForETH(uint256,uint256,address[],address,uint256)": (True, False, True),
                        "swapTokensForExactETH(uint256,uint256,address[],address,uint256)": (False, False, True),
                        "swapExactTokensForTokensSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)": (True, False, False),
                        "swapExactTokensForETHSupportingFeeOnTransferTokens(uint256,uint256,address[],address,uint256)": (True, False, True),
                        "swapExactETHForTokens(uint256,address[],address,uint256)": (True, True, False),
                        "swapETHForExactTokens(uint256,address[],address,uint256)": (False, True, False),
                    }
                    for signature, (exact, native_in, native_out) in methods.items():
                        if sel != selector(signature):
                            continue
                        if native_in:
                            bound, route, recipient, deadline = decode(["uint256", "address[]", "address", "uint256"], args)
                            amount, limit = (value, bound) if exact else (bound, value)
                        else:
                            amount, limit, route, recipient, deadline = decode(["uint256", "uint256", "address[]", "address", "uint256"], args)
                        if len(route) < 2:
                            raise ValueError("invalid V2 route")
                        swap(wallet, mode, path, to, data, op, R.NATIVE if native_in else route[0],
                             R.NATIVE if native_out else route[-1], amount, limit, recipient, "v2", exact,
                             evidence={"route": list(route), "deadline": deadline})
                        return
                    if sel in {selector(s) for s in (
                        "addLiquidity(address,address,uint256,uint256,uint256,uint256,address,uint256)",
                        "addLiquidityETH(address,uint256,uint256,uint256,address,uint256)",
                        "removeLiquidity(address,address,uint256,uint256,uint256,address,uint256)",
                        "removeLiquidityETH(address,uint256,uint256,uint256,address,uint256)",
                    )}:
                        emit(wallet, mode, path, to, data, "LIQUIDITY", op)
                        return

                if to == C.v3_router:
                    if sel in {bytes.fromhex("04e45aaf"), bytes.fromhex("5023b4df")}:
                        a, b, fee, recipient, amount, limit, sqrt = decode(["(address,address,uint24,address,uint256,uint256,uint160)"], args)[0]
                        swap(wallet, mode, path, to, data, op, a, b, amount, limit, recipient,
                             "v3", sel.hex() == "04e45aaf", evidence={"fee": fee})
                        return
                    if sel in {bytes.fromhex("b858183f"), bytes.fromhex("09b81346")}:
                        route, recipient, amount, limit = decode(["(bytes,address,uint256,uint256)"], args)[0]
                        exact = sel.hex() == "b858183f"
                        hops = v3_hops(route, exact)
                        swap(wallet, mode, path, to, data, op, hops[0]["token_in"], hops[-1]["token_out"],
                             amount, limit, recipient, "v3", exact, evidence={"hops": hops})
                        return

                if to == C.universal_router and sel in {bytes.fromhex("3593564c"), bytes.fromhex("24856bc3")}:
                    types = ["bytes", "bytes[]", "uint256"] if sel.hex() == "3593564c" else ["bytes", "bytes[]"]
                    values = decode(types, args)
                    commands, inputs = values[:2]
                    if len(commands) != len(inputs) or len(commands) > 256:
                        raise ValueError("invalid Universal Router commands")
                    for i, (command, body) in enumerate(zip(commands, inputs)):
                        cmd = command & 0x7f
                        subpath = path + f"/command/{i}"
                        if cmd in (0, 1, 8, 9):
                            exact = cmd in (0, 8)
                            route_type = "bytes" if cmd in (0, 1) else "address[]"
                            recipient, amount, limit, route, payer_user = decode(
                                ["address", "uint256", "uint256", route_type, "bool"], body)
                            if cmd in (0, 1):
                                hops = v3_hops(route, exact)
                                a, b = hops[0]["token_in"], hops[-1]["token_out"]
                            else:
                                if len(route) < 2:
                                    raise ValueError("invalid V2 route")
                                a, b = route[0], route[-1]
                            if recipient == "0x" + "0" * 39 + "1":
                                recipient = caller or wallet
                            elif recipient == "0x" + "0" * 39 + "2":
                                recipient = to
                            swap(wallet, mode, subpath, to, data, op, a, b, amount, limit, recipient,
                                 "v3" if cmd in (0, 1) else "v2", exact,
                                 evidence={"allow_revert": bool(command & 0x80), "payer_is_user": payer_user,
                                           **({"hops": hops} if cmd in (0, 1) else {"route": list(route)})})
                        elif cmd == 0x10:
                            actions, params = decode(["bytes", "bytes[]"], body)
                            if len(actions) != len(params) or len(actions) > 256:
                                raise ValueError("invalid V4 actions")
                            settlement = []
                            for settlement_action, settlement_param in zip(actions, params):
                                if settlement_action == 0x0b:
                                    currency, amount, payer_user = decode(
                                        ["address", "uint256", "bool"], settlement_param)
                                    settlement.append({"action": "SETTLE", "currency": address(currency),
                                                       "amount_raw": str(amount),
                                                       "payer_is_user": bool(payer_user)})
                                elif settlement_action == 0x0c:
                                    currency, max_amount = decode(["address", "uint256"], settlement_param)
                                    settlement.append({"action": "SETTLE_ALL", "currency": address(currency),
                                                       "max_amount_raw": str(max_amount), "payer_is_user": True})
                                elif settlement_action == 0x0e:
                                    currency, settlement_recipient, amount = decode(
                                        ["address", "address", "uint256"], settlement_param)
                                    if settlement_recipient == "0x" + "0" * 39 + "1":
                                        settlement_recipient = caller or wallet
                                    elif settlement_recipient == "0x" + "0" * 39 + "2":
                                        settlement_recipient = to
                                    settlement.append({"action": "TAKE", "currency": address(currency),
                                                       "recipient": address(settlement_recipient),
                                                       "amount_raw": str(amount)})
                                elif settlement_action == 0x0f:
                                    currency, min_amount = decode(["address", "uint256"], settlement_param)
                                    settlement.append({"action": "TAKE_ALL", "currency": address(currency),
                                                       "recipient": address(caller or wallet),
                                                       "min_amount_raw": str(min_amount)})
                            for j, (action, param) in enumerate(zip(actions, params)):
                                actionpath = subpath + f"/action/{j}"
                                if action in (6, 8):
                                    # Robinhood samples use the newer IV4Router layout
                                    # including minHopPriceX36, not the older five-field tuple.
                                    key, zero_for_one, amount, limit, min_price, hook_data = decode(
                                        [f"({POOL_KEY},bool,uint128,uint128,uint256,bytes)"], param)[0]
                                    a, b = (key[0], key[1]) if zero_for_one else (key[1], key[0])
                                    item = swap(wallet, mode, actionpath, to, data, op, a, b, amount, limit,
                                                None, "v4", action == 6,
                                                pool_id="0x" + keccak(encode([POOL_KEY], [key])).hex(),
                                                evidence={"pool_key": list(key), "hook_data": "0x" + hook_data.hex(),
                                                          "min_hop_price_x36": str(min_price),
                                                          "v4_settlement_actions": settlement,
                                                          "recipient_requires_settlement_check": True})
                                    if key[4] != R.NATIVE:
                                        item.reasons.append("nonzero_hook_requires_historical_code_check")
                                elif action in (7, 9):
                                    if action == 7:
                                        currency, path_keys, min_prices, amount, limit = decode(
                                            ["address", f"{V4_PATH_KEY}[]", "uint256[]", "uint128", "uint128"],
                                            param)
                                        current = address(currency)
                                        hops = []
                                        for path_key in path_keys:
                                            output = address(path_key[0])
                                            currency0, currency1 = sorted((current, output))
                                            hops.append({"token_in": current, "token_out": output,
                                                         "pool_key": [currency0, currency1, path_key[1],
                                                                      path_key[2], address(path_key[3])],
                                                         "hook_data": "0x" + path_key[4].hex()})
                                            current = output
                                        token_in, token_out, exact = address(currency), current, True
                                    else:
                                        currency, path_keys, min_prices, amount, limit = decode(
                                            ["address", f"{V4_PATH_KEY}[]", "uint256[]", "uint128", "uint128"],
                                            param)
                                        current = address(currency)
                                        reverse_hops = []
                                        for path_key in reversed(path_keys):
                                            input_currency = address(path_key[0])
                                            currency0, currency1 = sorted((input_currency, current))
                                            reverse_hops.append({"token_in": input_currency, "token_out": current,
                                                                 "pool_key": [currency0, currency1, path_key[1],
                                                                              path_key[2], address(path_key[3])],
                                                                 "hook_data": "0x" + path_key[4].hex()})
                                            current = input_currency
                                        hops = list(reversed(reverse_hops))
                                        token_in, token_out, exact = current, address(currency), False
                                    if not hops or len(min_prices) not in (0, len(hops)):
                                        raise ValueError("invalid V4 multi-hop path")
                                    pool_ids = ["0x" + keccak(encode([POOL_KEY], [hop["pool_key"]])).hex()
                                                for hop in hops]
                                    item = swap(wallet, mode, actionpath, to, data, op, token_in, token_out,
                                                amount, limit, None, "v4", exact,
                                                evidence={"v4_hops": hops, "v4_pool_ids": pool_ids,
                                                          "min_hop_price_x36": [str(v) for v in min_prices],
                                                          "v4_settlement_actions": settlement,
                                                          "recipient_requires_settlement_check": True})
                                    if any(hop["pool_key"][4] != R.NATIVE for hop in hops):
                                        item.reasons.append("nonzero_hook_requires_historical_code_check")
                                elif action not in (0x0b, 0x0c, 0x0e, 0x0f):
                                    emit(wallet, mode, actionpath, to, data, "UNKNOWN", op,
                                         reasons=[f"unsupported_v4_action:{action}"])
                        elif cmd == 0x21:
                            nested_commands, nested_inputs = decode(["bytes", "bytes[]"], body)
                            child(to, value, bytes.fromhex("24856bc3") + encode(["bytes", "bytes[]"], [nested_commands, nested_inputs]), f"subplan/{i}")
                        elif cmd in (2, 3, 10, 13):
                            emit(wallet, mode, subpath, to, data, "AUTHORIZATION", op)
                        elif cmd in (4, 5, 6, 11, 12, 14):
                            emit(wallet, mode, subpath, to, data, "SETTLEMENT", op)
                        else:
                            emit(wallet, mode, subpath, to, data, "UNKNOWN", op, reasons=[f"unsupported_router_command:{cmd}"])
                    return

                if to == C.permit2 and sel == bytes.fromhex("87517c45"):
                    token, spender, amount, expiration = decode(["address", "address", "uint160", "uint48"], args)
                    emit(wallet, mode, path, to, data, "APPROVAL", op, token_in=token,
                         recipient=spender, amount_limit_raw=str(amount))
                    return
                if to in C.position_managers:
                    emit(wallet, mode, path, to, data, "LIQUIDITY_OR_POSITION_CALL", op,
                         reasons=["position_manager_not_a_plain_token_purchase"])
                    return
                if sel == bytes.fromhex("095ea7b3"):
                    spender, amount = decode(["address", "uint256"], args)
                    emit(wallet, mode, path, to, data, "APPROVAL", op, recipient=spender,
                         token_in=to, amount_limit_raw=str(amount))
                    return
                if sel == bytes.fromhex("a9059cbb"):
                    recipient, amount = decode(["address", "uint256"], args)
                    emit(wallet, mode, path, to, data, "TRANSFER", op, token_in=to,
                         recipient=recipient, amount_in_raw=str(amount))
                    return
                if not data and value:
                    emit(wallet, mode, path, to, data, "TRANSFER", op, token_in=R.NATIVE,
                         recipient=to, amount_in_raw=str(value))
                    return
                emit(wallet, mode, path, to, data, "UNKNOWN", op, reasons=["unsupported_contract_or_method"])
            except Exception as exc:
                emit(wallet, mode, path + "/decode_error", to, data, "UNKNOWN", op,
                     reasons=["decode_error:" + type(exc).__name__])

        if tx.to == C.entrypoint and tx.data[:4] == bytes.fromhex("765e827f"):
            try:
                ops, beneficiary = decode([PACKED_OPS, "address"], tx.data[4:])
                if len(ops) > 256:
                    raise ValueError("UserOperation limit")
                for i, op in enumerate(ops):
                    wallet = address(op[0])
                    if wallet in self.watchlist:
                        walk(wallet, "bundled_account", wallet, 0, op[3], f"userop/{i}", (i, op[1]), caller=C.entrypoint)
            except Exception:
                # Untrusted malformed sender bytes must never become ownership evidence.
                return []
        elif tx.sender in self.watchlist:
            mode = "self_account" if tx.to == tx.sender else "direct"
            walk(tx.sender, mode, tx.to, tx.value, tx.data, "call", caller=tx.sender)
        relay_groups = {}
        for item in result:
            if "/relay/" not in item.path:
                continue
            root = item.path.split("/relay/", 1)[0]
            relay_groups.setdefault((item.wallet, item.userop_index, root), []).append(item)
        for items in relay_groups.values():
            aggregators = [item for item in items if item.behavior == "AGGREGATOR_SWAP_INTENT"]
            deposits = [item for item in items if (
                item.behavior == "INTENT_DEPOSIT"
                and item.evidence.get("source_orchestrator") == "relay")]
            if len(aggregators) != 1 or len(deposits) != 1:
                for item in aggregators:
                    item.behavior = "UNKNOWN"
                    item.reasons.append("aggregator_call_not_linked_to_unique_relay_deposit")
                continue
            trade, deposit = aggregators[0], deposits[0]
            if (deposit.recipient != trade.wallet or deposit.token_in != C.usdg
                    or deposit.evidence.get("relay_cleanup_token") != deposit.token_in
                    or trade.token_out not in (None, deposit.token_in)
                    or (trade.protocol == "kyber" and trade.recipient != C.relay_router)):
                trade.behavior = "UNKNOWN"
                trade.reasons.append("aggregator_call_relay_deposit_identity_mismatch")
                continue
            trade.token_out = deposit.token_in
            trade.recipient = C.relay_router
            trade.behavior = side(trade.token_in, trade.token_out, C.quote_assets)
            if trade.behavior != "SELL":
                trade.behavior = "UNKNOWN"
                trade.reasons.append("relay_mvp_only_supports_token_to_usdg_sell")
                continue
            trade.evidence.update({
                "source_orchestrator": "relay",
                "relay_deposit_order_id": deposit.evidence["order_id"],
                "relay_deposit_path": deposit.path,
            })
        for item in result:
            if item.behavior == "AGGREGATOR_SWAP_INTENT":
                item.behavior = "UNKNOWN"
                item.reasons.append("aggregator_call_not_linked_to_relay_sell")
        return result
