"""Post-run measurements from recorded wall clocks; never invent send timestamps."""
from collections import Counter
import math


def _percentiles(values):
    values = sorted(values)
    return {"count": len(values), **({f"p{p}_ms": values[max(0, math.ceil(len(values)*p/100)-1)]
            for p in (50, 95)} if values else {})}


def summarize_early_trial(proposals, signals, events, trial_id):
    by_proposal, strict, settled_blocks = {}, {}, {}
    for event in events:
        at = event.get("observed_at")
        if type(at) not in (int, float) or not math.isfinite(at):
            continue
        pid = event.get("proposal_id")
        if pid:
            marks = by_proposal.setdefault(pid, {})
            key = event.get("event")
            marks[key] = min(at, marks.get(key, at))
            if key == "live_execution_settled" and type(event.get("block_number")) is int:
                settled_blocks[pid] = event["block_number"]
        if event.get("event") == "source_evidence_available" and event.get("stage") in {
                "swap_evidenced", "relay_buy_evidenced", "relay_sell_evidenced"}:
            key = (event.get("source_tx_hash"), event.get("smart_wallet"), event.get("order_id"))
            strict[key] = min(at, strict.get(key, at))
    counts, latency, rows = Counter(), {}, []
    for p in proposals:
        attr = p["attribution"]
        if attr.get("early_trial_id") != trial_id:
            continue
        counts["reserved_proposals"] += 1
        marks = by_proposal.get(p["proposal_id"], {})
        ack = marks.get("live_execution_broadcast")
        if ack is None:
            counts["broadcast_not_acknowledged"] += 1
            continue
        counts["broadcast_acknowledged"] += 1
        key = (p["source_tx_hash"], attr["smart_wallet"], attr["copy_operation_order_id"])
        observed = strict.get(key)
        if observed is not None:
            counts["comparable_strict_timing"] += 1
            counts["acknowledged_before_strict_evidence"] += ack < observed
        else:
            counts["strict_timing_unknown"] += 1
        truth = [s for s in signals if s.get("tx_hash") == key[0] and s.get("wallet") == key[1]
                 and s.get("evidence", {}).get("relay_order_id", s.get("evidence", {}).get("relay_deposit_order_id")) == key[2]]
        labels = set()
        for s in truth:
            if s.get("canonical_status") == "orphaned":
                labels.add("source_orphaned")
            elif s.get("execution_status") in {"reverted", "failed"}:
                labels.add("source_failed")
            elif s.get("stage") in {"swap_evidenced", "relay_buy_evidenced", "relay_sell_evidenced"} and s.get("execution_status") == "success":
                same = (s.get("behavior") == attr["source_behavior"] and s.get("token_in") == p["input_asset"]
                        and s.get("token_out") == p["output_asset"])
                if same and attr["source_behavior"] == "SELL":
                    same = s.get("evidence", {}).get("actual_input_debit_raw") == attr["source_amount_in_raw"]
                labels.add("matched" if same else "source_mismatch")
        label = next(iter(labels)) if len(labels) == 1 else "unknown"
        counts[label] += 1
        for name, start, end in (
                ("feed_to_broadcast_ack", attr.get("early_received_at"), ack),
                ("feed_to_decision", attr.get("early_received_at"), attr.get("early_checked_at")),
                ("decision_to_prepared", attr.get("early_checked_at"), marks.get("live_execution_prepared")),
                ("prepared_to_signed", marks.get("live_execution_prepared"), marks.get("live_execution_signed")),
                ("send_call_to_ack", marks.get("live_execution_send_started"), ack),
                ("feed_to_own_settlement_observed", attr.get("early_received_at"), marks.get("live_execution_settled"))):
            if type(start) in (int, float) and type(end) in (int, float) and end >= start:
                latency.setdefault(name, []).append(round((end-start)*1000, 3))
        source_blocks = {s.get("evidence", {}).get("block_number") for s in truth
                         if s.get("canonical_status") != "orphaned" and s.get("execution_status") == "success"}
        source_block = next(iter(source_blocks)) if len(source_blocks) == 1 else None
        own_block = settled_blocks.get(p["proposal_id"])
        block_gap = own_block - source_block if type(own_block) is int and type(source_block) is int else None
        rows.append({"proposal_id": p["proposal_id"], "source_outcome": label,
                     "broadcast_ack_at": ack, "strict_evidence_at": observed,
                     "own_block_minus_source_block": block_gap})
    total = counts["broadcast_acknowledged"]
    wrong = sum(counts[k] for k in ("source_failed", "source_orphaned", "source_mismatch"))
    return {"trial_id": trial_id, "counts": dict(counts), "known_misfollow_count": wrong,
            "known_misfollow_fraction_of_acknowledged": wrong/total if total else None,
            "unknown_source_fraction": counts["unknown"]/total if total else None,
            "latency": {k: _percentiles(v) for k, v in latency.items()}, "rows": rows,
            "notes": ["Broadcast acknowledgement is not on-chain inclusion or successful settlement.",
                      "Absent/ambiguous evidence remains unknown, not a successful match.",
                      "No same-trade counterfactual execution latency is inferred."]}
