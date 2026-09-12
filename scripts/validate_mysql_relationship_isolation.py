#!/usr/bin/env python3
"""Temporary real-MySQL validation for multi-follower relationship isolation."""
from __future__ import annotations

import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from smart_money.mysql_config import (
    MySqlRelationshipGate, load_mysql_paper_config, mysql_connection,
)
from smart_money.store import Store


TEST_FOLLOWERS = (
    "0x00000000000000000000000000000000000000f1",
    "0x00000000000000000000000000000000000000f2",
)
IMMUTABLE_COLUMNS = {"id", "created_at", "updated_at"}


def main() -> None:
    connection = mysql_connection(write=True)
    inserted_ids: list[int] = []
    smart_wallet = None
    try:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT id FROM copy_relationships WHERE follower_wallet IN (%s,%s)",
                TEST_FOLLOWERS,
            )
            if cursor.fetchall():
                raise ValueError("reserved validation follower already exists")
            cursor.execute("SELECT * FROM copy_relationships ORDER BY id LIMIT 1")
            template = cursor.fetchone()
            if template is None:
                raise ValueError("relationship template row is unavailable")
            smart_wallet = template["smart_wallet"]
            columns = [name for name in template if name not in IMMUTABLE_COLUMNS]
            quoted = ",".join(f"`{name}`" for name in columns)
            placeholders = ",".join(["%s"] * len(columns))
            sql = f"INSERT INTO copy_relationships ({quoted}) VALUES ({placeholders})"
            for index, follower in enumerate(TEST_FOLLOWERS, 1):
                row = dict(template)
                row.update({
                    "follower_wallet": follower,
                    "follower_label": f"validation-follower-{index}",
                    "enabled": True,
                    "strategy_version": f"mysql-isolation-v{index}",
                    "trigger_mode": "swap_evidenced" if index == 1 else "receipt_success",
                    "shadow_trigger_modes": json.dumps(
                        ["feed_intent", "receipt_success"] if index == 1
                        else ["feed_intent", "swap_evidenced"]),
                })
                quote = (row["quote_policy"] if isinstance(row["quote_policy"], dict)
                         else json.loads(row["quote_policy"]))
                row["quote_policy"] = json.dumps({
                    **quote, "max_slippage_bps": 100 if index == 1 else 500,
                })
                cursor.execute(sql, [row[name] for name in columns])
                inserted_ids.append(cursor.lastrowid)
        connection.commit()

        config = load_mysql_paper_config()
        selected = [policy for policy in config.policies_for(smart_wallet)
                    if policy.follower_wallet in TEST_FOLLOWERS]
        if len(selected) != 2:
            raise AssertionError("runtime reader did not load both validation relationships")
        if ({policy.strategy_version for policy in selected}
                != {"mysql-isolation-v1", "mysql-isolation-v2"}):
            raise AssertionError("relationship strategy versions were merged")
        if {policy.quote_policy.max_slippage_bps for policy in selected} != {100, 500}:
            raise AssertionError("relationship quote policies were merged")
        if len({policy.snapshot_hash for policy in selected}) != 2:
            raise AssertionError("relationship snapshots were merged")
        gate = MySqlRelationshipGate()
        for policy in selected:
            gate.validate(policy.relationship_id, policy.follower_wallet,
                          policy.wallet, policy.snapshot_hash)
        disabled_id = next(policy.relationship_id for policy in selected
                           if policy.follower_wallet == TEST_FOLLOWERS[0])
        with connection.cursor() as cursor:
            cursor.execute("UPDATE copy_relationships SET enabled=FALSE WHERE id=%s",
                           (int(disabled_id),))
        connection.commit()
        emergency_stop_rejected = False
        try:
            stopped = next(policy for policy in selected
                           if policy.relationship_id == disabled_id)
            gate.validate(stopped.relationship_id, stopped.follower_wallet,
                          stopped.wallet, stopped.snapshot_hash)
        except ValueError as exc:
            emergency_stop_rejected = str(exc) == "relationship is disabled or unavailable"
        if not emergency_stop_rejected:
            raise AssertionError("disabled relationship passed the fresh signing gate")

        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder) / "relationship-isolation.sqlite3")
            try:
                store.start_paper_budget_cycle("mysql-isolation", "validation")
                for policy in selected:
                    for bucket, limit in policy.budget_limits.items():
                        store.configure_paper_budget(policy.ledger_scope, bucket, limit)
                budgets = {
                    policy.follower_wallet: store.paper_budget(policy.ledger_scope, "USDG")
                    for policy in selected
                }
            finally:
                store.close()
        print(json.dumps({
            "event": "mysql_relationship_isolation_validated",
            "relationships": len(selected),
            "followers": sorted(budgets),
            "strategy_versions": sorted(policy.strategy_version for policy in selected),
            "slippage_bps": sorted(policy.quote_policy.max_slippage_bps
                                   for policy in selected),
            "independent_scopes": len({policy.ledger_scope for policy in selected}) == 2,
            "budgets_configured": all(value is not None for value in budgets.values()),
            "emergency_stop_rejected": emergency_stop_rejected,
            "copy_eligible": False,
            "live_trading": False,
        }, sort_keys=True))
    finally:
        try:
            if inserted_ids:
                with connection.cursor() as cursor:
                    placeholders = ",".join(["%s"] * len(inserted_ids))
                    cursor.execute(
                        f"DELETE FROM copy_relationships WHERE id IN ({placeholders})",
                        inserted_ids,
                    )
                connection.commit()
                with connection.cursor() as cursor:
                    placeholders = ",".join(["%s"] * len(inserted_ids))
                    cursor.execute(
                        f"SELECT COUNT(*) AS count FROM copy_relationships "
                        f"WHERE id IN ({placeholders})",
                        inserted_ids,
                    )
                    remaining = int(cursor.fetchone()["count"])
                if remaining:
                    raise RuntimeError("validation relationship cleanup failed")
                print(json.dumps({
                    "event": "mysql_relationship_isolation_cleanup",
                    "removed": len(inserted_ids), "remaining": remaining,
                }, sort_keys=True))
        finally:
            connection.close()


if __name__ == "__main__":
    main()
