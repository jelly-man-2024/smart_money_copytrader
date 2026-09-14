"""Read-only historical coverage report. No Store, signer or executor imports.

Default input is an explicitly supplied JSON file. --mysql selects only public
business-ledger rows inside a READ ONLY transaction; it never reads wallet_keys.
Reports go to stdout, diagnostics contain no connection strings or raw calldata.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import sys

from smart_money.early_intent import hash32
from smart_money.early_replay import replay_cases


def _object(value):
    return json.loads(value) if isinstance(value, str) else value


def mysql_cases(limit, cohort=None, reconstruct=False):
    from smart_money.mysql_config import mysql_connection
    connection = mysql_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute("SELECT UTC_TIMESTAMP(6) AS t")
            captured_at = str(cursor.fetchone()["t"])
            condition, parameters = "", []
            if cohort is not None:
                hashes = sorted({hash32(h) for h in cohort})
                if not hashes or len(hashes) > 1000:
                    raise ValueError("invalid cohort size")
                condition = " AND p.source_tx_hash IN (" + ",".join(["%s"] * len(hashes)) + ")"
                parameters.extend(hashes)
            # LEFT JOIN retains missing candidates in the denominator as errors.
            cursor.execute("""SELECT p.proposal_id,p.attribution_payload,
                       c.payload AS candidate_payload,s.payload AS signal_payload
                FROM paper_proposals p
                LEFT JOIN candidates c ON c.tx_hash=p.source_tx_hash
                LEFT JOIN signals s ON s.event_id=JSON_UNQUOTE(
                    JSON_EXTRACT(p.attribution_payload,'$.source_event_id'))
                WHERE EXISTS (SELECT 1 FROM execution_plans e
                    JOIN execution_attempts a ON a.plan_id=e.plan_id
                    WHERE e.proposal_id=p.proposal_id AND a.status IN ('confirmed','reverted'))
                """ + condition + " ORDER BY p.created_at DESC,p.proposal_id LIMIT %s",
                (*parameters, limit))
            cases = []
            for row in cursor.fetchall():
                attr = _object(row["attribution_payload"])
                tx = _object(row["candidate_payload"])
                cases.append({"transaction": tx, "wallet": attr["smart_wallet"],
                              "relationship_id": attr.get("relationship_id"),
                              "record_id": row["proposal_id"],
                              "expected_side": attr["source_behavior"],
                              "truth": _object(row["signal_payload"]),
                              "provenance": "business_mysql_read_only:" + captured_at,
                              "snapshots": {}})
            provenance = {"source": "business_mysql_read_only", "captured_at_utc": captured_at,
                           "selected_records": len(cases), "limit": limit,
                           "selection": "proposals_with_confirmed_or_reverted_follower_attempt",
                           "historical_context_not_reconstructed_from_current_state": True}
            if reconstruct:
                from smart_money.early_history import read_context
                provenance["recorded_context"] = read_context(cursor, cases)
            return cases, provenance
    finally:
        connection.rollback()
        connection.close()


def log_baseline(path, cases):
    """Only read the prefix present at opening; retain original intent freshness."""
    scope = {(case["transaction"]["hash"], case["wallet"]) for case in cases
             if case.get("transaction")}
    hashes = {h for h, _ in scope}
    digest, intents, decisions = hashlib.sha256(), {}, {}
    with path.open("rb") as stream:
        import os
        size = os.fstat(stream.fileno()).st_size
        consumed = 0
        for lineno, line in enumerate(stream, 1):
            if consumed + len(line) > size:
                break
            consumed += len(line)
            digest.update(line)
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            if not isinstance(row, dict):
                continue
            key = (row.get("tx_hash"), row.get("wallet"))
            if key in scope and row.get("stage") == "intent" and row.get("behavior") in {"BUY", "SELL"}:
                intents.setdefault(row["event_id"], {"line": lineno, "side": row["behavior"],
                                                    "fresh": row.get("fresh"),
                                                    "tx_hash": key[0], "wallet": key[1]})
            eid = row.get("source_event_id", "")
            parts = eid.split(":") if isinstance(eid, str) else []
            if (row.get("event") == "paper_decision" and row.get("trigger_mode") == "feed_intent"
                    and len(parts) >= 3 and (parts[1], parts[2]) in scope):
                decisions.setdefault(row["decision_id"], {
                    "line": lineno, "accepted": row.get("accepted"), "reason": row.get("reason")})
    # Stored candidates can be rewritten during enrichment. Original intent logs
    # are stronger evidence of the freshness flag used at the early decision.
    stale = {(i["tx_hash"], i["wallet"]) for i in intents.values() if i["fresh"] is False}
    for case in cases:
        tx = case.get("transaction")
        if tx and (tx["hash"], case["wallet"]) in stale:
            case["transaction"] = {**tx, "fresh": False}
    return {"path": str(path), "prefix_bytes": consumed, "sha256": digest.hexdigest(),
            "cohort_hash_count": len(hashes), "intent_count": len(intents),
            "intent_sides": dict(Counter(v["side"] for v in intents.values())),
            "intent_stale": sum(v["fresh"] is False for v in intents.values()),
            "feed_decisions": len(decisions),
            "feed_accepted": sum(v["accepted"] is True for v in decisions.values()),
            "rejections": dict(Counter(v["reason"] for v in decisions.values() if not v["accepted"]))}


def mysql_controls(wallets):
    """Deterministic labeled controls; unresolved rows are NOT negative truth."""
    from smart_money.mysql_config import mysql_connection
    connection = mysql_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute("START TRANSACTION READ ONLY")
            cursor.execute("SELECT UTC_TIMESTAMP(6) AS t")
            captured = str(cursor.fetchone()["t"])
            cursor.execute("SELECT payload FROM signals WHERE JSON_UNQUOTE(JSON_EXTRACT(payload,'$.wallet')) IN ("
                           + ",".join(["%s"] * len(wallets)) + ") OR "
                           "JSON_UNQUOTE(JSON_EXTRACT(payload,'$.behavior'))='BULK_DISTRIBUTION'", tuple(wallets))
            grouped = defaultdict(list)
            for row in cursor.fetchall():
                signal = _object(row["payload"])
                grouped[(signal["tx_hash"], signal["wallet"])].append(signal)
            pools = defaultdict(list)
            for (tx_hash, wallet), signals in grouped.items():
                behaviors = {s["behavior"] for s in signals}
                if behaviors & {"BUY", "SELL", "TOKEN_SWAP"}:
                    continue
                if all(s.get("execution_status") in {"failed", "reverted"} for s in signals):
                    label = "failed_nontrade"
                elif behaviors == {"BULK_DISTRIBUTION"}:
                    label = "bulk"
                elif behaviors & {"INCOMING_TRANSFER", "EXTERNAL_DELIVERY_CANDIDATE", "UNKNOWN"}:
                    label = "unresolved_not_negative_truth"
                else:
                    label = "other_nontrade"
                pools[label].append((tx_hash, wallet))
            selected = [(label, h, w) for label, items in sorted(pools.items()) for h, w in sorted(items)[:100]]
            hashes = sorted({h for _, h, _ in selected})
            txs = {}
            if hashes:
                cursor.execute("SELECT tx_hash,payload FROM candidates WHERE tx_hash IN ("
                               + ",".join(["%s"] * len(hashes)) + ")", tuple(hashes))
                txs = {r["tx_hash"]: _object(r["payload"]) for r in cursor.fetchall()}
            cases = [{"transaction": txs.get(h), "wallet": w,
                      "expected_side": "control:" + label, "snapshots": {},
                      "provenance": "business_mysql_controls_read_only:" + captured}
                     for label, h, w in selected]
            return cases, {"captured_at_utc": captured, "population_groups": len(grouped),
                           "available": {k: len(v) for k, v in pools.items()},
                           "sampling": "up to 100 per category, hash-sorted, NOT random",
                           "labels": "existing ledger classifications; not independent ground truth"}
    finally:
        connection.rollback()
        connection.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--input", type=Path, help="JSON {cases:[...]} or a case array")
    source.add_argument("--mysql", action="store_true", help="read-only BUSINESS database")
    parser.add_argument("--cohort", type=Path, help="JSON {tx_hashes:[...]} (MySQL only)")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--log", type=Path, help="optional original JSONL monitor log")
    parser.add_argument("--evaluate", action="store_true", help="enable OFFLINE decision checks, never live")
    parser.add_argument("--summary", action="store_true", help="omit per-record rows")
    parser.add_argument("--controls", action="store_true", help="include read-only MySQL negative/unknown controls")
    parser.add_argument("--audit-race-code", action="store_true",
                        help="explicit read-only RPC historical code audit; NEVER creates early snapshots")
    parser.add_argument("--reconstruct-context", action="store_true",
                        help="audit recorded business context and recover compatible snapshots; requires --mysql --log")
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 1000 or (args.cohort and not args.mysql):
        parser.error("limit must be 1..1000; --cohort requires --mysql")
    if args.controls and not args.mysql:
        parser.error("--controls requires --mysql")
    if args.reconstruct_context and (not args.mysql or not args.log):
        parser.error("--reconstruct-context requires --mysql and --log")
    try:
        if args.mysql:
            cohort = json.loads(args.cohort.read_text())["tx_hashes"] if args.cohort else None
            cases, provenance = mysql_cases(args.limit, cohort, args.reconstruct_context)
        else:
            raw = args.input.read_bytes()
            if len(raw) > 64 * 1024 * 1024:
                raise ValueError("input too large")
            document = json.loads(raw)
            cases = document["cases"] if isinstance(document, dict) else document
            if not isinstance(cases, list) or len(cases) > 1000:
                raise ValueError("invalid case list")
            provenance = {"source": str(args.input), "sha256": hashlib.sha256(raw).hexdigest()}
        baseline = log_baseline(args.log, cases) if args.log else None
        history = None
        if args.reconstruct_context:
            from smart_money.early_history import audit_archives, read_log, reconstruct_cases
            cases, history = reconstruct_cases(cases, provenance.pop("recorded_context"), read_log(args.log, cases))
            data = Path(__file__).resolve().parents[1] / "data"
            history["public_archive_inventory"] = audit_archives([data / name for name in (
                "relay_sell_evidence_2026-09-13.json", "relay_order_evidence_3ccc6f52.json",
                "relay_passive_buy_evidence_2026-09-12.json", "relay_race_runtime_2026-09-14.json",
                "account_codes.json")], cases)
        control_provenance = None
        if args.controls:
            controls, control_provenance = mysql_controls(sorted({c["wallet"] for c in cases}))
            cases.extend(controls)
        report = replay_cases(cases, enabled=args.evaluate)
        report["provenance"] = provenance
        if baseline:
            report["original_log_baseline"] = baseline
        if control_provenance:
            report["control_provenance"] = control_provenance
        if history:
            report["historical_context_audit"] = history
        report["paths"] = dict(Counter(r["candidate"]["route_kind"]
                                      for row in report["rows"] for r in row["results"]))
        report["signature_valid_count"] = sum(
            r["candidate"]["metadata"].get("signature_valid") is True
            for row in report["rows"] for r in row["results"])
        report["posthoc"] = dict(Counter(r["posthoc_reconciliation"]
                                        for row in report["rows"] for r in row["results"]))
        if args.audit_race_code:
            import os
            from smart_money.config import load_endpoint_env
            from smart_money.race_audit import audit_deployments
            from smart_money.rpc import ReadOnlyRpc
            load_endpoint_env()
            async def audit():
                return await audit_deployments(cases, ReadOnlyRpc(
                    os.environ["ROBINHOOD_RPC_URL"], concurrency=2, timeout=5))
            report["posthoc_race_deployment_audit"] = asyncio.run(audit())
        if args.summary:
            report.pop("rows")
            if history:
                from smart_money.early_history import compact_history
                report["historical_context_audit"] = compact_history(history)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["errors"] else 0
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "read_only": True,
                          "private_key_read": False, "broadcast_performed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
