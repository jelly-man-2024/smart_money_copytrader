"""Receipt evidence and per-UserOperation attribution, not trade execution."""
from __future__ import annotations

from collections import Counter, defaultdict

from eth_abi import decode
from eth_utils import keccak

from .models import Signal, Transaction, number
from . import registry as R


def topic(signature: str) -> str:
    return "0x" + keccak(text=signature).hex()


TRANSFER = topic("Transfer(address,address,uint256)")
USEROP = topic("UserOperationEvent(bytes32,address,address,uint256,bool,uint256,uint256)")
BEFORE = topic("BeforeExecution()")
SWAPS = {
    topic("Swap(address,uint256,uint256,uint256,uint256,address)"): "v2",
    topic("Swap(address,address,int256,int256,uint160,uint128,int24)"): "v3",
    topic("Swap(bytes32,address,int128,int128,uint160,uint128,int24,uint24)"): "v4",
}
DEPOSIT_RECORDED = "0x49fed1d0b752ce30eee63c7a81133f3363b532fec5d4d7dd1ccfd005de4555e1"
TRADE_BEHAVIORS = {"BUY", "SELL", "TOKEN_SWAP"}


def transfers(logs: list[dict]) -> list[tuple[str, str, str, int]]:
    result = []
    for log in logs:
        topics = log.get("topics", [])
        if (len(topics) == 3 and topics[0].lower() == TRANSFER
                and len(log.get("data", "")) == 66 and not log.get("removed", False)):
            try:
                if any(len(t) != 66 for t in topics):
                    continue
                result.append((log["address"].lower(), "0x" + topics[1][-40:].lower(),
                               "0x" + topics[2][-40:].lower(), int(log["data"], 16)))
            except (KeyError, ValueError):
                continue
    return result


def deltas(logs: list[dict], wallet: str) -> dict[str, str]:
    net = defaultdict(int)
    for token, sender, recipient, amount in transfers(logs):
        if sender == wallet:
            net[token] -= amount
        if recipient == wallet:
            net[token] += amount
    return {token: str(amount) for token, amount in net.items() if amount}


def operation_scopes(logs: list[dict]) -> dict[tuple[str, int], list[tuple[bool, list[dict], str]]]:
    result = defaultdict(list)
    start = None
    for i, log in enumerate(logs):
        topics = log.get("topics", [])
        if log.get("address", "").lower() != R.ENTRYPOINT or not topics:
            continue
        if topics[0] == BEFORE:
            start = i + 1
        elif topics[0] == USEROP and len(topics) == 4:
            try:
                nonce, success, cost, used = decode(["uint256", "bool", "uint256", "uint256"], bytes.fromhex(log["data"][2:]))
                wallet = "0x" + topics[2][-40:].lower()
                if start is not None:
                    result[(wallet, nonce)].append((success, logs[start:i], topics[1]))
                start = i + 1
            except Exception:
                start = None
    return result


def enrich(tx: Transaction, signals: list[Signal], receipt: dict, watchlist: dict,
           pool_checks: dict[str, dict] | None = None,
           native_checks: dict[str, dict] | None = None) -> list[Signal]:
    if receipt.get("transactionHash", "").lower() != tx.hash:
        raise ValueError("receipt transaction hash mismatch")
    logs = sorted(receipt.get("logs", []), key=lambda log: number(log.get("logIndex", 0)))
    if any(log.get("removed", False) for log in logs):
        raise ValueError("removed receipt logs")
    succeeded = number(receipt.get("status", 0)) == 1
    scopes = operation_scopes(logs)
    trade_counts = Counter((s.wallet, s.userop_index) for s in signals if s.behavior in TRADE_BEHAVIORS)
    uncertain_groups = {(s.wallet, s.userop_index) for s in signals if s.behavior == "UNKNOWN"}
    for signal in signals:
        signal.evidence["block_hash"] = receipt.get("blockHash")
        signal.evidence["block_number"] = number(receipt.get("blockNumber", 0))
        signal.evidence["canonicality"] = "not_rechecked_for_reorgs"
        signal.execution_success = succeeded
        signal.execution_status = "success" if succeeded else "reverted"
        local_logs = logs
        if not succeeded:
            signal.stage = "failed"
            signal.reasons.append("outer_transaction_reverted")
            continue
        if signal.userop_index is not None:
            matches = scopes.get((signal.wallet, int(signal.userop_nonce)), [])
            if len(matches) != 1:
                signal.stage = "needs_review"
                signal.execution_success = None
                signal.execution_status = "unknown"
                signal.reasons.append("user_operation_scope_not_uniquely_proven")
                continue
            signal.execution_success, local_logs, op_hash = matches[0]
            signal.execution_status = "success" if signal.execution_success else "reverted"
            signal.evidence["userop_hash"] = op_hash
            if not signal.execution_success:
                signal.stage = "failed"
                signal.reasons.append("user_operation_reverted")
                continue
        signal.evidence["wallet_erc20_deltas_raw"] = deltas(local_logs, signal.wallet)
        signal.stage = "execution_observed"
        if signal.behavior == "WRAP_NATIVE":
            received = int(signal.evidence["wallet_erc20_deltas_raw"].get(R.WETH, "0"))
            signal.evidence["actual_output_credit_raw"] = str(max(received, 0))
            if received != int(signal.amount_in_raw or "0"):
                signal.stage = "needs_review"
                signal.reasons.append("weth_wrap_credit_does_not_match_call_value")
        elif signal.behavior == "UNWRAP_WETH":
            spent = -int(signal.evidence["wallet_erc20_deltas_raw"].get(R.WETH, "0"))
            signal.evidence["actual_input_debit_raw"] = str(max(spent, 0))
            if spent != int(signal.amount_in_raw or "0"):
                signal.stage = "needs_review"
                signal.reasons.append("weth_unwrap_debit_does_not_match_requested_amount")
        if signal.behavior == "INTENT_DEPOSIT":
            matches = []
            for item in local_logs:
                topics = item.get("topics", [])
                if (item.get("address", "").lower() != R.DEPOSITORY or not topics
                        or topics[0] != DEPOSIT_RECORDED or len(item.get("data", "")) != 258):
                    continue
                try:
                    depositor, token, amount, order_id = decode(
                        ["address", "address", "uint256", "bytes32"], bytes.fromhex(item["data"][2:]))
                    candidate = (depositor.lower(), token.lower(), str(amount), "0x" + order_id.hex())
                    expected = (signal.wallet, signal.token_in, signal.evidence.get("order_id"))
                    identity = (candidate[0], candidate[1], candidate[3])
                    amount_matches = (signal.amount_in_raw is None
                                      or candidate[2] == signal.amount_in_raw)
                    if identity == expected and amount_matches:
                        matches.append(item)
                except Exception:
                    continue
            signal.evidence["matching_deposit_order_events"] = len(matches)
            if len(matches) == 1:
                if signal.amount_in_raw is None:
                    _, _, amount, _ = decode(
                        ["address", "address", "uint256", "bytes32"],
                        bytes.fromhex(matches[0]["data"][2:]))
                    signal.amount_in_raw = str(amount)
                    signal.evidence["actual_deposit_amount_raw"] = str(amount)
                signal.evidence["solver_order_status"] = "source_deposit_evidenced"
            else:
                signal.stage = "needs_review"
                signal.evidence["solver_order_status"] = "deposit_event_not_uniquely_proven"
                signal.reasons.append("deposit_order_event_not_uniquely_matched")
        if signal.behavior not in TRADE_BEHAVIORS:
            # Outer/UserOp success does not prove each allow-failure subcall succeeded.
            continue
        swap_logs = [log for log in local_logs if log.get("topics") and log["topics"][0] in SWAPS]
        if (signal.protocol in {"0x", "kyber"}
                and signal.evidence.get("source_orchestrator") == "relay"):
            signal.evidence["swap_event_count_in_scope"] = len(swap_logs)
            continue
        check = (pool_checks or {}).get(signal.event_id)
        if signal.protocol in ("v2", "v3"):
            signal.evidence["pool_verification"] = check or {
                "verified": False, "reason": "historical_pool_verification_unavailable"}
            expected_pools = [item["address"] for item in (check or {}).get("pools", [])]
            matching = [log for log in swap_logs if (
                check and check.get("verified") and SWAPS[log["topics"][0]] == signal.protocol
                and log.get("address", "").lower() in expected_pools
            )]
        else:
            expected_pools = []
            expected_v4_ids = ([signal.pool_id] if signal.pool_id else
                               signal.evidence.get("v4_pool_ids", []))
            signal.evidence["pool_verification"] = check or {
                "verified": False, "reason": "historical_pool_verification_unavailable"}
            matching = [log for log in swap_logs if (
                signal.protocol == "v4" and check and check.get("verified")
                and log["address"].lower() == R.V4_MANAGER
                and SWAPS[log["topics"][0]] == "v4" and len(log["topics"]) >= 2
                and log["topics"][1].lower() in expected_v4_ids
            )]
        signal.evidence["swap_event_count_in_scope"] = len(swap_logs)
        signal.evidence["matching_pool_events"] = len(matching)
        if not swap_logs:
            signal.stage = "needs_review"
            signal.reasons.append("no_swap_event_in_this_operation")
            continue
        if signal.protocol in ("v2", "v3"):
            if not check or not check.get("verified"):
                signal.stage = "needs_review"
                signal.reasons.append("v2_v3_factory_pool_not_verified")
                continue
            matched_addresses = Counter(log.get("address", "").lower() for log in matching)
            if matched_addresses != Counter(expected_pools):
                signal.stage = "needs_review"
                signal.reasons.append("verified_pool_swap_events_not_exactly_matched")
                continue
        elif signal.protocol == "v4":
            if not check or not check.get("verified"):
                signal.stage = "needs_review"
                signal.reasons.append("v4_pool_or_hook_not_verified")
                continue
            if Counter(log["topics"][1].lower() for log in matching) != Counter(expected_v4_ids):
                signal.stage = "needs_review"
                signal.reasons.append("verified_v4_pool_swap_events_not_exactly_matched")
                continue
            settlement = signal.evidence.get("v4_settlement_actions", [])
            payers = [item for item in settlement
                      if item.get("action") in {"SETTLE", "SETTLE_ALL"}
                      and item.get("currency") == signal.token_in and item.get("payer_is_user")]
            takes = [item for item in settlement
                     if item.get("action") in {"TAKE", "TAKE_ALL"}
                     and item.get("currency") == signal.token_out
                     and item.get("recipient") == signal.wallet]
            signal.evidence["v4_settlement_input_matches"] = len(payers)
            signal.evidence["v4_settlement_output_matches"] = len(takes)
            if len(payers) != 1 or len(takes) != 1:
                signal.stage = "needs_review"
                signal.reasons.append("v4_wallet_settlement_not_uniquely_proven")
                continue
            signal.recipient = signal.wallet
            signal.evidence["recipient_requires_settlement_check"] = False
        group = (signal.wallet, signal.userop_index)
        required_swap_events = (len(expected_pools) if signal.protocol in ("v2", "v3")
                                else len(expected_v4_ids))
        if (trade_counts[group] != 1 or group in uncertain_groups
                or len(matching) != required_swap_events or len(swap_logs) != required_swap_events):
            signal.stage = "needs_review"
            signal.reasons.append("swap_attribution_ambiguous")
            continue
        net = signal.evidence["wallet_erc20_deltas_raw"]
        native = (native_checks or {}).get(signal.event_id)
        if R.NATIVE in (signal.token_in, signal.token_out):
            signal.evidence["native_flow_verification"] = native or {
                "verified": False, "reason": "native_state_diff_unavailable"}
        native_delta = int((native or {}).get("wallet_native_asset_delta_raw", "0"))
        if (native and native.get("verified") and R.NATIVE in (signal.token_in, signal.token_out)
                and not native.get("wallet_is_outer_transaction_sender")):
            pool_delta = None
            if signal.protocol == "v4" and len(matching) == 1:
                try:
                    amount0, amount1, *_ = decode(
                        ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
                        bytes.fromhex(matching[0]["data"][2:]))
                    key = signal.evidence.get("pool_key", [])
                    if len(key) == 5:
                        pool_delta = amount0 if key[0] == R.NATIVE else amount1 if key[1] == R.NATIVE else None
                except Exception:
                    pool_delta = None
            native["native_matches_v4_pool_delta"] = (
                pool_delta is not None and abs(native_delta) == abs(pool_delta))
            if not native["native_matches_v4_pool_delta"]:
                native["verified"] = False
                native["reason"] = "bundled_native_flow_not_separable_from_gas_or_hook"
        spent = native_delta < 0 if signal.token_in == R.NATIVE else int(net.get(signal.token_in, "0")) < 0
        received = native_delta > 0 if signal.token_out == R.NATIVE else int(net.get(signal.token_out, "0")) > 0
        if R.NATIVE in (signal.token_in, signal.token_out) and not (native and native.get("verified")):
            signal.stage = "needs_review"
            signal.reasons.append("native_net_flows_require_trace_or_state_accounting")
        elif spent and received:
            signal.stage = "swap_evidenced"
            signal.evidence["actual_input_debit_raw"] = (
                str(-native_delta) if signal.token_in == R.NATIVE else str(-int(net[signal.token_in])))
            signal.evidence["actual_output_credit_raw"] = (
                str(native_delta) if signal.token_out == R.NATIVE else net[signal.token_out])
            signal.reasons.append("receipt_level_evidence_not_finality_or_live_trade_approval")
        else:
            signal.stage = "needs_review"
            signal.reasons.append("wallet_exchange_flows_not_closed")

    for signal in signals:
        if (signal.protocol not in {"0x", "kyber"}
                or signal.evidence.get("source_orchestrator") != "relay"
                or signal.behavior != "SELL"):
            continue
        group = (signal.wallet, signal.userop_index)
        deposits = [item for item in signals if (
            (item.wallet, item.userop_index) == group
            and item.behavior == "INTENT_DEPOSIT"
            and item.path == signal.evidence.get("relay_deposit_path"))]
        net = signal.evidence.get("wallet_erc20_deltas_raw", {})
        try:
            actual_input = -int(net.get(signal.token_in, "0"))
        except (TypeError, ValueError):
            actual_input = 0
        if (signal.execution_status != "success" or len(deposits) != 1
                or deposits[0].evidence.get("solver_order_status") != "source_deposit_evidenced"
                or deposits[0].evidence.get("order_id") != signal.evidence.get("relay_deposit_order_id")
                or deposits[0].token_in != signal.token_out
                or not deposits[0].amount_in_raw
                or int(deposits[0].amount_in_raw) <= 0
                or actual_input <= 0
                or actual_input != int(signal.amount_in_raw or "0")
                or int(signal.evidence.get("swap_event_count_in_scope", 0)) <= 0
                or trade_counts[group] != 1 or group in uncertain_groups):
            signal.stage = "needs_review"
            signal.reasons.append("relay_sell_evidence_not_uniquely_closed")
            continue
        signal.stage = "relay_sell_evidenced"
        signal.evidence.update({
            "actual_input_debit_raw": str(actual_input),
            "actual_output_deposit_raw": deposits[0].amount_in_raw,
            "source_deposit_event_id": deposits[0].event_id,
        })
        signal.reasons.append("relay_source_deposit_is_not_destination_finality_or_trade_approval")

    if not signals and succeeded:
        events = transfers(logs)
        recipients = {recipient for _, _, recipient, _ in events}
        senders = {sender for _, sender, _, _ in events}
        watched_recipients = sorted(recipients & watchlist.keys())
        if not watched_recipients:
            return signals
        swap_count = sum(bool(log.get("topics")) and log["topics"][0] in SWAPS for log in logs)
        bulk = (len(recipients) >= 100 and len(senders) == 1
                and len({token for token, _, _, _ in events}) == 1
                and len({amount for _, _, _, amount in events}) == 1
                and not (senders & watchlist.keys()) and not swap_count)
        if bulk:
            # One summary per distribution, not hundreds of fake buy signals.
            signals.append(Signal(
                tx.hash, watched_recipients[0], "third_party", "BULK_DISTRIBUTION", "distribution",
                tx.to, "0x" + tx.data[:4].hex(), stage="execution_observed", execution_success=True,
                fresh=tx.fresh, reasons=["passive_distribution_not_evidence_of_a_purchase"],
                intent_status="not_attributed", execution_status="success",
                evidence={"recipient_count": len(recipients), "watched_recipients": watched_recipients,
                          "swap_event_count": swap_count, "block_hash": receipt.get("blockHash")},
            ))
        else:
            for wallet in watched_recipients:
                signals.append(Signal(
                    tx.hash, wallet, "third_party", "EXTERNAL_DELIVERY_CANDIDATE" if swap_count else "INCOMING_TRANSFER",
                    "incoming", tx.to, "0x" + tx.data[:4].hex(), stage="needs_review", fresh=tx.fresh,
                    execution_success=True, reasons=["recipient_is_not_proof_of_order_ownership"],
                    intent_status="not_attributed", execution_status="success",
                    evidence={"wallet_erc20_deltas_raw": deltas(logs, wallet), "swap_event_count": swap_count,
                              "block_hash": receipt.get("blockHash")},
                ))
    return signals
