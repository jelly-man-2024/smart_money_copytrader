"""Offline summary of a shadow JSONL capture, not a real-trade success report."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path


def summarize(records):
    early, labels, counters, rejected, runs, errors = {}, {}, {}, 0, set(), Counter()
    for row in records:
        runs.add(row.get("run_id"))
        kind = row.get("record_type")
        if kind == "early_case":
            early[row["case"]["record_id"]] = row
            errors.update(row.get("capture_errors", {}).keys())
        elif kind == "reconciliation":
            labels[row["record_id"]] = row["label"]
        elif kind == "run_finished":
            counters[row["run_id"]] = row["counters"]
        elif kind == "parse_rejected":
            rejected += 1
    groups, delays = {}, []
    for rid, row in early.items():
        result = row["result"]
        c = result["candidate"]
        group = groups.setdefault(c["side"] + ":" + c["route_kind"], Counter())
        group["candidate_relationships"] += 1
        for name, check in result["checks"].items():
            group[name + ":" + check["status"]] += 1
        group["reconciliation:" + labels.get(rid, "pending")] += 1
        delays.append(row["feed_to_evaluation_ms"])
    delays.sort()
    percentile = lambda p: delays[max(0, math.ceil(len(delays) * p) - 1)] if delays else None
    return {"shadow_only": True, "real_trades_submitted": 0,
            "actual_misfollow_rate": None,
            "scope": "signal_attribution_and_business_snapshots_no_market_or_preparation",
            "runs": len(runs), "runs_without_finish_record": len(runs - counters.keys()),
            "candidate_relationships": len(early), "parse_rejected_wallet_transactions": rejected,
            "groups": groups, "capture_errors": errors, "run_counters": counters,
            "feed_to_evaluation_ms": {"p50": percentile(.5), "p95": percentile(.95)},
            "latency_scope": "capture_and_evaluation_not_order_submission_or_inclusion",
            "pending_is_not_failure": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args(argv)
    if args.input.stat().st_size > 64 * 1024 * 1024:
        parser.error("capture exceeds 64 MiB")
    with args.input.open(encoding="utf-8") as stream:
        result = summarize(json.loads(line) for line in stream if line.strip())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
