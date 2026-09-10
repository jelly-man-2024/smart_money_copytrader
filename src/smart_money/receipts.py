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


def enrich(tx: Transaction, signals: list[Signal], receipt: dict, watchlist: dict) -> list[Signal]:
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
                signal.reasons.append("user_operation_scope_not_uniquely_proven")
                continue
            signal.execution_success, local_logs, op_hash = matches[0]
            signal.evidence["userop_hash"] = op_hash
            if not signal.execution_success:
                signal.stage = "failed"
                signal.reasons.append("user_operation_reverted")
                continue
        signal.evidence["wallet_erc20_deltas_raw"] = deltas(local_logs, signal.wallet)
        signal.stage = "execution_observed"
        if signal.behavior not in TRADE_BEHAVIORS:
            # Outer/UserOp success does not prove each allow-failure subcall succeeded.
            continue
        swap_logs = [log for log in local_logs if log.get("topics") and log["topics"][0] in SWAPS]
        matching = [log for log in swap_logs if (
            signal.protocol == "v4" and log["address"].lower() == R.V4_MANAGER
            and SWAPS[log["topics"][0]] == "v4" and len(log["topics"]) >= 2
            and log["topics"][1].lower() == signal.pool_id
        )]
        signal.evidence["swap_event_count_in_scope"] = len(swap_logs)
        signal.evidence["matching_v4_pool_events"] = len(matching)
        if not swap_logs:
            signal.stage = "needs_review"
            signal.reasons.append("no_swap_event_in_this_operation")
            continue
        if signal.protocol in ("v2", "v3"):
            signal.stage = "needs_review"
            signal.reasons.append("v2_v3_factory_pool_verification_not_implemented")
            continue
        group = (signal.wallet, signal.userop_index)
        if trade_counts[group] != 1 or group in uncertain_groups or len(matching) != 1 or len(swap_logs) != 1:
            signal.stage = "needs_review"
            signal.reasons.append("swap_attribution_ambiguous")
            continue
        net = signal.evidence["wallet_erc20_deltas_raw"]
        spent = int(net.get(signal.token_in, "0")) < 0
        received = int(net.get(signal.token_out, "0")) > 0
        if R.NATIVE in (signal.token_in, signal.token_out):
            signal.stage = "needs_review"
            signal.reasons.append("native_net_flows_require_trace_or_state_accounting")
        elif spent and received:
            signal.stage = "swap_evidenced"
            signal.evidence["actual_input_debit_raw"] = str(-int(net[signal.token_in]))
            signal.evidence["actual_output_credit_raw"] = net[signal.token_out]
            signal.reasons.append("receipt_level_evidence_not_finality_or_live_trade_approval")
        else:
            signal.stage = "needs_review"
            signal.reasons.append("wallet_exchange_flows_not_closed")

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
                evidence={"recipient_count": len(recipients), "watched_recipients": watched_recipients,
                          "swap_event_count": swap_count, "block_hash": receipt.get("blockHash")},
            ))
        else:
            for wallet in watched_recipients:
                signals.append(Signal(
                    tx.hash, wallet, "third_party", "EXTERNAL_DELIVERY_CANDIDATE" if swap_count else "INCOMING_TRANSFER",
                    "incoming", tx.to, "0x" + tx.data[:4].hex(), stage="needs_review", fresh=tx.fresh,
                    execution_success=True, reasons=["recipient_is_not_proof_of_order_ownership"],
                    evidence={"wallet_erc20_deltas_raw": deltas(logs, wallet), "swap_event_count": swap_count,
                              "block_hash": receipt.get("blockHash")},
                ))
    return signals
