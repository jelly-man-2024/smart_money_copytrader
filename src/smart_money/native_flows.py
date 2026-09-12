"""Read-only per-transaction native balance evidence from state-diff traces."""
from __future__ import annotations

from .models import Signal, Transaction, number
from . import registry as R


async def verify_native_flows(rpc, tx: Transaction, receipt: dict,
                              signals: list[Signal]) -> dict[str, dict]:
    relevant = [signal for signal in signals
                if signal.behavior in {"BUY", "SELL", "TOKEN_SWAP"}
                and R.NATIVE in (signal.token_in, signal.token_out)]
    if not relevant:
        return {}
    trace = await rpc.call("debug_traceTransaction", [tx.hash, {
        "tracer": "prestateTracer", "tracerConfig": {"diffMode": True},
    }])
    if not isinstance(trace, dict) or not isinstance(trace.get("pre"), dict) \
            or not isinstance(trace.get("post"), dict):
        raise ValueError("invalid native state-diff trace")
    pre = {key.lower(): value for key, value in trace["pre"].items()}
    post = {key.lower(): value for key, value in trace["post"].items()}
    results = {}
    for signal in relevant:
        evidence = {"verified": False, "trace_type": "prestateTracer_diffMode"}
        results[signal.event_id] = evidence
        before, after = pre.get(signal.wallet), post.get(signal.wallet)
        if not isinstance(before, dict) or not isinstance(after, dict) \
                or "balance" not in before or "balance" not in after:
            evidence["reason"] = "wallet_balance_not_explicit_in_state_diff"
            continue
        delta = number(after["balance"]) - number(before["balance"])
        gas_adjustment = 0
        if tx.sender == signal.wallet:
            if "gasUsed" not in receipt or "effectiveGasPrice" not in receipt:
                evidence["reason"] = "outer_sender_gas_fields_missing"
                continue
            gas_adjustment = number(receipt["gasUsed"]) * number(receipt["effectiveGasPrice"])
        asset_delta = delta + gas_adjustment
        evidence.update({
            "verified": True,
            "wallet_is_outer_transaction_sender": tx.sender == signal.wallet,
            "wallet_balance_before_raw": str(number(before["balance"])),
            "wallet_balance_after_raw": str(number(after["balance"])),
            "wallet_native_delta_including_gas_raw": str(delta),
            "outer_transaction_gas_adjustment_raw": str(gas_adjustment),
            "wallet_native_asset_delta_raw": str(asset_delta),
        })
    return results
