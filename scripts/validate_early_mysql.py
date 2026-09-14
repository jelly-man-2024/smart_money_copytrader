#!/usr/bin/env python3
"""Isolated prefixed-table MySQL contract test; never touches runtime table rows.

Explicit --run required. Uses business DB admin to create and remove ONLY this
run's randomly named synthetic tables. No keys, RPC, live configuration or Feed.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import threading
import uuid

from smart_money.mysql_config import mysql_connection
from smart_money.mysql_store import MySqlConnectionCompat, MySqlStore
from smart_money.registry import USDG
from smart_money.models import Signal


def run():
    prefix = "earlytest_" + uuid.uuid4().hex[:16] + "_"
    root = Path(__file__).resolve().parents[1]
    statements = []
    for filename in ("003_runtime_ledger.sql", "008_copy_operation_claims.sql",
                     "009_early_trials.sql", "010_early_feed_jobs.sql"):
        source = (root / "docker/mysql/init" / filename).read_text()
        source = re.sub(r"--[^\n]*", "", source)
        statements.extend(s.strip() for s in source.split(";")
                          if s.strip().upper().startswith("CREATE TABLE"))
    names = [re.search(r"CREATE TABLE IF NOT EXISTS (\w+)", s, re.I)[1] for s in statements]
    table_pattern = re.compile(r"\b(" + "|".join(names) + r")\b")
    def renamed(sql):
        return table_pattern.sub(lambda m: prefix + m[0], sql)
    class IsolatedCompat(MySqlConnectionCompat):
        @classmethod
        def _sql(cls, sql, lock=False):
            return renamed(super()._sql(sql, lock))
    def connection_factory(**kwargs):
        kwargs["write"] = True
        return mysql_connection(**kwargs)
    def store():
        result = MySqlStore(connection_factory)
        result.connection = IsolatedCompat(result.connection._connection, connection_factory)
        return result
    admin = mysql_connection(write=True, autocommit=True, dict_rows=False)
    created, opened = [], []
    try:
        with admin.cursor() as q:
            for name, sql in zip(names, statements):
                target = prefix + name
                q.execute("SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE() AND table_name=%s", (target,))
                if q.fetchone()[0]:
                    raise ValueError("random test table already exists")
                sql = re.sub(r"CONSTRAINT\s+(\w+)", lambda m: "CONSTRAINT " + prefix + m[1], renamed(sql), flags=re.I)
                q.execute(sql)
                created.append(target)
        db = store(); opened.append(db)
        smart, follower, token = ("0x" + x * 20 for x in ("11", "22", "33"))
        order, tx = "0x" + "ab" * 32, "0x" + "cd" * 32
        db.start_paper_budget_cycle("test", "isolated")
        db.configure_paper_budget(smart, "USDG", "1000")
        db.start_early_trial("test", follower, [1])
        def proposal(name):
            return dict(proposal_id=name, source_event_id=name, source_tx_hash=tx,
                wallet=smart, trigger_mode="feed_intent", strategy_version="test", input_asset=USDG,
                output_asset=token, budget_bucket="USDG", amount_in_raw="100",
                attribution=dict(smart_wallet=smart, follower_wallet=follower, relationship_id="1",
                                 copy_operation_order_id=order, early_trial_id="test", source_behavior="BUY",
                                 source_stage="intent", source_position_status="pending"))
        peers = [store(), store()]; opened.extend(peers)
        barrier = threading.Barrier(2)
        def reserve(i):
            barrier.wait(timeout=5)
            return peers[i].reserve_paper_proposal(proposal("p" + str(i)))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
        if sum(bool(r[0]) for r in results) != 1:
            raise ValueError("operation race did not have exactly one winner")
        if db.paper_budget(smart, "USDG")["reserved_raw"] != "100":
            raise ValueError("operation race budget mismatch")
        winner = "p" + str(next(i for i, r in enumerate(results) if r[0]))
        if not db.cancel_paper_proposal(winner, "synthetic cancellation"):
            raise ValueError("cancel failed")
        if not db.reserve_paper_proposal(proposal("fallback"))[0]:
            raise ValueError("strict fallback handoff failed")
        # Reopening must preserve the same trial window and operation lock.
        reopened = store(); opened.append(reopened)
        if reopened.reserve_paper_proposal(proposal("restart"))[0]:
            raise ValueError("restart duplicated operation")
        if reopened.early_trial_status("test")["started_at"] != db.early_trial_status("test")["started_at"]:
            raise ValueError("trial window changed")
        strict = Signal(tx, smart, "third_party", "BUY", "strict", None, "",
            stage="relay_buy_evidenced", execution_status="success", execution_success=True,
            token_in=USDG, token_out=token,
            evidence={"relay_order_id": order, "actual_output_credit_raw": "5000"})
        db.put(strict)
        db.fill_paper_buy("fallback", dict(order_id="o", fill_id="f", lot_id="lot",
            amount_out_raw="1000", fee_asset=USDG, fee_amount_raw="0", gas_cost_wei="0",
            quote_observed_at="2026-09-14T00:00:00Z", filled_at="2026-09-14T00:00:01Z"))
        if db.paper_position("lot")["attribution"].get("source_position_remaining_raw") != "5000":
            raise ValueError("source-before-fill reconciliation failed")
        strict.canonical_status = "orphaned"
        db.put(strict)
        if db.paper_position("lot")["attribution"].get("source_position_status") != "source_orphaned":
            raise ValueError("source reorg did not invalidate basis")
        print(json.dumps({"passed": True, "checks": ["mysql_same_order_concurrency", "atomic_budget",
            "cancel_fallback", "restart_dedup", "trial_window_preserved", "source_before_fill", "source_orphaned"],
            "runtime_rows_modified": False, "private_keys_read": False, "broadcast_performed": False}))
    finally:
        for db in opened:
            db.close()
        # Exact list of newly created tables only, in reverse FK dependency order.
        with admin.cursor() as q:
            for target in reversed(created):
                if target not in {prefix + n for n in names} or not re.fullmatch(r"earlytest_[a-f0-9]{16}_[a-z_]+", target):
                    raise ValueError("unsafe synthetic cleanup target")
                q.execute("DROP TABLE `" + target + "`")
        admin.close()
        print(json.dumps({"synthetic_tables_removed": len(created), "runtime_tables_removed": 0}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    if args.run:
        try:
            run()
        except Exception as exc:
            print(json.dumps({"passed": False, "error_type": type(exc).__name__}))
            raise SystemExit(1)
    else:
        print('{"enabled":false,"network_used":false}')
