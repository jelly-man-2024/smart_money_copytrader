"""Read-only shadow snapshots. Never instantiate a runtime Store here."""
from __future__ import annotations

from dataclasses import asdict
from datetime import timezone
import json
import os
from pathlib import Path
import time

from .early_shadow import snapshot
from .mysql_config import mysql_connection, rows_to_document
from .paper_config import parse_paper_config


def obj(value):
    return json.loads(value) if isinstance(value, str) else value


def epoch(value):
    return value.replace(tzinfo=timezone.utc).timestamp()


class BoundedReadCursor:
    def __init__(self, cursor):
        self.cursor, self.deadline = cursor, time.monotonic() + 2

    def execute(self, sql, params=None):
        if not sql.lstrip().upper().startswith("SELECT "):
            raise PermissionError("shadow_query_must_be_select")
        if time.monotonic() > self.deadline:
            raise TimeoutError("shadow_database_snapshot_timeout")
        return self.cursor.execute(sql, params)

    def fetchall(self):
        return self.cursor.fetchall()


class ShadowBusinessReader:
    def __init__(self, connect=None):
        self.connect = connect or (lambda: mysql_connection(read_timeout=2))

    def read(self, callback):
        connection = self.connect()
        try:
            with connection.cursor() as cursor:
                cursor.execute("START TRANSACTION READ ONLY")
                # A consistent snapshot, not locks, reservations or writes.
                return callback(BoundedReadCursor(cursor))
        finally:
            try:
                connection.rollback()
            finally:
                connection.close()

    def wallets(self):
        def query(q):
            q.execute("SELECT DISTINCT smart_wallet FROM copy_relationships WHERE enabled=TRUE LIMIT 257")
            rows = q.fetchall()
            if not rows or len(rows) > 256:
                raise ValueError("invalid_shadow_watchlist_size")
            from .models import address
            return sorted({address(r["smart_wallet"]) for r in rows})
        return self.read(query)

    def truth(self, tx_hash, wallet):
        def query(q):
            q.execute("SELECT payload FROM signals WHERE tx_hash=%s LIMIT 257", (tx_hash,))
            rows = q.fetchall()
            if len(rows) > 256:
                raise ValueError("strict_evidence_limit")
            return [s for row in rows if (s := obj(row["payload"])).get("wallet") == wallet]
        return self.read(query)

    def capture(self, candidate):
        def query(q):
            started = time.time()
            q.execute("SELECT * FROM copy_relationships WHERE smart_wallet=%s AND enabled=TRUE ORDER BY id LIMIT 33",
                      (candidate.wallet,))
            rows = q.fetchall()
            if len(rows) > 32:
                raise ValueError("relationship_snapshot_limit")
            result = []
            for row in rows:
                document = rows_to_document([row])
                policy = parse_paper_config(document).relationships[0]
                binding = {"relationship_id": policy.relationship_id,
                           "follower": policy.follower_wallet, "smart_wallet": policy.wallet,
                           "config_snapshot_hash": policy.snapshot_hash}
                q.execute("""SELECT b.* FROM paper_budgets b JOIN paper_budget_cycles c
                    ON b.cycle_id=c.cycle_id WHERE c.status='active' AND b.wallet=%s
                    AND b.bucket='USDG' LIMIT 2""", (policy.ledger_scope,))
                budgets = q.fetchall()
                q.execute("""SELECT p.* FROM paper_positions p WHERE p.wallet=%s
                    AND p.token=%s AND p.status='open' ORDER BY p.created_at,p.lot_id LIMIT 1001""",
                          (policy.ledger_scope, candidate.token_in))
                positions = q.fetchall()
                if len(positions) > 1000:
                    raise ValueError("position_snapshot_limit")
                lots = []
                for p in positions:
                    q.execute("""SELECT token_amount_raw FROM paper_position_reservations
                        WHERE lot_id=%s AND status='active' LIMIT 1001""", (p["lot_id"],))
                    reservations = q.fetchall()
                    if len(reservations) > 1000:
                        raise ValueError("reservation_snapshot_limit")
                    attr = obj(p["attribution_payload"])
                    source = attr.get("source_position_remaining_raw")
                    if source is None and p["token_initial_raw"] == p["token_remaining_raw"]:
                        source = attr.get("source_amount_out_raw")
                    lots.append({"lot_id": p["lot_id"], "token": p["token"],
                                 "relationship_id": policy.relationship_id,
                                 "principal_asset": p["principal_asset"],
                                 "token_remaining_raw": str(p["token_remaining_raw"]),
                                 "reserved_raw": str(sum(int(r["token_amount_raw"]) for r in reservations)),
                                 "source_remaining_raw": str(source) if source is not None else None,
                                 "created_at": epoch(p["created_at"])})
                # A bounded complete read is required. Truncation is an error,
                # never evidence that an already-consumed operation is absent.
                q.execute("""SELECT p.source_tx_hash,s.payload FROM paper_proposals p
                    LEFT JOIN signals s ON s.event_id=JSON_UNQUOTE(
                        JSON_EXTRACT(p.attribution_payload,'$.source_event_id'))
                    WHERE p.wallet=%s AND p.status IN ('reserved','filled') LIMIT 1001""",
                          (policy.ledger_scope,))
                proposals = q.fetchall()
                if len(proposals) > 1000:
                    raise ValueError("dedup_snapshot_limit")
                consumed = False
                for p in proposals:
                    sig = obj(p["payload"]) or {}
                    evidence = sig.get("evidence", {})
                    order = evidence.get("relay_order_id", evidence.get("relay_deposit_order_id"))
                    if p["source_tx_hash"] == candidate.tx_hash or order == candidate.order_id:
                        consumed = True
                q.execute("SELECT payload FROM signals WHERE tx_hash=%s LIMIT 257", (candidate.tx_hash,))
                signals = q.fetchall()
                if len(signals) > 256:
                    raise ValueError("canonical_snapshot_limit")
                orphaned = any(obj(s["payload"]).get("canonical_status") == "orphaned" for s in signals)
                budget = budgets[0] if len(budgets) == 1 else None
                portfolio = {**binding, "lots": lots, "source_orphaned": orphaned,
                             "consumed_operation_keys": [candidate.relationship_key(
                                 policy.relationship_id, policy.follower_wallet)] if consumed else []}
                if budget is not None:
                    available = int(budget["limit_raw"]) - int(budget["invested_raw"]) - int(budget["reserved_raw"])
                    if available < 0:
                        raise ValueError("negative_budget_available")
                    portfolio["budget_available_raw"] = str(available)
                cap = (int(policy.budget_limits["USDG"]) if candidate.side == "BUY" else
                       sum(int(l["token_remaining_raw"]) for l in lots))
                config = {**binding, "enabled": bool(row["enabled"]),
                          "stop_active": (os.environ.get("SMART_MONEY_EMERGENCY_STOP", "1") != "0"
                                          or Path(os.environ.get("SMART_MONEY_EMERGENCY_STOP_FILE",
                                                                 "var/EXECUTION_STOP")).exists()),
                          "stop_state_scope": "shadow_process_environment_and_shared_stop_file",
                          "buy_rule": asdict(policy.buy_rules["USDG"]),
                          "sell_rule": asdict(policy.sell_rule),
                          "max_input_raw": str(cap),
                          "cap_basis": "configured_cycle_budget_or_attributed_inventory",
                          "allowed_protocols": sorted(policy.allowed_protocols),
                          "allowed_assets": sorted(policy.allowed_assets),
                          "execution_providers": list(policy.execution_providers),
                          "quote_policy": asdict(policy.quote_policy),
                          "configuration_document": document}
                observed = time.time()
                # Slow snapshot reads must not look freshly captured just
                # because the final query finished recently.
                snaps = {name: {**snapshot(payload, "business_mysql_read_only", observed),
                                "capture_started_at": started}
                         for name, payload in (("policy", config), ("portfolio", portfolio))}
                result.append({"relationship_id": policy.relationship_id, "snapshots": snaps})
            return {"relationships": result}
        return self.read(query)
