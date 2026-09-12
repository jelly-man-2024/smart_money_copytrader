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


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def side(token_in: str, token_out: str) -> str:
    if token_in in R.QUOTE_ASSETS and token_out not in R.QUOTE_ASSETS:
        return "BUY"
    if token_out in R.QUOTE_ASSETS and token_in not in R.QUOTE_ASSETS:
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
    def __init__(self, watchlist: dict, delegations: dict[str, str] | None = None):
        self.watchlist = watchlist
        self.delegations = delegations or {}

    def decode(self, tx: Transaction) -> list[Signal]:
        if tx.chain_id != R.CHAIN_ID:
            return []
        result: list[Signal] = []
        budget = [0]

        def emit(wallet, mode, path, to, data, behavior, op=None, **fields):
            signal = Signal(
                tx_hash=tx.hash, wallet=wallet, mode=mode, behavior=behavior,
                path=path, contract=to, selector="0x" + data[:4].hex(), fresh=tx.fresh,
                userop_index=op[0] if op else None, userop_nonce=str(op[1]) if op else None,
                **fields,
            )
            result.append(signal)
            return signal

        def swap(wallet, mode, path, to, data, op, token_in, token_out,
                 amount, limit, recipient, protocol, exact_in=True, **extra):
            token_in, token_out = address(token_in), address(token_out)
            item = emit(
                wallet, mode, path, to, data, side(token_in, token_out), op,
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
                    if implementation == R.SIMPLE_ACCOUNT:
                        if sel == bytes.fromhex("b61d27f6"):
                            calls = [decode(["address", "uint256", "bytes"], args)]
                        elif sel == bytes.fromhex("34fcd5be"):
                            calls = decode([CALLS], args)[0]
                    elif implementation == R.METAMASK_ACCOUNT and sel == bytes.fromhex("e9ae5c53"):
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

                if to == R.RELAY_PROXY and sel == bytes.fromhex("f9e4bab4"):
                    tokens, amounts, calls, refund, nft, metadata = decode(
                        ["address[]", "uint256[]", RELAY_CALLS, "address", "address", "bytes"], args)
                    if len(tokens) != len(amounts) or len(calls) > 256:
                        raise ValueError("invalid Relay batch")
                    for i, (dest, allow_failure, amount, body) in enumerate(calls):
                        # Relay Router executes these calls, not the proxy itself.
                        walk(wallet, mode, dest, amount, body, path + f"/relay/{i}", op, depth + 1, R.RELAY_ROUTER)
                    return
                if to == R.RELAY_ROUTER and sel == bytes.fromhex("cd6e13f7"):
                    calls, refund, nft, metadata = decode([RELAY_CALLS, "address", "address", "bytes"], args)
                    if len(calls) > 256:
                        raise ValueError("Relay batch limit")
                    for i, (dest, allow_failure, amount, body) in enumerate(calls):
                        child(dest, amount, body, f"relay/{i}")
                    return

                if to == R.DEPOSITORY:
                    if sel == bytes.fromhex("e8017952"):
                        depositor, token, amount, order = decode(["address", "address", "uint256", "bytes32"], args)
                    elif sel == bytes.fromhex("49290c1c"):
                        depositor, order = decode(["address", "bytes32"], args)
                        token, amount = R.NATIVE, value
                    else:
                        raise ValueError("unsupported depository method")
                    emit(wallet, mode, path, to, data, "INTENT_DEPOSIT", op, token_in=token,
                         amount_in_raw=str(amount), recipient=depositor,
                         evidence={"order_id": "0x" + order.hex()},
                         reasons=["deposit_is_not_a_destination_purchase"])
                    return

                if to == R.RIPE_CLAIM and sel == bytes.fromhex("815a4392"):
                    recipient, flag = decode(["address", "bool"], args)
                    emit(wallet, mode, path, to, data, "CLAIM", op, recipient=recipient)
                    return

                if to == R.WETH:
                    if sel == bytes.fromhex("d0e30db0") and not args and value > 0:
                        emit(wallet, mode, path, to, data, "WRAP_NATIVE", op,
                             token_in=R.NATIVE, token_out=R.WETH,
                             amount_in_raw=str(value), recipient=wallet,
                             reasons=["wrap_is_asset_conversion_not_purchase"])
                        return
                    if sel == bytes.fromhex("2e1a7d4d"):
                        amount = decode(["uint256"], args)[0]
                        emit(wallet, mode, path, to, data, "UNWRAP_WETH", op,
                             token_in=R.WETH, token_out=R.NATIVE,
                             amount_in_raw=str(amount), recipient=wallet,
                             reasons=["unwrap_is_asset_conversion_not_sale"])
                        return

                if to == R.V3_ROUTER and sel in {bytes.fromhex("ac9650d8"), bytes.fromhex("5ae401dc")}:
                    calls = decode(["bytes[]"], args)[0] if sel.hex() == "ac9650d8" else decode(["uint256", "bytes[]"], args)[1]
                    if len(calls) > 256:
                        raise ValueError("router batch limit")
                    for i, body in enumerate(calls):
                        child(to, value, body, f"multicall/{i}")
                    return

                if to == R.V2_ROUTER:
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

                if to == R.V3_ROUTER:
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

                if to == R.UNIVERSAL_ROUTER and sel in {bytes.fromhex("3593564c"), bytes.fromhex("24856bc3")}:
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

                if to == R.PERMIT2 and sel == bytes.fromhex("87517c45"):
                    token, spender, amount, expiration = decode(["address", "address", "uint160", "uint48"], args)
                    emit(wallet, mode, path, to, data, "APPROVAL", op, token_in=token,
                         recipient=spender, amount_limit_raw=str(amount))
                    return
                if to in R.POSITION_MANAGERS:
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

        if tx.to == R.ENTRYPOINT and tx.data[:4] == bytes.fromhex("765e827f"):
            try:
                ops, beneficiary = decode([PACKED_OPS, "address"], tx.data[4:])
                if len(ops) > 256:
                    raise ValueError("UserOperation limit")
                for i, op in enumerate(ops):
                    wallet = address(op[0])
                    if wallet in self.watchlist:
                        walk(wallet, "bundled_account", wallet, 0, op[3], f"userop/{i}", (i, op[1]), caller=R.ENTRYPOINT)
            except Exception:
                # Untrusted malformed sender bytes must never become ownership evidence.
                return []
        elif tx.sender in self.watchlist:
            mode = "self_account" if tx.to == tx.sender else "direct"
            walk(tx.sender, mode, tx.to, tx.value, tx.data, "call", caller=tx.sender)
        return result
