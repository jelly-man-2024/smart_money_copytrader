from __future__ import annotations

import json
import hashlib
from pathlib import Path
import sqlite3
import time

from . import registry as R
from .models import Signal, Transaction, address

STAGE_RANK = {"intent": 0, "execution_observed": 1, "needs_review": 1,
              "swap_evidenced": 2, "relay_sell_evidenced": 2,
              "relay_buy_evidenced": 2, "failed": 3}
MAX_CANDIDATE_ATTEMPTS = 8


RELEASED_NONCE_SENTINEL_BASE = 1 << 62


def released_nonce_sentinel(reservation_id: str) -> int:
    """Non-colliding placeholder nonce for a released, never-signed reservation.

    Released rows must keep existing (execution plans reference them) yet must not
    occupy the real nonce in the unique (wallet, chain, nonce) key, because the
    network still expects that nonce next. The sentinel sits far above any real
    account nonce and within BIGINT UNSIGNED range.
    """
    digest = hashlib.sha256(f"released:{reservation_id}".encode()).digest()
    return RELEASED_NONCE_SENTINEL_BASE + int.from_bytes(digest[:7], "big")


def _execution_plan_integrity(plan_id: str, proposal_id: str, follower_wallet: str,
                              relationship_id: str, config_snapshot_hash: str,
                              nonce_reservation_id: str, plan_payload: str,
                              preflight_payload: str) -> str:
    payload = json.dumps({
        "plan_id": plan_id, "proposal_id": proposal_id,
        "follower_wallet": follower_wallet.lower(),
        "relationship_id": relationship_id,
        "config_snapshot_hash": config_snapshot_hash,
        "nonce_reservation_id": nonce_reservation_id,
        "plan_payload": json.loads(plan_payload),
        "preflight_payload": json.loads(preflight_payload),
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _paper_budget_bucket(asset: str) -> str | None:
    asset = asset.lower()
    if asset == R.USDG:
        return "USDG"
    if asset in {R.NATIVE, R.WETH}:
        return "ETH_WETH"
    return None


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.integrity_error = sqlite3.IntegrityError
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS signals (
            event_id TEXT PRIMARY KEY, tx_hash TEXT NOT NULL, stage_rank INTEGER NOT NULL,
            payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS candidates (
            tx_hash TEXT PRIMARY KEY, payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','queued','retry','complete','failed')),
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS chain_cursors (
            name TEXT PRIMARY KEY, block_number INTEGER NOT NULL, block_hash TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS canonical_blocks (
            block_number INTEGER PRIMARY KEY, block_hash TEXT NOT NULL, parent_hash TEXT NOT NULL)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS candidate_inclusions (
            tx_hash TEXT PRIMARY KEY, block_number INTEGER NOT NULL, block_hash TEXT NOT NULL)""")
        self.connection.execute("""CREATE INDEX IF NOT EXISTS candidate_inclusions_by_block
            ON candidate_inclusions(block_number,block_hash)""")
        self.connection.execute("CREATE INDEX IF NOT EXISTS signals_by_tx_hash ON signals(tx_hash)")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS solver_order_evidence (
            evidence_id TEXT PRIMARY KEY, order_id TEXT NOT NULL, kind TEXT NOT NULL
                CHECK(kind IN ('source_deposit','destination_delivery')),
            wallet TEXT NOT NULL, tx_hash TEXT NOT NULL, payload TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.connection.execute("""CREATE INDEX IF NOT EXISTS solver_order_by_order_id
            ON solver_order_evidence(order_id,kind)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_budget_cycles (
            cycle_id TEXT PRIMARY KEY, status TEXT NOT NULL CHECK(status IN ('active','closed')),
            reason TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            closed_at TEXT)""")
        self.connection.execute("""CREATE UNIQUE INDEX IF NOT EXISTS one_active_paper_budget_cycle
            ON paper_budget_cycles(status) WHERE status='active'""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_budgets (
            cycle_id TEXT NOT NULL, wallet TEXT NOT NULL,
            bucket TEXT NOT NULL CHECK(bucket IN ('USDG','ETH_WETH')),
            limit_raw TEXT NOT NULL, reserved_raw TEXT NOT NULL DEFAULT '0',
            invested_raw TEXT NOT NULL DEFAULT '0',
            PRIMARY KEY(cycle_id,wallet,bucket),
            FOREIGN KEY(cycle_id) REFERENCES paper_budget_cycles(cycle_id))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_proposals (
            proposal_id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL,
            source_tx_hash TEXT NOT NULL, wallet TEXT NOT NULL, trigger_mode TEXT NOT NULL,
            strategy_version TEXT NOT NULL, input_asset TEXT NOT NULL,
            output_asset TEXT NOT NULL, budget_bucket TEXT NOT NULL,
            amount_in_raw TEXT NOT NULL, status TEXT NOT NULL
                CHECK(status IN ('reserved','rejected','cancelled','filled')),
            quote_payload TEXT, attribution_payload TEXT NOT NULL,
            rejection_reason TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_event_id,trigger_mode,strategy_version))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_reservations (
            proposal_id TEXT PRIMARY KEY, cycle_id TEXT NOT NULL, wallet TEXT NOT NULL,
            bucket TEXT NOT NULL, amount_raw TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','released','consumed')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(proposal_id) REFERENCES paper_proposals(proposal_id))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_orders (
            order_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
            side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
            status TEXT NOT NULL CHECK(status IN ('filled','cancelled')),
            payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(proposal_id) REFERENCES paper_proposals(proposal_id))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_fills (
            fill_id TEXT PRIMARY KEY, order_id TEXT NOT NULL,
            input_asset TEXT NOT NULL, output_asset TEXT NOT NULL,
            amount_in_raw TEXT NOT NULL, amount_out_raw TEXT NOT NULL,
            fee_asset TEXT, fee_amount_raw TEXT,
            gas_cost_wei TEXT NOT NULL DEFAULT '0',
            quote_observed_at TEXT NOT NULL, filled_at TEXT NOT NULL,
            attribution_payload TEXT NOT NULL,
            FOREIGN KEY(order_id) REFERENCES paper_orders(order_id))""")
        if "gas_cost_wei" not in {
                row[1] for row in self.connection.execute("PRAGMA table_info(paper_fills)")}:
            self.connection.execute(
                "ALTER TABLE paper_fills ADD COLUMN gas_cost_wei TEXT NOT NULL DEFAULT '0'")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_positions (
            lot_id TEXT PRIMARY KEY, wallet TEXT NOT NULL, token TEXT NOT NULL,
            budget_cycle_id TEXT NOT NULL, budget_bucket TEXT NOT NULL,
            principal_asset TEXT NOT NULL,
            principal_initial_raw TEXT NOT NULL, principal_remaining_raw TEXT NOT NULL,
            token_initial_raw TEXT NOT NULL, token_remaining_raw TEXT NOT NULL,
            source_event_id TEXT NOT NULL, buy_fill_id TEXT NOT NULL UNIQUE,
            attribution_payload TEXT NOT NULL, status TEXT NOT NULL
                CHECK(status IN ('open','closed')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP, updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(buy_fill_id) REFERENCES paper_fills(fill_id))""")
        if "principal_asset" not in {
                row[1] for row in self.connection.execute("PRAGMA table_info(paper_positions)")}:
            self.connection.execute(
                "ALTER TABLE paper_positions ADD COLUMN principal_asset TEXT NOT NULL DEFAULT ''")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_position_reservations (
            proposal_id TEXT NOT NULL, lot_id TEXT NOT NULL, token_amount_raw TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('active','consumed','released')),
            PRIMARY KEY(proposal_id,lot_id),
            FOREIGN KEY(proposal_id) REFERENCES paper_proposals(proposal_id),
            FOREIGN KEY(lot_id) REFERENCES paper_positions(lot_id))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_realized_pnl (
            fill_id TEXT NOT NULL, lot_id TEXT NOT NULL,
            principal_asset TEXT NOT NULL, principal_released_raw TEXT NOT NULL,
            proceeds_raw TEXT NOT NULL, fee_in_principal_asset_raw TEXT NOT NULL,
            realized_pnl_raw TEXT NOT NULL, gas_cost_wei TEXT NOT NULL,
            PRIMARY KEY(fill_id,lot_id),
            FOREIGN KEY(fill_id) REFERENCES paper_fills(fill_id),
            FOREIGN KEY(lot_id) REFERENCES paper_positions(lot_id))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_position_marks (
            mark_id TEXT PRIMARY KEY, lot_id TEXT NOT NULL, principal_asset TEXT NOT NULL,
            token_amount_raw TEXT NOT NULL, gross_value_raw TEXT NOT NULL,
            principal_remaining_raw TEXT NOT NULL, unrealized_pnl_raw TEXT NOT NULL,
            gas_cost_wei TEXT NOT NULL, block_number INTEGER NOT NULL,
            block_hash TEXT NOT NULL, quote_source TEXT NOT NULL,
            quote_observed_at TEXT NOT NULL, risk_payload TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(lot_id,block_hash,token_amount_raw),
            FOREIGN KEY(lot_id) REFERENCES paper_positions(lot_id))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS paper_decisions (
            decision_id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL,
            trigger_mode TEXT NOT NULL, strategy_version TEXT NOT NULL,
            accepted INTEGER NOT NULL CHECK(accepted IN (0,1)), reason TEXT,
            payload TEXT NOT NULL, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(source_event_id,trigger_mode,strategy_version))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS execution_nonce_reservations (
            reservation_id TEXT PRIMARY KEY, follower_wallet TEXT NOT NULL,
            relationship_id TEXT NOT NULL, proposal_id TEXT NOT NULL UNIQUE,
            chain_id INTEGER NOT NULL, nonce INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('reserved','signed','broadcast','confirmed','released')),
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(follower_wallet,chain_id,nonce))""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS execution_plans (
            plan_id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL UNIQUE,
            follower_wallet TEXT NOT NULL, relationship_id TEXT NOT NULL,
            config_snapshot_hash TEXT NOT NULL, nonce_reservation_id TEXT NOT NULL UNIQUE,
            status TEXT NOT NULL CHECK(status IN ('prepared','signed','cancelled')),
            plan_payload TEXT NOT NULL, preflight_payload TEXT NOT NULL,
            signed_tx_hash TEXT, final_review_payload TEXT, plan_integrity_hash TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(proposal_id) REFERENCES paper_proposals(proposal_id),
            FOREIGN KEY(nonce_reservation_id)
              REFERENCES execution_nonce_reservations(reservation_id))""")
        if "signed_tx_hash" not in {
                row[1] for row in self.connection.execute("PRAGMA table_info(execution_plans)")}:
            self.connection.execute("ALTER TABLE execution_plans ADD COLUMN signed_tx_hash TEXT")
        if "final_review_payload" not in {
                row[1] for row in self.connection.execute("PRAGMA table_info(execution_plans)")}:
            self.connection.execute(
                "ALTER TABLE execution_plans ADD COLUMN final_review_payload TEXT")
        if "plan_integrity_hash" not in {
                row[1] for row in self.connection.execute("PRAGMA table_info(execution_plans)")}:
            self.connection.execute(
                "ALTER TABLE execution_plans ADD COLUMN plan_integrity_hash TEXT")
        for row in self.connection.execute("""SELECT plan_id,proposal_id,follower_wallet,
                relationship_id,config_snapshot_hash,nonce_reservation_id,plan_payload,
                preflight_payload FROM execution_plans WHERE plan_integrity_hash IS NULL""").fetchall():
            integrity = _execution_plan_integrity(*row)
            self.connection.execute(
                "UPDATE execution_plans SET plan_integrity_hash=? WHERE plan_id=?",
                (integrity, row[0]))
        self.connection.execute("""CREATE TABLE IF NOT EXISTS execution_attempts (
            tx_hash TEXT PRIMARY KEY, plan_id TEXT NOT NULL,
            replaces_tx_hash TEXT, nonce INTEGER NOT NULL,
            status TEXT NOT NULL CHECK(status IN
                ('signed','observed_pending','confirmed','reverted','replaced','orphaned')),
            public_payload TEXT NOT NULL, block_number INTEGER, block_hash TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(plan_id) REFERENCES execution_plans(plan_id),
            FOREIGN KEY(replaces_tx_hash) REFERENCES execution_attempts(tx_hash))""")
        self.connection.execute("""CREATE INDEX IF NOT EXISTS execution_attempts_by_plan
            ON execution_attempts(plan_id,created_at)""")
        self.connection.commit()

    def reserve_execution_nonce(self, reservation_id: str, follower_wallet: str,
                                relationship_id: str, proposal_id: str,
                                chain_id: int, pending_nonce: int) -> tuple[int, str]:
        """Persistently reserve the first free nonce at/above the RPC pending nonce."""
        if (not reservation_id or not relationship_id or not proposal_id
                or not isinstance(chain_id, int) or chain_id <= 0
                or not isinstance(pending_nonce, int) or pending_nonce < 0):
            raise ValueError("invalid nonce reservation")
        follower_wallet = follower_wallet.lower()
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute("""SELECT nonce,status
                FROM execution_nonce_reservations WHERE proposal_id=?""",
                (proposal_id,)).fetchone()
            if existing:
                self.connection.rollback()
                return existing[0], existing[1]
            used = {row[0] for row in self.connection.execute("""SELECT nonce
                FROM execution_nonce_reservations
                WHERE follower_wallet=? AND chain_id=?
                  AND status IN ('reserved','signed','broadcast') AND nonce>=?""",
                (follower_wallet, chain_id, pending_nonce))}
            nonce = pending_nonce
            while nonce in used:
                nonce += 1
            self.connection.execute("""INSERT INTO execution_nonce_reservations(
                reservation_id,follower_wallet,relationship_id,proposal_id,
                chain_id,nonce,status) VALUES(?,?,?,?,?,?,'reserved')""", (
                    reservation_id, follower_wallet, relationship_id,
                    proposal_id, chain_id, nonce,
                ))
            self.connection.commit()
            return nonce, "reserved"
        except Exception:
            self.connection.rollback()
            raise

    def update_execution_nonce_status(self, reservation_id: str,
                                      expected: str, status: str) -> bool:
        transitions = {
            "reserved": {"signed", "released"},
            "signed": {"broadcast", "released"},
            "broadcast": {"confirmed", "released"},
        }
        if status not in transitions.get(expected, set()):
            raise ValueError("invalid nonce reservation transition")
        cursor = self.connection.execute("""UPDATE execution_nonce_reservations
            SET status=?,updated_at=CURRENT_TIMESTAMP
            WHERE reservation_id=? AND status=?""", (status, reservation_id, expected))
        self.connection.commit()
        return cursor.rowcount == 1

    def execution_nonce_reservation(self, proposal_id: str) -> dict | None:
        row = self.connection.execute("""SELECT reservation_id,follower_wallet,
            relationship_id,chain_id,nonce,status,created_at,updated_at
            FROM execution_nonce_reservations WHERE proposal_id=?""",
            (proposal_id,)).fetchone()
        if row is None:
            return None
        names = ("reservation_id", "follower_wallet", "relationship_id",
                 "chain_id", "nonce", "status", "created_at", "updated_at")
        return {"proposal_id": proposal_id, **dict(zip(names, row))}

    def record_execution_plan(self, plan: dict, preflight: dict) -> bool:
        required = {"plan_id", "proposal_id", "follower_wallet", "relationship_id",
                    "config_snapshot_hash", "nonce_reservation_id", "transaction",
                    "unsigned_plan"}
        if set(plan) != required or not isinstance(preflight, dict):
            raise ValueError("invalid execution plan record")
        plan_payload = json.dumps({"transaction": plan["transaction"],
                                   "unsigned_plan": plan["unsigned_plan"]}, sort_keys=True)
        preflight_payload = json.dumps(preflight, sort_keys=True)
        integrity = _execution_plan_integrity(
            plan["plan_id"], plan["proposal_id"], plan["follower_wallet"],
            plan["relationship_id"], plan["config_snapshot_hash"],
            plan["nonce_reservation_id"], plan_payload, preflight_payload)
        cursor = self.connection.execute("""INSERT OR IGNORE INTO execution_plans(
            plan_id,proposal_id,follower_wallet,relationship_id,config_snapshot_hash,
            nonce_reservation_id,status,plan_payload,preflight_payload,plan_integrity_hash)
            VALUES(?,?,?,?,?,?,'prepared',?,?,?)""", (
                plan["plan_id"], plan["proposal_id"], plan["follower_wallet"].lower(),
                plan["relationship_id"], plan["config_snapshot_hash"],
                plan["nonce_reservation_id"], plan_payload, preflight_payload, integrity,
            ))
        self.connection.commit()
        return cursor.rowcount == 1

    def execution_plan(self, proposal_id: str) -> dict | None:
        row = self.connection.execute("""SELECT plan_id,follower_wallet,relationship_id,
            config_snapshot_hash,nonce_reservation_id,status,plan_payload,
            preflight_payload,signed_tx_hash,final_review_payload,plan_integrity_hash,
            created_at,updated_at
            FROM execution_plans
            WHERE proposal_id=?""", (proposal_id,)).fetchone()
        if row is None:
            return None
        names = ("plan_id", "follower_wallet", "relationship_id",
                 "config_snapshot_hash", "nonce_reservation_id", "status",
                 "plan_payload", "preflight", "signed_tx_hash", "final_review",
                 "plan_integrity_hash", "created_at", "updated_at")
        result = {"proposal_id": proposal_id, **dict(zip(names, row))}
        expected_integrity = _execution_plan_integrity(
            result["plan_id"], proposal_id, result["follower_wallet"],
            result["relationship_id"], result["config_snapshot_hash"],
            result["nonce_reservation_id"], result["plan_payload"], result["preflight"])
        if result["plan_integrity_hash"] != expected_integrity:
            raise ValueError("execution plan integrity mismatch")
        payload = json.loads(result.pop("plan_payload"))
        if "transaction" in payload:
            result.update(payload)
        else:
            result["transaction"], result["unsigned_plan"] = payload, None
        result["preflight"] = json.loads(result["preflight"])
        result["final_review"] = (json.loads(result["final_review"])
                                  if result["final_review"] else None)
        return result

    def mark_execution_plan_signed(self, plan_id: str, nonce_reservation_id: str,
                                   signed_tx_hash: str, final_review: dict) -> bool:
        if (not isinstance(signed_tx_hash, str) or len(signed_tx_hash) != 66
                or not signed_tx_hash.startswith("0x")):
            raise ValueError("invalid signed transaction hash")
        try:
            final_review_payload = json.dumps(final_review, sort_keys=True)
        except (TypeError, ValueError):
            raise ValueError("invalid final execution review") from None
        lowered = final_review_payload.lower()
        if (not isinstance(final_review, dict) or len(final_review_payload) > 1024 * 1024
                or any(name in lowered for name in (
                    "private_key", "privatekey", "raw_transaction", "rawtransaction"))):
            raise ValueError("invalid final execution review")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            plan = self.connection.execute("""SELECT status,nonce_reservation_id
                FROM execution_plans WHERE plan_id=?""", (plan_id,)).fetchone()
            nonce = self.connection.execute("""SELECT status FROM execution_nonce_reservations
                WHERE reservation_id=?""", (nonce_reservation_id,)).fetchone()
            if (plan is None or nonce is None or plan[1] != nonce_reservation_id
                    or plan[0] != "prepared" or nonce[0] != "reserved"):
                self.connection.rollback()
                return False
            self.connection.execute("""UPDATE execution_plans SET status='signed',
                signed_tx_hash=?,final_review_payload=?,updated_at=CURRENT_TIMESTAMP
                WHERE plan_id=?""",
                (signed_tx_hash.lower(), final_review_payload, plan_id))
            self.connection.execute("""UPDATE execution_nonce_reservations SET status='signed',
                updated_at=CURRENT_TIMESTAMP WHERE reservation_id=?""",
                (nonce_reservation_id,))
            transaction = self.connection.execute(
                "SELECT plan_payload FROM execution_plans WHERE plan_id=?", (plan_id,)
            ).fetchone()[0]
            payload = json.loads(transaction)
            transaction = payload.get("transaction", payload)
            self.connection.execute("""INSERT INTO execution_attempts(
                tx_hash,plan_id,nonce,status,public_payload)
                VALUES(?,?,?,'signed',?)""", (
                    signed_tx_hash.lower(), plan_id, int(transaction["nonce"]),
                    json.dumps(transaction, sort_keys=True),
                ))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def cancel_prepared_execution_plan(self, proposal_id: str, reason: str) -> bool:
        """Release a plan that was prepared but never signed, freeing its nonce.

        Only the ``prepared`` state is cancellable: nothing was signed, so no raw
        transaction can exist anywhere and the reserved nonce can safely return to
        the pool. Signed or broadcast plans keep the operator-review path.
        """
        if not reason:
            raise ValueError("cancellation reason is required")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            plan = self.connection.execute("""SELECT plan_id,status,nonce_reservation_id
                FROM execution_plans WHERE proposal_id=?""", (proposal_id,)).fetchone()
            if plan is None or plan[1] != "prepared":
                self.connection.rollback()
                return False
            plan_id, _, reservation_id = plan
            nonce = self.connection.execute("""SELECT status FROM execution_nonce_reservations
                WHERE reservation_id=?""", (reservation_id,)).fetchone()
            attempts = self.connection.execute(
                "SELECT COUNT(*) FROM execution_attempts WHERE plan_id=?",
                (plan_id,)).fetchone()[0]
            if nonce is None or nonce[0] != "reserved" or int(attempts) != 0:
                self.connection.rollback()
                return False
            released_nonce = int(self.connection.execute(
                "SELECT nonce FROM execution_nonce_reservations WHERE reservation_id=?",
                (reservation_id,)).fetchone()[0])
            self.connection.execute("""UPDATE execution_plans SET status='cancelled',
                final_review_payload=?,updated_at=CURRENT_TIMESTAMP WHERE plan_id=?""",
                (json.dumps({"cancelled": True, "reason": reason[:300],
                             "signed": False, "broadcast_performed": False,
                             "released_nonce": released_nonce,
                             "released_reservation_id": reservation_id},
                            sort_keys=True), plan_id))
            # The plan keeps its foreign key to this row, so the row stays; its nonce
            # moves to a non-colliding sentinel because the network still expects the
            # original nonce next and the unique (wallet, chain, nonce) key must let
            # the following plan reserve it again.
            self.connection.execute("""UPDATE execution_nonce_reservations
                SET status='released',nonce=?,updated_at=CURRENT_TIMESTAMP
                WHERE reservation_id=? AND status='reserved'""",
                (released_nonce_sentinel(reservation_id), reservation_id))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def cancel_unbroadcast_signed_execution_plan(self, proposal_id: str,
                                                 reason: str) -> bool:
        """Release a signed plan whose bytes were never handed to a broadcaster.

        Callers must only use this when they know no broadcast was attempted: the
        pre-broadcast reviewer rejected the in-memory bytes, which are then dropped.
        The plan keeps its signed hash in ``final_review`` for history, its lone
        never-observed ``signed`` attempt row is removed, and the nonce returns to
        the pool exactly as for a prepared plan.
        """
        if not reason:
            raise ValueError("cancellation reason is required")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            plan = self.connection.execute("""SELECT plan_id,status,nonce_reservation_id,
                signed_tx_hash,final_review_payload FROM execution_plans
                WHERE proposal_id=?""", (proposal_id,)).fetchone()
            if plan is None or plan[1] != "signed" or not plan[3]:
                self.connection.rollback()
                return False
            plan_id, _, reservation_id, signed_hash, review_payload = plan
            nonce_row = self.connection.execute("""SELECT status,nonce
                FROM execution_nonce_reservations WHERE reservation_id=?""",
                (reservation_id,)).fetchone()
            attempts = self.connection.execute("""SELECT tx_hash,status,replaces_tx_hash
                FROM execution_attempts WHERE plan_id=?""", (plan_id,)).fetchall()
            if (nonce_row is None or nonce_row[0] != "signed" or len(attempts) != 1
                    or attempts[0][0] != signed_hash or attempts[0][1] != "signed"
                    or attempts[0][2] is not None):
                self.connection.rollback()
                return False
            released_nonce = int(nonce_row[1])
            try:
                signing_review = json.loads(review_payload) if review_payload else None
            except (TypeError, ValueError):
                signing_review = None
            self.connection.execute(
                "DELETE FROM execution_attempts WHERE plan_id=? AND tx_hash=? AND status='signed'",
                (plan_id, signed_hash))
            self.connection.execute("""UPDATE execution_plans SET status='cancelled',
                final_review_payload=?,updated_at=CURRENT_TIMESTAMP WHERE plan_id=?""",
                (json.dumps({"cancelled": True, "reason": reason[:300], "signed": True,
                             "broadcast_performed": False,
                             "signed_tx_hash_never_broadcast": signed_hash,
                             "released_nonce": released_nonce,
                             "released_reservation_id": reservation_id,
                             "signing_review": signing_review}, sort_keys=True), plan_id))
            self.connection.execute("""UPDATE execution_nonce_reservations
                SET status='released',nonce=?,updated_at=CURRENT_TIMESTAMP
                WHERE reservation_id=? AND status='signed'""",
                (released_nonce_sentinel(reservation_id), reservation_id))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def execution_attempts(self, plan_id: str) -> list[dict]:
        names = ("tx_hash", "replaces_tx_hash", "nonce", "status", "public_payload",
                 "block_number", "block_hash", "created_at", "updated_at")
        result = []
        for row in self.connection.execute("""SELECT tx_hash,replaces_tx_hash,nonce,status,
                public_payload,block_number,block_hash,created_at,updated_at
                FROM execution_attempts WHERE plan_id=? ORDER BY created_at,tx_hash""",
                (plan_id,)):
            item = dict(zip(names, row))
            item["public_payload"] = json.loads(item["public_payload"])
            result.append(item)
        return result

    def execution_audit(self) -> dict:
        """Read-only consistency audit for restart/operator review."""
        proposal_ids = [row[0] for row in self.connection.execute(
            "SELECT proposal_id FROM execution_plans ORDER BY created_at,plan_id")]
        counts = {"prepared": 0, "signed": 0, "cancelled": 0, "attempts": 0}
        attempt_statuses = {
            "signed": 0, "observed_pending": 0, "confirmed": 0,
            "reverted": 0, "replaced": 0, "orphaned": 0,
        }
        issues = []
        for proposal_id in proposal_ids:
            try:
                plan = self.execution_plan(proposal_id)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                issues.append({"proposal_id": proposal_id,
                               "reason": "execution_plan_integrity_or_decode_error",
                               "error_type": type(exc).__name__})
                continue
            status = plan.get("status")
            if status not in {"prepared", "signed", "cancelled"}:
                issues.append({"proposal_id": proposal_id,
                               "reason": "execution_plan_status_unknown"})
                continue
            counts[status] += 1
            try:
                reservation = self.execution_nonce_reservation(proposal_id)
                attempts = self.execution_attempts(plan["plan_id"])
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                issues.append({"proposal_id": proposal_id,
                               "reason": "execution_state_decode_error",
                               "error_type": type(exc).__name__})
                continue
            counts["attempts"] += len(attempts)
            attempt_by_hash = {item.get("tx_hash"): item for item in attempts}
            for attempt in attempts:
                attempt_status = attempt.get("status")
                if attempt_status in attempt_statuses:
                    attempt_statuses[attempt_status] += 1
                else:
                    issues.append({"proposal_id": proposal_id,
                                   "reason": "execution_attempt_status_unknown"})
                payload = attempt.get("public_payload")
                try:
                    def numeric(value):
                        if isinstance(value, int) and not isinstance(value, bool):
                            return value
                        if isinstance(value, str):
                            return int(value, 16) if value.startswith("0x") else int(value)
                        raise ValueError

                    expected = plan["transaction"]
                    identity_matches = (
                        isinstance(payload, dict)
                        and isinstance(attempt.get("tx_hash"), str)
                        and len(attempt["tx_hash"]) == 66
                        and attempt["tx_hash"].startswith("0x")
                        and numeric(attempt.get("nonce")) == int(expected["nonce"])
                        and numeric(payload.get("nonce")) == int(expected["nonce"])
                        and numeric(payload.get("chainId")) == int(expected["chainId"])
                        and numeric(payload.get("type")) == int(expected["type"])
                        and numeric(payload.get("gas")) == int(expected["gas"])
                        and numeric(payload.get("value")) == int(expected["value"])
                        and str(payload.get("to", "")).lower()
                        == str(expected["to"]).lower()
                        and str(payload.get("data", "")).lower()
                        == str(expected["data"]).lower()
                        and ("from" not in payload or str(payload["from"]).lower()
                             == plan["follower_wallet"])
                    )
                except (KeyError, TypeError, ValueError):
                    identity_matches = False
                if not identity_matches:
                    issues.append({"proposal_id": proposal_id,
                                   "reason": "execution_attempt_identity_mismatch"})
                parent_hash = attempt.get("replaces_tx_hash")
                if attempt.get("tx_hash") == plan.get("signed_tx_hash"):
                    if parent_hash is not None:
                        issues.append({"proposal_id": proposal_id,
                                       "reason": "signed_attempt_has_replacement_parent"})
                elif not parent_hash or parent_hash not in attempt_by_hash:
                    issues.append({"proposal_id": proposal_id,
                                   "reason": "replacement_parent_missing"})
                elif attempt_by_hash[parent_hash].get("status") != "replaced":
                    issues.append({"proposal_id": proposal_id,
                                   "reason": "replacement_parent_not_replaced"})
                try:
                    fee_source = (expected if parent_hash is None
                                  else attempt_by_hash[parent_hash]["public_payload"])
                    fee_matches = (
                        numeric(payload.get("maxFeePerGas"))
                        >= numeric(fee_source.get("maxFeePerGas"))
                        and numeric(payload.get("maxPriorityFeePerGas"))
                        >= numeric(fee_source.get("maxPriorityFeePerGas"))
                    )
                    if parent_hash is None:
                        fee_matches = fee_matches and (
                            numeric(payload["maxFeePerGas"])
                            == numeric(fee_source["maxFeePerGas"])
                            and numeric(payload["maxPriorityFeePerGas"])
                            == numeric(fee_source["maxPriorityFeePerGas"]))
                    else:
                        fee_matches = fee_matches and (
                            numeric(payload["maxFeePerGas"])
                            > numeric(fee_source["maxFeePerGas"])
                            or numeric(payload["maxPriorityFeePerGas"])
                            > numeric(fee_source["maxPriorityFeePerGas"]))
                except (KeyError, TypeError, ValueError):
                    fee_matches = False
                if not fee_matches:
                    issues.append({"proposal_id": proposal_id,
                                   "reason": "execution_attempt_fee_chain_invalid"})
                if attempt_status in {"confirmed", "reverted", "orphaned"}:
                    block_hash = attempt.get("block_hash")
                    if (not isinstance(attempt.get("block_number"), int)
                            or attempt["block_number"] < 0
                            or not isinstance(block_hash, str)
                            or len(block_hash) != 66 or not block_hash.startswith("0x")):
                        issues.append({"proposal_id": proposal_id,
                                       "reason": "final_attempt_block_evidence_invalid"})
            if reservation is None:
                issues.append({"proposal_id": proposal_id,
                               "reason": "nonce_reservation_missing"})
                continue
            allowed_nonce_status = {
                "prepared": {"reserved"},
                "signed": {"signed", "broadcast", "confirmed"},
                "cancelled": {"released"},
            }[status]
            if reservation["status"] not in allowed_nonce_status:
                issues.append({"proposal_id": proposal_id,
                               "reason": "plan_nonce_status_mismatch"})
            planned_nonce = int(plan["transaction"]["nonce"])
            released_review = plan.get("final_review") or {}
            nonce_matches = reservation["nonce"] == planned_nonce or (
                status == "cancelled" and reservation["status"] == "released"
                and released_review.get("cancelled") is True
                and released_review.get("broadcast_performed") is False
                and released_review.get("released_nonce") == planned_nonce
                and reservation["nonce"] == released_nonce_sentinel(
                    reservation["reservation_id"]))
            if (reservation["relationship_id"] != plan["relationship_id"]
                    or reservation["follower_wallet"] != plan["follower_wallet"]
                    or not nonce_matches):
                issues.append({"proposal_id": proposal_id,
                               "reason": "plan_nonce_identity_mismatch"})
            if status == "prepared" and attempts:
                issues.append({"proposal_id": proposal_id,
                               "reason": "prepared_plan_has_attempts"})
            if status == "signed":
                matching = [item for item in attempts
                            if item["tx_hash"] == plan["signed_tx_hash"]]
                if not matching:
                    issues.append({"proposal_id": proposal_id,
                                   "reason": "signed_attempt_missing"})
                active = [item for item in attempts
                          if item["status"] in {"signed", "observed_pending"}]
                if len(active) > 1:
                    issues.append({"proposal_id": proposal_id,
                                   "reason": "multiple_active_attempts"})
        coverage = {
            "has_plans": bool(proposal_ids),
            "has_signed_attempts": attempt_statuses["signed"] > 0,
            "has_rpc_observed_attempts": any(attempt_statuses[name] > 0 for name in (
                "observed_pending", "confirmed", "reverted", "replaced", "orphaned")),
            "has_canonical_receipts": (
                attempt_statuses["confirmed"] + attempt_statuses["reverted"] > 0),
            "has_successful_confirmation": attempt_statuses["confirmed"] > 0,
        }
        return {
            "plans": len(proposal_ids), **counts, "issues": issues,
            "attempt_statuses": attempt_statuses, "coverage": coverage,
            "end_to_end_evidenced": coverage["has_successful_confirmation"],
            "healthy": not issues, "read_only": True,
            "copy_eligible": False, "live_trading": False,
        }

    def observe_execution_attempt(self, plan_id: str, tx_hash: str, payload: dict,
                                  replaces_tx_hash: str | None = None) -> bool:
        """Record an RPC-observed transaction; this method does not broadcast it."""
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            if replaces_tx_hash:
                old = self.connection.execute("""SELECT status FROM execution_attempts
                    WHERE tx_hash=? AND plan_id=?""", (replaces_tx_hash, plan_id)).fetchone()
                if old is None or old[0] not in {"signed", "observed_pending"}:
                    self.connection.rollback()
                    return False
                cursor = self.connection.execute("""INSERT OR IGNORE INTO execution_attempts(
                    tx_hash,plan_id,replaces_tx_hash,nonce,status,public_payload)
                    VALUES(?,?,?,?, 'observed_pending',?)""", (
                        tx_hash.lower(), plan_id, replaces_tx_hash.lower(),
                        int(payload["nonce"]), json.dumps(payload, sort_keys=True),
                    ))
                if cursor.rowcount != 1:
                    self.connection.rollback()
                    return False
                self.connection.execute("""UPDATE execution_attempts SET status='replaced',
                    updated_at=CURRENT_TIMESTAMP WHERE tx_hash=?""", (replaces_tx_hash,))
            else:
                cursor = self.connection.execute("""UPDATE execution_attempts
                    SET status='observed_pending',public_payload=?,updated_at=CURRENT_TIMESTAMP
                    WHERE tx_hash=? AND plan_id=? AND status IN ('signed','orphaned')""", (
                        json.dumps(payload, sort_keys=True), tx_hash.lower(), plan_id,
                    ))
            if cursor.rowcount:
                nonce_id = self.connection.execute("""SELECT nonce_reservation_id
                    FROM execution_plans WHERE plan_id=?""", (plan_id,)).fetchone()
                if nonce_id:
                    self.connection.execute("""UPDATE execution_nonce_reservations
                        SET status='broadcast',updated_at=CURRENT_TIMESTAMP
                        WHERE reservation_id=? AND status='signed'""", (nonce_id[0],))
            self.connection.commit()
            return cursor.rowcount == 1
        except Exception:
            self.connection.rollback()
            raise

    def finalize_execution_attempt(self, tx_hash: str, status: str,
                                   block_number: int, block_hash: str) -> bool:
        if status not in {"confirmed", "reverted", "orphaned"}:
            raise ValueError("invalid execution attempt final status")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute("""SELECT plan_id,status FROM execution_attempts
                WHERE tx_hash=?""", (tx_hash.lower(),)).fetchone()
            allowed_previous = ({"observed_pending", "confirmed", "reverted"}
                                if status == "orphaned"
                                else {"observed_pending", "orphaned"})
            if row is None or row[1] not in allowed_previous:
                self.connection.rollback()
                return False
            self.connection.execute("""UPDATE execution_attempts SET status=?,block_number=?,
                block_hash=?,updated_at=CURRENT_TIMESTAMP WHERE tx_hash=?""", (
                    status, block_number, block_hash.lower(), tx_hash.lower(),
                ))
            if status in {"confirmed", "reverted"}:
                nonce_id = self.connection.execute("""SELECT nonce_reservation_id
                    FROM execution_plans WHERE plan_id=?""", (row[0],)).fetchone()[0]
                self.connection.execute("""UPDATE execution_nonce_reservations
                    SET status='confirmed',updated_at=CURRENT_TIMESTAMP
                    WHERE reservation_id=? AND status='broadcast'""", (nonce_id,))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def start_paper_budget_cycle(self, cycle_id: str, reason: str) -> None:
        if not cycle_id or not reason:
            raise ValueError("cycle id and reason are required")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            active = self.connection.execute(
                "SELECT cycle_id FROM paper_budget_cycles WHERE status='active'").fetchone()
            if active and self.connection.execute("""SELECT 1 FROM paper_reservations
                    WHERE cycle_id=? AND status='active' LIMIT 1""", (active[0],)).fetchone():
                raise ValueError("cannot reset a budget cycle with active reservations")
            self.connection.execute("""UPDATE paper_budget_cycles
                SET status='closed',closed_at=CURRENT_TIMESTAMP WHERE status='active'""")
            self.connection.execute("""INSERT INTO paper_budget_cycles(cycle_id,status,reason)
                VALUES(?,'active',?)""", (cycle_id, reason))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def active_paper_budget_cycle(self) -> str | None:
        row = self.connection.execute(
            "SELECT cycle_id FROM paper_budget_cycles WHERE status='active'").fetchone()
        return row[0] if row else None

    def configure_paper_budget(self, wallet: str, bucket: str, limit_raw: str) -> None:
        cycle_id = self.active_paper_budget_cycle()
        if cycle_id is None:
            raise ValueError("no active paper budget cycle")
        wallet = wallet.lower()
        if bucket not in {"USDG", "ETH_WETH"} or not limit_raw.isdecimal() or int(limit_raw) <= 0:
            raise ValueError("invalid paper budget")
        old = self.connection.execute("""SELECT reserved_raw,invested_raw FROM paper_budgets
            WHERE cycle_id=? AND wallet=? AND bucket=?""",
            (cycle_id, wallet, bucket)).fetchone()
        if old and int(old[0]) + int(old[1]) > int(limit_raw):
            raise ValueError("new limit is below occupied budget")
        self.connection.execute("""INSERT INTO paper_budgets(
            cycle_id,wallet,bucket,limit_raw) VALUES(?,?,?,?)
            ON CONFLICT(cycle_id,wallet,bucket) DO UPDATE SET limit_raw=excluded.limit_raw""",
            (cycle_id, wallet, bucket, limit_raw))
        self.connection.commit()

    def reserve_paper_proposal(self, proposal: dict) -> tuple[bool, str]:
        """Atomically create an idempotent proposal and reserve its wallet budget."""
        required = {
            "proposal_id", "source_event_id", "source_tx_hash", "wallet", "trigger_mode",
            "strategy_version", "input_asset", "output_asset", "budget_bucket",
            "amount_in_raw", "attribution",
        }
        if set(proposal) not in (required, required | {"quote"}):
            raise ValueError("invalid proposal fields")
        amount = proposal["amount_in_raw"]
        if not isinstance(amount, str) or not amount.isdecimal() or int(amount) <= 0:
            raise ValueError("invalid proposal amount")
        if _paper_budget_bucket(proposal["input_asset"]) != proposal["budget_bucket"]:
            return False, "input_asset_budget_bucket_mismatch"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            active = self.connection.execute(
                "SELECT cycle_id FROM paper_budget_cycles WHERE status='active'").fetchone()
            if active is None:
                self.connection.rollback()
                return False, "no_active_budget_cycle"
            cycle_id = active[0]
            existing = self.connection.execute(
                "SELECT status FROM paper_proposals WHERE proposal_id=?",
                (proposal["proposal_id"],)).fetchone()
            if existing:
                self.connection.rollback()
                return existing[0] == "reserved", "proposal_already_exists"
            budget = self.connection.execute("""SELECT limit_raw,reserved_raw,invested_raw
                FROM paper_budgets WHERE cycle_id=? AND wallet=? AND bucket=?""",
                (cycle_id, proposal["wallet"].lower(), proposal["budget_bucket"])).fetchone()
            if budget is None:
                self.connection.rollback()
                return False, "budget_bucket_not_configured"
            available = int(budget[0]) - int(budget[1]) - int(budget[2])
            if int(amount) > available:
                self.connection.rollback()
                return False, "budget_limit_exceeded"
            self.connection.execute("""INSERT INTO paper_proposals(
                proposal_id,source_event_id,source_tx_hash,wallet,trigger_mode,
                strategy_version,input_asset,output_asset,budget_bucket,amount_in_raw,
                status,quote_payload,attribution_payload) VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?,?)""", (
                    proposal["proposal_id"], proposal["source_event_id"],
                    proposal["source_tx_hash"].lower(), proposal["wallet"].lower(),
                    proposal["trigger_mode"], proposal["strategy_version"],
                    proposal["input_asset"].lower(), proposal["output_asset"].lower(),
                    proposal["budget_bucket"], amount,
                    json.dumps(proposal.get("quote"), sort_keys=True)
                    if proposal.get("quote") is not None else None,
                    json.dumps(proposal["attribution"], sort_keys=True),
                ))
            self.connection.execute("""INSERT INTO paper_reservations(
                proposal_id,cycle_id,wallet,bucket,amount_raw,status)
                VALUES(?,?,?,?,?,'active')""", (
                    proposal["proposal_id"], cycle_id, proposal["wallet"].lower(),
                    proposal["budget_bucket"], amount,
                ))
            self.connection.execute("""UPDATE paper_budgets SET reserved_raw=?
                WHERE cycle_id=? AND wallet=? AND bucket=?""", (
                    str(int(budget[1]) + int(amount)), cycle_id,
                    proposal["wallet"].lower(), proposal["budget_bucket"],
                ))
            self.connection.commit()
            return True, "reserved"
        except self.integrity_error:
            self.connection.rollback()
            return False, "proposal_source_already_reserved"
        except Exception:
            self.connection.rollback()
            raise

    def paper_budget(self, wallet: str, bucket: str) -> dict | None:
        cycle_id = self.active_paper_budget_cycle()
        if cycle_id is None:
            return None
        row = self.connection.execute("""SELECT limit_raw,reserved_raw,invested_raw
            FROM paper_budgets WHERE cycle_id=? AND wallet=? AND bucket=?""",
            (cycle_id, wallet.lower(), bucket)).fetchone()
        if row is None:
            return None
        return {"cycle_id": cycle_id, "limit_raw": row[0], "reserved_raw": row[1],
                "invested_raw": row[2],
                "available_raw": str(int(row[0]) - int(row[1]) - int(row[2]))}

    def paper_proposal(self, proposal_id: str) -> dict | None:
        row = self.connection.execute("""SELECT source_event_id,source_tx_hash,wallet,
            trigger_mode,strategy_version,input_asset,output_asset,budget_bucket,
            amount_in_raw,status,quote_payload,attribution_payload,rejection_reason
            FROM paper_proposals WHERE proposal_id=?""", (proposal_id,)).fetchone()
        if row is None:
            return None
        names = ("source_event_id", "source_tx_hash", "wallet", "trigger_mode",
                 "strategy_version", "input_asset", "output_asset", "budget_bucket",
                 "amount_in_raw", "status", "quote", "attribution", "rejection_reason")
        result = dict(zip(names, row))
        result["proposal_id"] = proposal_id
        result["quote"] = json.loads(result["quote"]) if result["quote"] else None
        result["attribution"] = json.loads(result["attribution"])
        result["ledger_source_event_id"] = result["source_event_id"]
        result["source_event_id"] = result["attribution"].get(
            "source_event_id", result["source_event_id"])
        return result

    def execution_budget_evidence(self, proposal_id: str) -> dict:
        """Recheck the paper reservation that bounds a future execution amount."""
        proposal = self.connection.execute("""SELECT wallet,amount_in_raw,status,
            attribution_payload FROM paper_proposals WHERE proposal_id=?""",
            (proposal_id,)).fetchone()
        if proposal is None or proposal[2] != "reserved":
            raise ValueError("execution proposal is not reserved")
        attribution = json.loads(proposal[3])
        reservation = self.connection.execute("""SELECT r.cycle_id,r.wallet,r.bucket,
            r.amount_raw,r.status,b.limit_raw,b.reserved_raw,b.invested_raw,c.status
            FROM paper_reservations r
            JOIN paper_budgets b ON b.cycle_id=r.cycle_id AND b.wallet=r.wallet
                AND b.bucket=r.bucket
            JOIN paper_budget_cycles c ON c.cycle_id=r.cycle_id
            WHERE r.proposal_id=?""", (proposal_id,)).fetchone()
        if reservation is not None:
            if (reservation[1] != proposal[0] or reservation[3] != proposal[1]
                    or reservation[4] != "active" or reservation[8] != "active"
                    or int(reservation[6]) < int(reservation[3])
                    or int(reservation[6]) + int(reservation[7]) > int(reservation[5])):
                raise ValueError("execution budget reservation is stale or inconsistent")
            return {
                "kind": "buy_budget", "cycle_id": reservation[0],
                "ledger_scope": reservation[1], "bucket": reservation[2],
                "amount_raw": reservation[3], "limit_raw": reservation[5],
                "reserved_raw": reservation[6], "invested_raw": reservation[7],
                "follower_wallet": attribution.get("follower_wallet"),
                "relationship_id": attribution.get("relationship_id"),
            }
        positions = self.connection.execute("""SELECT r.token_amount_raw,p.wallet,p.status
            FROM paper_position_reservations r JOIN paper_positions p ON p.lot_id=r.lot_id
            WHERE r.proposal_id=? AND r.status='active'""", (proposal_id,)).fetchall()
        if (not positions or any(row[1] != proposal[0] or row[2] != "open"
                                 for row in positions)
                or sum(int(row[0]) for row in positions) != int(proposal[1])):
            raise ValueError("execution position reservation is stale or inconsistent")
        return {
            "kind": "sell_position", "ledger_scope": proposal[0],
            "amount_raw": proposal[1], "lots": len(positions),
            "follower_wallet": attribution.get("follower_wallet"),
            "relationship_id": attribution.get("relationship_id"),
        }

    def reserved_paper_proposal_ids(self, strategy_version: str,
                                    trigger_mode: str) -> list[str]:
        return [row[0] for row in self.connection.execute(
            """SELECT proposal_id FROM paper_proposals
               WHERE strategy_version=? AND trigger_mode=? AND status='reserved'
               ORDER BY created_at,proposal_id""", (strategy_version, trigger_mode))]

    def signal(self, event_id: str) -> Signal | None:
        row = self.connection.execute(
            "SELECT payload FROM signals WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[0])
        payload.pop("event_id", None)
        return Signal(**payload)

    def cancel_paper_proposal(self, proposal_id: str, reason: str) -> bool:
        if not reason:
            raise ValueError("cancellation reason is required")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            proposal = self.connection.execute(
                "SELECT status FROM paper_proposals WHERE proposal_id=?",
                (proposal_id,)).fetchone()
            if proposal is None or proposal[0] != "reserved":
                self.connection.rollback()
                return False
            row = self.connection.execute("""SELECT r.cycle_id,r.wallet,r.bucket,r.amount_raw,
                    p.status FROM paper_reservations r JOIN paper_proposals p
                    ON p.proposal_id=r.proposal_id WHERE r.proposal_id=?""",
                (proposal_id,)).fetchone()
            if row is not None:
                budget = self.connection.execute("""SELECT reserved_raw FROM paper_budgets
                    WHERE cycle_id=? AND wallet=? AND bucket=?""", row[:3]).fetchone()
                if budget is None or int(budget[0]) < int(row[3]):
                    raise ValueError("reservation ledger mismatch")
                self.connection.execute("""UPDATE paper_budgets SET reserved_raw=?
                    WHERE cycle_id=? AND wallet=? AND bucket=?""",
                    (str(int(budget[0]) - int(row[3])), *row[:3]))
                self.connection.execute("""UPDATE paper_reservations SET status='released',
                    updated_at=CURRENT_TIMESTAMP WHERE proposal_id=?""", (proposal_id,))
            else:
                released = self.connection.execute("""UPDATE paper_position_reservations
                    SET status='released' WHERE proposal_id=? AND status='active'""",
                    (proposal_id,)).rowcount
                if not released:
                    raise ValueError("proposal reservation missing")
            self.connection.execute("""UPDATE paper_proposals SET status='cancelled',
                rejection_reason=?,updated_at=CURRENT_TIMESTAMP WHERE proposal_id=?""",
                (reason, proposal_id))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def fill_paper_buy(self, proposal_id: str, fill: dict) -> bool:
        """Atomically consume a reservation and create immutable paper order/fill/lot rows."""
        required = {
            "order_id", "fill_id", "lot_id", "amount_out_raw", "fee_asset",
            "fee_amount_raw", "gas_cost_wei", "quote_observed_at", "filled_at",
        }
        if set(fill) != required:
            raise ValueError("invalid paper fill fields")
        for name in ("amount_out_raw", "fee_amount_raw", "gas_cost_wei"):
            value = fill[name]
            if not isinstance(value, str) or not value.isdecimal() or int(value) < 0:
                raise ValueError(f"invalid {name}")
        if int(fill["amount_out_raw"]) <= 0 or not fill["quote_observed_at"] or not fill["filled_at"]:
            raise ValueError("paper fill requires output and timestamps")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute("""SELECT p.source_event_id,p.source_tx_hash,p.wallet,
                    p.input_asset,p.output_asset,p.budget_bucket,p.amount_in_raw,
                    p.attribution_payload,p.status,r.cycle_id,r.status
                FROM paper_proposals p JOIN paper_reservations r
                ON r.proposal_id=p.proposal_id WHERE p.proposal_id=?""",
                (proposal_id,)).fetchone()
            if row is None:
                self.connection.rollback()
                return False
            if row[8] == "filled":
                existing = self.connection.execute(
                    "SELECT fill_id FROM paper_fills WHERE order_id=?", (fill["order_id"],)).fetchone()
                self.connection.rollback()
                return bool(existing and existing[0] == fill["fill_id"])
            if row[8] != "reserved" or row[10] != "active":
                raise ValueError("proposal has no active reservation")
            budget = self.connection.execute("""SELECT reserved_raw,invested_raw FROM paper_budgets
                WHERE cycle_id=? AND wallet=? AND bucket=?""",
                (row[9], row[2], row[5])).fetchone()
            if budget is None or int(budget[0]) < int(row[6]):
                raise ValueError("reservation ledger mismatch")
            attribution = json.loads(row[7])
            position_attribution = dict(attribution)
            source_output = position_attribution.get("source_amount_out_raw")
            if (isinstance(source_output, str) and source_output.isdecimal()
                    and int(source_output) > 0):
                position_attribution["source_position_initial_raw"] = source_output
                position_attribution["source_position_remaining_raw"] = source_output
            source_event_id = attribution.get("source_event_id", row[0])
            order_payload = json.dumps({
                "paper_only": True, "source_event_id": source_event_id,
                "source_tx_hash": row[1],
                "strategy_attribution": attribution,
            }, sort_keys=True)
            self.connection.execute("""INSERT INTO paper_orders(
                order_id,proposal_id,side,status,payload) VALUES(?,?,'BUY','filled',?)""",
                (fill["order_id"], proposal_id, order_payload))
            self.connection.execute("""INSERT INTO paper_fills(
                fill_id,order_id,input_asset,output_asset,amount_in_raw,amount_out_raw,
                fee_asset,fee_amount_raw,gas_cost_wei,quote_observed_at,filled_at,
                attribution_payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    fill["fill_id"], fill["order_id"], row[3], row[4], row[6],
                    fill["amount_out_raw"], fill["fee_asset"], fill["fee_amount_raw"],
                    fill["gas_cost_wei"], fill["quote_observed_at"], fill["filled_at"], row[7],
                ))
            self.connection.execute("""INSERT INTO paper_positions(
                lot_id,wallet,token,budget_cycle_id,budget_bucket,principal_asset,
                principal_initial_raw,principal_remaining_raw,token_initial_raw,
                token_remaining_raw,source_event_id,buy_fill_id,attribution_payload,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?, 'open')""", (
                    fill["lot_id"], row[2], row[4], row[9], row[5], row[3], row[6], row[6],
                    fill["amount_out_raw"], fill["amount_out_raw"], source_event_id,
                    fill["fill_id"], json.dumps(position_attribution, sort_keys=True),
                ))
            self.connection.execute("""UPDATE paper_budgets
                SET reserved_raw=?,invested_raw=? WHERE cycle_id=? AND wallet=? AND bucket=?""",
                (str(int(budget[0]) - int(row[6])),
                 str(int(budget[1]) + int(row[6])), row[9], row[2], row[5]))
            self.connection.execute("""UPDATE paper_reservations SET status='consumed',
                updated_at=CURRENT_TIMESTAMP WHERE proposal_id=?""", (proposal_id,))
            self.connection.execute("""UPDATE paper_proposals SET status='filled',
                updated_at=CURRENT_TIMESTAMP WHERE proposal_id=?""", (proposal_id,))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def paper_position(self, lot_id: str) -> dict | None:
        row = self.connection.execute("""SELECT wallet,token,budget_cycle_id,budget_bucket,principal_asset,
            principal_initial_raw,principal_remaining_raw,token_initial_raw,
            token_remaining_raw,source_event_id,buy_fill_id,attribution_payload,status
            FROM paper_positions WHERE lot_id=?""", (lot_id,)).fetchone()
        if row is None:
            return None
        names = ("wallet", "token", "budget_cycle_id", "budget_bucket", "principal_asset",
                 "principal_initial_raw", "principal_remaining_raw", "token_initial_raw",
                 "token_remaining_raw", "source_event_id", "buy_fill_id", "attribution", "status")
        result = dict(zip(names, row))
        result["attribution"] = json.loads(result["attribution"])
        return result

    def open_paper_position_ids(self) -> list[str]:
        return [row[0] for row in self.connection.execute(
            "SELECT lot_id FROM paper_positions WHERE status='open' ORDER BY created_at,lot_id")]

    def record_paper_position_mark(self, mark: dict) -> bool:
        required = {
            "mark_id", "lot_id", "principal_asset", "token_amount_raw", "gross_value_raw",
            "principal_remaining_raw", "unrealized_pnl_raw", "gas_cost_wei",
            "block_number", "block_hash", "quote_source", "quote_observed_at", "risk",
        }
        if set(mark) != required:
            raise ValueError("invalid paper position mark fields")
        for name in ("token_amount_raw", "gross_value_raw", "principal_remaining_raw",
                     "gas_cost_wei"):
            if not isinstance(mark[name], str) or not mark[name].isdecimal():
                raise ValueError(f"invalid mark {name}")
        pnl = mark["unrealized_pnl_raw"]
        if not isinstance(pnl, str) or not pnl.lstrip("-").isdecimal():
            raise ValueError("invalid mark unrealized pnl")
        cursor = self.connection.execute("""INSERT OR IGNORE INTO paper_position_marks(
            mark_id,lot_id,principal_asset,token_amount_raw,gross_value_raw,
            principal_remaining_raw,unrealized_pnl_raw,gas_cost_wei,block_number,
            block_hash,quote_source,quote_observed_at,risk_payload)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                mark["mark_id"], mark["lot_id"], mark["principal_asset"].lower(),
                mark["token_amount_raw"], mark["gross_value_raw"],
                mark["principal_remaining_raw"], mark["unrealized_pnl_raw"],
                mark["gas_cost_wei"], mark["block_number"], mark["block_hash"].lower(),
                mark["quote_source"].lower(), mark["quote_observed_at"],
                json.dumps(mark["risk"], sort_keys=True),
            ))
        self.connection.commit()
        return cursor.rowcount == 1

    def paper_position_marks(self, lot_id: str) -> list[dict]:
        names = ("mark_id", "principal_asset", "token_amount_raw", "gross_value_raw",
                 "principal_remaining_raw", "unrealized_pnl_raw", "gas_cost_wei",
                 "block_number", "block_hash", "quote_source", "quote_observed_at", "risk")
        result = []
        for row in self.connection.execute("""SELECT mark_id,principal_asset,token_amount_raw,
                gross_value_raw,principal_remaining_raw,unrealized_pnl_raw,gas_cost_wei,
                block_number,block_hash,quote_source,quote_observed_at,risk_payload
                FROM paper_position_marks WHERE lot_id=? ORDER BY block_number,mark_id""",
                (lot_id,)):
            item = dict(zip(names, row))
            item["risk"] = json.loads(item["risk"])
            result.append(item)
        return result

    def paper_trades(self) -> list[dict]:
        """Return attributed paper fills with current source canonicality for analysis."""
        result = []
        rows = self.connection.execute("""SELECT o.order_id,o.side,o.status,f.fill_id,
                f.input_asset,f.output_asset,f.amount_in_raw,f.amount_out_raw,
                f.fee_asset,f.fee_amount_raw,f.gas_cost_wei,f.quote_observed_at,f.filled_at,
                p.proposal_id,p.source_event_id,p.source_tx_hash,p.wallet,p.trigger_mode,
                p.strategy_version,p.quote_payload,p.attribution_payload,p.created_at,
                d.created_at,d.payload,s.payload
            FROM paper_orders o JOIN paper_fills f ON f.order_id=o.order_id
            JOIN paper_proposals p ON p.proposal_id=o.proposal_id
            LEFT JOIN paper_decisions d ON d.source_event_id=p.source_event_id
                AND d.trigger_mode=p.trigger_mode AND d.strategy_version=p.strategy_version
            LEFT JOIN signals s ON s.event_id=COALESCE(
                json_extract(p.attribution_payload,'$.source_event_id'),p.source_event_id)
            ORDER BY f.filled_at,f.fill_id""").fetchall()
        names = ("order_id", "side", "order_status", "fill_id", "input_asset",
                 "output_asset", "amount_in_raw", "amount_out_raw", "fee_asset",
                 "fee_amount_raw", "gas_cost_wei", "quote_observed_at", "filled_at",
                 "proposal_id", "source_event_id", "source_tx_hash", "smart_wallet",
                 "trigger_mode", "strategy_version")
        for row in rows:
            item = dict(zip(names, row[:19]))
            item["decision_quote"] = json.loads(row[19]) if row[19] else None
            item["attribution"] = json.loads(row[20])
            item["ledger_scope"] = item["smart_wallet"]
            item["smart_wallet"] = item["attribution"].get(
                "smart_wallet", item["smart_wallet"])
            item["source_event_id"] = item["attribution"].get(
                "source_event_id", item["source_event_id"])
            item["proposal_created_at"] = row[21]
            item["decision_created_at"] = row[22]
            decision_payload = json.loads(row[23]) if row[23] else None
            item["decision"] = decision_payload
            item["source_signal_at_decision"] = (
                decision_payload.get("source_signal") if decision_payload else None)
            source = json.loads(row[24]) if row[24] else None
            item["source_canonical_status"] = (
                source.get("canonical_status") if source else "source_signal_missing")
            item["source_stage_current"] = source.get("stage") if source else None
            item["paper_only"] = True
            if item["side"] == "BUY":
                lot_ids = [lot[0] for lot in self.connection.execute(
                    "SELECT lot_id FROM paper_positions WHERE buy_fill_id=? ORDER BY lot_id",
                    (item["fill_id"],))]
                item["position_lots"] = []
                for lot_id in lot_ids:
                    position = self.paper_position(lot_id)
                    position["lot_id"] = lot_id
                    marks = self.paper_position_marks(lot_id)
                    position["latest_mark"] = marks[-1] if marks else None
                    item["position_lots"].append(position)
            else:
                item["realized_pnl_by_lot"] = self.paper_realized_pnl(item["fill_id"])
            result.append(item)
        return result

    def reserve_paper_sell(self, proposal: dict) -> tuple[bool, str]:
        """Reserve only inventory attributed to this smart wallet; never sell unrelated lots."""
        required = {
            "proposal_id", "source_event_id", "source_tx_hash", "wallet", "trigger_mode",
            "strategy_version", "input_asset", "output_asset", "budget_bucket",
            "amount_in_raw", "attribution",
        }
        if set(proposal) not in (required, required | {"quote"}):
            raise ValueError("invalid sell proposal fields")
        amount = proposal["amount_in_raw"]
        if not isinstance(amount, str) or not amount.isdecimal() or int(amount) <= 0:
            raise ValueError("invalid sell amount")
        if _paper_budget_bucket(proposal["output_asset"]) != proposal["budget_bucket"]:
            return False, "sell_output_budget_bucket_mismatch"
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT status FROM paper_proposals WHERE proposal_id=?",
                (proposal["proposal_id"],)).fetchone()
            if existing:
                self.connection.rollback()
                return existing[0] == "reserved", "proposal_already_exists"
            lots = self.connection.execute("""SELECT p.lot_id,p.token_remaining_raw,
                    p.principal_asset
                FROM paper_positions p WHERE p.wallet=? AND p.token=? AND p.budget_bucket=?
                    AND p.status='open' AND p.principal_asset!=''
                ORDER BY p.created_at,p.lot_id""",
                (proposal["wallet"].lower(), proposal["input_asset"].lower(),
                 proposal["budget_bucket"])).fetchall()
            allocations = []
            remaining = int(amount)
            for lot_id, token_remaining, principal_asset in lots:
                if _paper_budget_bucket(principal_asset) != proposal["budget_bucket"]:
                    continue
                reserved = sum(int(row[0]) for row in self.connection.execute(
                    """SELECT token_amount_raw FROM paper_position_reservations
                       WHERE lot_id=? AND status='active'""", (lot_id,)))
                available = int(token_remaining) - reserved
                take = min(max(available, 0), remaining)
                if take:
                    allocations.append((lot_id, str(take)))
                    remaining -= take
                if remaining == 0:
                    break
            if remaining:
                self.connection.rollback()
                return False, "attributed_position_insufficient"
            self.connection.execute("""INSERT INTO paper_proposals(
                proposal_id,source_event_id,source_tx_hash,wallet,trigger_mode,
                strategy_version,input_asset,output_asset,budget_bucket,amount_in_raw,
                status,quote_payload,attribution_payload) VALUES(?,?,?,?,?,?,?,?,?,?,'reserved',?,?)""", (
                    proposal["proposal_id"], proposal["source_event_id"],
                    proposal["source_tx_hash"].lower(), proposal["wallet"].lower(),
                    proposal["trigger_mode"], proposal["strategy_version"],
                    proposal["input_asset"].lower(), proposal["output_asset"].lower(),
                    proposal["budget_bucket"], amount,
                    json.dumps(proposal.get("quote"), sort_keys=True)
                    if proposal.get("quote") is not None else None,
                    json.dumps(proposal["attribution"], sort_keys=True),
                ))
            for lot_id, token_amount in allocations:
                self.connection.execute("""INSERT INTO paper_position_reservations(
                    proposal_id,lot_id,token_amount_raw,status) VALUES(?,?,?,'active')""",
                    (proposal["proposal_id"], lot_id, token_amount))
            self.connection.commit()
            return True, "reserved"
        except self.integrity_error:
            self.connection.rollback()
            return False, "proposal_source_already_reserved"
        except Exception:
            self.connection.rollback()
            raise

    def paper_sell_principal_asset(self, wallet: str, token: str,
                                   amount_raw: str) -> tuple[str | None, str]:
        """Select one attributed principal asset that can cover the requested sale."""
        if (not isinstance(amount_raw, str) or not amount_raw.isdecimal()
                or int(amount_raw) <= 0):
            raise ValueError("invalid sell amount")
        rows = self.connection.execute("""SELECT p.lot_id,p.token_remaining_raw,
                p.principal_asset FROM paper_positions p
            WHERE p.wallet=? AND p.token=? AND p.status='open'
                AND p.principal_asset!='' ORDER BY p.created_at,p.lot_id""",
            (wallet.lower(), token.lower())).fetchall()
        available_by_asset = {}
        for lot_id, remaining, principal_asset in rows:
            reserved = sum(int(row[0]) for row in self.connection.execute(
                """SELECT token_amount_raw FROM paper_position_reservations
                   WHERE lot_id=? AND status='active'""", (lot_id,)))
            available_by_asset[principal_asset] = (
                available_by_asset.get(principal_asset, 0)
                + max(int(remaining) - reserved, 0))
        matches = sorted(asset for asset, available in available_by_asset.items()
                         if available >= int(amount_raw))
        if not matches:
            return None, "attributed_position_insufficient"
        if len(matches) != 1:
            return None, "attributed_principal_asset_ambiguous"
        return matches[0], "selected"

    def paper_proportional_sell_amount(
            self, wallet: str, token: str, source_sell_raw: str,
            ratio_ppm: int) -> tuple[str | None, str]:
        """Map a smart-wallet sell fraction onto this relationship's attributed lots."""
        if (not isinstance(source_sell_raw, str) or not source_sell_raw.isdecimal()
                or int(source_sell_raw) <= 0 or not isinstance(ratio_ppm, int)
                or not 1 <= ratio_ppm <= 1_000_000):
            raise ValueError("invalid proportional sell inputs")
        target_source = int(source_sell_raw) * ratio_ppm // 1_000_000
        if target_source <= 0:
            return None, "planned_amount_rounds_to_zero"
        rows = self.connection.execute("""SELECT token_initial_raw,token_remaining_raw,
                attribution_payload FROM paper_positions
            WHERE wallet=? AND token=? AND status='open'
            ORDER BY created_at,lot_id""", (wallet.lower(), token.lower())).fetchall()
        local_total = 0
        source_left = target_source
        for token_initial, token_remaining, raw_attribution in rows:
            attribution = json.loads(raw_attribution)
            source_remaining = attribution.get("source_position_remaining_raw")
            if source_remaining is None:
                source_initial = attribution.get("source_amount_out_raw")
                if source_initial is None and int(token_initial) == int(token_remaining):
                    source = self.signal(attribution.get("source_event_id", ""))
                    if source is not None:
                        source_initial = source.evidence.get(
                            "actual_output_credit_raw", source.amount_out_raw)
                if (not isinstance(source_initial, str) or not source_initial.isdecimal()
                        or int(token_initial) != int(token_remaining)):
                    return None, "source_position_basis_missing"
                source_remaining = source_initial
            if (not isinstance(source_remaining, str)
                    or not source_remaining.isdecimal() or int(source_remaining) <= 0):
                return None, "source_position_basis_invalid"
            take_source = min(int(source_remaining), source_left)
            take_local = (int(token_remaining) if take_source == int(source_remaining)
                          else int(token_remaining) * take_source // int(source_remaining))
            local_total += take_local
            source_left -= take_source
            if source_left == 0:
                break
        if local_total <= 0:
            return None, "attributed_position_insufficient"
        return str(local_total), "selected"

    def paper_open_position_amount(self, wallet: str, token: str) -> str:
        """Return this ledger scope's attributed open balance for one token."""
        rows = self.connection.execute("""SELECT token_remaining_raw
            FROM paper_positions WHERE wallet=? AND token=? AND status='open'""",
            (wallet.lower(), token.lower())).fetchall()
        return str(sum(int(row[0]) for row in rows))

    def paper_sell_execution_route(
            self, wallet: str, token: str, principal_asset: str,
            amount_raw: str) -> tuple[dict | None, str]:
        """Recover one execution route from the BUY signals backing selected lots."""
        if (not isinstance(amount_raw, str) or not amount_raw.isdecimal()
                or int(amount_raw) <= 0):
            raise ValueError("invalid sell route amount")
        rows = self.connection.execute("""SELECT p.lot_id,p.token_remaining_raw,
                p.source_event_id,p.principal_asset
            FROM paper_positions p WHERE p.wallet=? AND p.token=?
                AND p.principal_asset=? AND p.status='open'
            ORDER BY p.created_at,p.lot_id""", (
                wallet.lower(), token.lower(), principal_asset.lower())).fetchall()
        remaining = int(amount_raw)
        selected = []
        for lot_id, token_remaining, source_event_id, _ in rows:
            reserved = sum(int(row[0]) for row in self.connection.execute(
                """SELECT token_amount_raw FROM paper_position_reservations
                   WHERE lot_id=? AND status='active'""", (lot_id,)))
            available = max(int(token_remaining) - reserved, 0)
            take = min(available, remaining)
            if not take:
                continue
            source = self.signal(source_event_id)
            route = (source.evidence.get("local_execution_route")
                     if source is not None else None)
            if not isinstance(route, dict):
                return None, "attributed_buy_execution_route_missing"
            try:
                protocol = route.get("protocol")
                assets = tuple(address(item) for item in route.get("assets", []))
                if (protocol not in {"v2", "v3", "v4", "kyber"} or len(assets) < 2
                        or {assets[0], assets[-1]}
                        != {token.lower(), principal_asset.lower()}):
                    return None, "attributed_buy_execution_route_invalid"
                if protocol == "kyber":
                    if (len(assets) != 2 or route.get("provider") != protocol
                            or not isinstance(route.get("router"), str)):
                        return None, "attributed_buy_execution_route_invalid"
                    parameters = ("aggregator", address(route["router"]))
                elif protocol == "v2":
                    parameters = ()
                elif protocol == "v3":
                    fees = route.get("fees")
                    if (not isinstance(fees, list) or len(fees) != len(assets) - 1
                            or any(not isinstance(fee, int)
                                   or not 0 <= fee < 2 ** 24 for fee in fees)):
                        return None, "attributed_buy_execution_route_invalid"
                    parameters = tuple(fees)
                else:
                    fields = tuple(tuple(route.get(name, [])) for name in (
                        "fees", "tick_spacings", "hooks", "hook_data"))
                    if any(len(field) != len(assets) - 1 for field in fields):
                        return None, "attributed_buy_execution_route_invalid"
                    parameters = fields
                selected.append(((protocol, assets, parameters), route))
            except (TypeError, ValueError):
                return None, "attributed_buy_execution_route_invalid"
            remaining -= take
            if remaining == 0:
                break
        if remaining:
            return None, "attributed_position_insufficient"
        unique = {item[0] for item in selected}
        if len(unique) != 1:
            return None, "attributed_buy_execution_route_ambiguous"
        return dict(selected[0][1]), "selected"

    def fill_paper_sell(self, proposal_id: str, fill: dict) -> bool:
        """Fill a paper sell and restore each source lot's original-principal budget."""
        required = {
            "order_id", "fill_id", "amount_out_raw", "fee_asset", "fee_amount_raw",
            "gas_cost_wei", "quote_observed_at", "filled_at",
        }
        if set(fill) != required:
            raise ValueError("invalid paper sell fill fields")
        for name in ("amount_out_raw", "fee_amount_raw", "gas_cost_wei"):
            if (not isinstance(fill[name], str) or not fill[name].isdecimal()
                    or int(fill[name]) < 0):
                raise ValueError(f"invalid {name}")
        if int(fill["amount_out_raw"]) <= 0:
            raise ValueError("sell fill output must be positive")
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            proposal = self.connection.execute("""SELECT source_event_id,source_tx_hash,wallet,
                    input_asset,output_asset,budget_bucket,amount_in_raw,attribution_payload,status
                FROM paper_proposals WHERE proposal_id=?""", (proposal_id,)).fetchone()
            if proposal is None:
                self.connection.rollback()
                return False
            if proposal[8] == "filled":
                existing = self.connection.execute(
                    "SELECT fill_id FROM paper_fills WHERE order_id=?", (fill["order_id"],)).fetchone()
                self.connection.rollback()
                return bool(existing and existing[0] == fill["fill_id"])
            reservations = self.connection.execute("""SELECT lot_id,token_amount_raw
                FROM paper_position_reservations WHERE proposal_id=? AND status='active'
                ORDER BY lot_id""", (proposal_id,)).fetchall()
            if proposal[8] != "reserved" or not reservations:
                raise ValueError("sell proposal has no active position reservation")
            total_tokens = sum(int(row[1]) for row in reservations)
            if total_tokens != int(proposal[6]):
                raise ValueError("sell reservation ledger mismatch")
            attribution = json.loads(proposal[7])
            order_payload = json.dumps({
                "paper_only": True,
                "source_event_id": attribution.get("source_event_id", proposal[0]),
                "source_tx_hash": proposal[1],
                "strategy_attribution": attribution,
            }, sort_keys=True)
            self.connection.execute("""INSERT INTO paper_orders(
                order_id,proposal_id,side,status,payload) VALUES(?,?,'SELL','filled',?)""",
                (fill["order_id"], proposal_id, order_payload))
            self.connection.execute("""INSERT INTO paper_fills(
                fill_id,order_id,input_asset,output_asset,amount_in_raw,amount_out_raw,
                fee_asset,fee_amount_raw,gas_cost_wei,quote_observed_at,filled_at,
                attribution_payload) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""", (
                    fill["fill_id"], fill["order_id"], proposal[3], proposal[4], proposal[6],
                    fill["amount_out_raw"], fill["fee_asset"], fill["fee_amount_raw"],
                    fill["gas_cost_wei"], fill["quote_observed_at"], fill["filled_at"], proposal[7],
                ))
            proceeds_left = int(fill["amount_out_raw"])
            fee_left = int(fill["fee_amount_raw"])
            gas_left = int(fill["gas_cost_wei"])
            for index, (lot_id, token_amount) in enumerate(reservations):
                lot = self.connection.execute("""SELECT token_remaining_raw,
                        principal_remaining_raw,budget_cycle_id,budget_bucket,status,principal_asset,
                        attribution_payload,token_initial_raw
                    FROM paper_positions WHERE lot_id=?""", (lot_id,)).fetchone()
                sold, token_remaining, principal_remaining = (
                    int(token_amount), int(lot[0]), int(lot[1]))
                if sold > token_remaining or lot[4] != "open":
                    raise ValueError("position reservation exceeds open lot")
                principal = (principal_remaining if sold == token_remaining else
                             principal_remaining * sold // token_remaining)
                if index + 1 == len(reservations):
                    proceeds, fee, gas = proceeds_left, fee_left, gas_left
                else:
                    proceeds = int(fill["amount_out_raw"]) * sold // total_tokens
                    fee = int(fill["fee_amount_raw"]) * sold // total_tokens
                    gas = int(fill["gas_cost_wei"]) * sold // total_tokens
                    proceeds_left -= proceeds
                    fee_left -= fee
                    gas_left -= gas
                budget = self.connection.execute("""SELECT invested_raw FROM paper_budgets
                    WHERE cycle_id=? AND wallet=? AND bucket=?""",
                    (lot[2], proposal[2], lot[3])).fetchone()
                if budget is None or int(budget[0]) < principal:
                    raise ValueError("invested budget ledger mismatch")
                new_tokens = token_remaining - sold
                new_principal = principal_remaining - principal
                position_attribution = json.loads(lot[6])
                source_remaining = position_attribution.get(
                    "source_position_remaining_raw")
                if source_remaining is None:
                    source_remaining = position_attribution.get("source_amount_out_raw")
                if (source_remaining is None
                        and int(lot[7]) == token_remaining):
                    source = self.signal(position_attribution.get("source_event_id", ""))
                    if source is not None:
                        source_remaining = source.evidence.get(
                            "actual_output_credit_raw", source.amount_out_raw)
                if (isinstance(source_remaining, str) and source_remaining.isdecimal()
                        and int(source_remaining) > 0):
                    source_value = int(source_remaining)
                    source_sold = (source_value if sold == token_remaining else
                                   source_value * sold // token_remaining)
                    position_attribution["source_position_remaining_raw"] = str(
                        source_value - source_sold)
                self.connection.execute("""UPDATE paper_positions SET token_remaining_raw=?,
                    principal_remaining_raw=?,attribution_payload=?,status=?,
                    updated_at=CURRENT_TIMESTAMP
                    WHERE lot_id=?""", (
                        str(new_tokens), str(new_principal),
                        json.dumps(position_attribution, sort_keys=True),
                        "closed" if new_tokens == 0 else "open", lot_id,
                    ))
                self.connection.execute("""UPDATE paper_budgets SET invested_raw=?
                    WHERE cycle_id=? AND wallet=? AND bucket=?""",
                    (str(int(budget[0]) - principal), lot[2], proposal[2], lot[3]))
                same_principal_bucket = (
                    _paper_budget_bucket(fill["fee_asset"]) == lot[3])
                fee_in_principal = fee if same_principal_bucket else 0
                pnl = proceeds - principal - fee_in_principal
                self.connection.execute("""INSERT INTO paper_realized_pnl(
                    fill_id,lot_id,principal_asset,principal_released_raw,proceeds_raw,
                    fee_in_principal_asset_raw,realized_pnl_raw,gas_cost_wei)
                    VALUES(?,?,?,?,?,?,?,?)""", (
                        fill["fill_id"], lot_id, lot[5], str(principal), str(proceeds),
                        str(fee_in_principal), str(pnl), str(gas),
                    ))
            self.connection.execute("""UPDATE paper_position_reservations SET status='consumed'
                WHERE proposal_id=?""", (proposal_id,))
            self.connection.execute("""UPDATE paper_proposals SET status='filled',
                updated_at=CURRENT_TIMESTAMP WHERE proposal_id=?""", (proposal_id,))
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise

    def paper_realized_pnl(self, fill_id: str) -> list[dict]:
        names = ("lot_id", "principal_asset", "principal_released_raw", "proceeds_raw",
                 "fee_in_principal_asset_raw", "realized_pnl_raw", "gas_cost_wei")
        return [dict(zip(names, row)) for row in self.connection.execute(
            """SELECT lot_id,principal_asset,principal_released_raw,proceeds_raw,
               fee_in_principal_asset_raw,realized_pnl_raw,gas_cost_wei
               FROM paper_realized_pnl WHERE fill_id=? ORDER BY lot_id""", (fill_id,))]

    def record_paper_decision(self, decision_id: str, source_event_id: str,
                              trigger_mode: str, strategy_version: str,
                              accepted: bool, reason: str | None, payload: dict) -> bool:
        cursor = self.connection.execute("""INSERT OR IGNORE INTO paper_decisions(
            decision_id,source_event_id,trigger_mode,strategy_version,accepted,reason,payload)
            VALUES(?,?,?,?,?,?,?)""", (
                decision_id, source_event_id, trigger_mode, strategy_version,
                int(accepted), reason, json.dumps(payload, sort_keys=True),
            ))
        self.connection.commit()
        return cursor.rowcount == 1

    def paper_decision(self, decision_id: str) -> dict | None:
        row = self.connection.execute("""SELECT source_event_id,trigger_mode,strategy_version,
            accepted,reason,payload FROM paper_decisions WHERE decision_id=?""",
            (decision_id,)).fetchone()
        if row is None:
            return None
        payload = json.loads(row[5])
        return {"source_event_id": payload.get("source_event_id", row[0]),
                "ledger_source_event_id": row[0], "trigger_mode": row[1],
                "strategy_version": row[2], "accepted": bool(row[3]),
                "reason": row[4], "payload": payload}

    def recover_inflight(self) -> int:
        recovered = self.connection.execute(
            "UPDATE candidates SET status='pending', updated_at=CURRENT_TIMESTAMP WHERE status='queued'"
        ).rowcount
        self.connection.commit()
        return max(recovered, 0)

    @staticmethod
    def _transaction_payload(tx: Transaction) -> str:
        return json.dumps({
            "hash": tx.hash, "sender": tx.sender, "to": tx.to, "data": "0x" + tx.data.hex(),
            "value": str(tx.value), "chain_id": tx.chain_id, "nonce": tx.nonce,
            "tx_type": tx.tx_type, "sequence": tx.sequence, "timestamp": tx.timestamp,
            "received_at": tx.received_at, "fresh": tx.fresh,
            "observation_source": tx.observation_source,
        }, sort_keys=True)

    @staticmethod
    def _transaction(payload: str) -> Transaction:
        row = json.loads(payload)
        row["data"] = bytes.fromhex(row["data"][2:])
        row["value"] = int(row["value"])
        return Transaction(**row)

    def put_candidate(self, tx: Transaction) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO candidates(tx_hash,payload,status) VALUES(?,?,'pending')",
            (tx.hash, self._transaction_payload(tx)),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def claim_candidates(self, limit: int, now: float | None = None) -> list[Transaction]:
        if limit <= 0:
            return []
        now = time.time() if now is None else now
        rows = self.connection.execute(
            """SELECT tx_hash,payload FROM candidates
               WHERE status IN ('pending','retry') AND next_attempt_at<=?
               ORDER BY created_at,tx_hash LIMIT ?""",
            (now, limit),
        ).fetchall()
        claimed = []
        for tx_hash, payload in rows:
            cursor = self.connection.execute(
                """UPDATE candidates SET status='queued', attempts=attempts+1,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE tx_hash=? AND status IN ('pending','retry') AND next_attempt_at<=?""",
                (tx_hash, now),
            )
            if cursor.rowcount == 1:
                claimed.append(self._transaction(payload))
        self.connection.commit()
        return claimed

    def retry_candidate(self, tx_hash: str, reason: str,
                        now: float | None = None) -> tuple[int, float | None]:
        now = time.time() if now is None else now
        row = self.connection.execute(
            "SELECT attempts FROM candidates WHERE tx_hash=?", (tx_hash,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown candidate")
        attempts = row[0]
        if attempts >= MAX_CANDIDATE_ATTEMPTS:
            self.fail_candidate(tx_hash, reason + "_retry_exhausted")
            return attempts, None
        delay = float(min(60, 2 ** min(attempts, 6)))
        self.connection.execute(
            """UPDATE candidates SET status='retry', next_attempt_at=?, last_error=?,
               updated_at=CURRENT_TIMESTAMP WHERE tx_hash=?""",
            (now + delay, reason, tx_hash),
        )
        self.connection.commit()
        return attempts, delay

    def complete_candidate(self, tx_hash: str, block_number: int | None = None,
                           block_hash: str | None = None) -> None:
        self.connection.execute(
            """UPDATE candidates SET status='complete', next_attempt_at=0, last_error=NULL,
               updated_at=CURRENT_TIMESTAMP WHERE tx_hash=?""",
            (tx_hash,),
        )
        if block_number is not None and block_hash:
            self.connection.execute("""INSERT INTO candidate_inclusions(tx_hash,block_number,block_hash)
                VALUES(?,?,?) ON CONFLICT(tx_hash) DO UPDATE SET block_number=excluded.block_number,
                block_hash=excluded.block_hash""", (tx_hash, block_number, block_hash.lower()))
            known = self.chain_block_hash(block_number)
            if known == block_hash.lower():
                self._mark_block_safe_head(block_number, block_hash.lower())
        self.connection.commit()

    def fail_candidate(self, tx_hash: str, reason: str) -> None:
        self.connection.execute(
            """UPDATE candidates SET status='failed', next_attempt_at=0, last_error=?,
               updated_at=CURRENT_TIMESTAMP WHERE tx_hash=?""",
            (reason, tx_hash),
        )
        self.connection.commit()

    def candidate_counts(self) -> dict[str, int]:
        counts = {status: count for status, count in self.connection.execute(
            "SELECT status,COUNT(*) FROM candidates GROUP BY status"
        )}
        return {status: counts.get(status, 0) for status in (
            "pending", "queued", "retry", "complete", "failed"
        )}

    def chain_cursor(self, name: str = "canonical_l2") -> tuple[int, str] | None:
        row = self.connection.execute(
            "SELECT block_number,block_hash FROM chain_cursors WHERE name=?", (name,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def set_chain_cursor(self, block_number: int, block_hash: str,
                         name: str = "canonical_l2") -> None:
        if block_number < 0 or not isinstance(block_hash, str) or not block_hash.startswith("0x"):
            raise ValueError("invalid chain cursor")
        old = self.chain_cursor(name)
        if old and block_number < old[0]:
            raise ValueError("chain cursor rewind requires explicit reorg handling")
        self.connection.execute("""INSERT INTO chain_cursors(name,block_number,block_hash) VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET block_number=excluded.block_number,
            block_hash=excluded.block_hash, updated_at=CURRENT_TIMESTAMP""",
            (name, block_number, block_hash.lower()))
        self.connection.commit()

    def record_chain_block(self, block_number: int, block_hash: str, parent_hash: str) -> None:
        if block_number < 0 or not all(
                isinstance(value, str) and value.startswith("0x")
                for value in (block_hash, parent_hash)):
            raise ValueError("invalid canonical block")
        old = self.chain_cursor()
        if old and block_number < old[0]:
            raise ValueError("canonical block rewind requires explicit reorg handling")
        self.connection.execute("""INSERT INTO canonical_blocks(block_number,block_hash,parent_hash)
            VALUES(?,?,?) ON CONFLICT(block_number) DO UPDATE SET block_hash=excluded.block_hash,
            parent_hash=excluded.parent_hash""",
            (block_number, block_hash.lower(), parent_hash.lower()))
        self.connection.execute("""INSERT INTO chain_cursors(name,block_number,block_hash)
            VALUES('canonical_l2',?,?) ON CONFLICT(name) DO UPDATE SET
            block_number=excluded.block_number,block_hash=excluded.block_hash,
            updated_at=CURRENT_TIMESTAMP""", (block_number, block_hash.lower()))
        self._mark_block_safe_head(block_number, block_hash.lower())
        self.connection.commit()

    def _mark_block_safe_head(self, block_number: int, block_hash: str) -> int:
        updated = 0
        rows = self.connection.execute("""SELECT s.event_id,s.payload FROM signals s
            JOIN candidate_inclusions c ON c.tx_hash=s.tx_hash
            WHERE c.block_number=? AND c.block_hash=?""", (block_number, block_hash)).fetchall()
        for event_id, payload in rows:
            document = json.loads(payload)
            evidence = document.get("evidence", {})
            if (evidence.get("block_hash", "").lower() != block_hash
                    or document.get("canonical_status") == "orphaned"):
                continue
            document["canonical_status"] = "safe_head_confirmed"
            evidence["canonicality"] = "safe_head_hash_rechecked_not_l1_finality"
            self.connection.execute(
                "UPDATE signals SET payload=?,updated_at=CURRENT_TIMESTAMP WHERE event_id=?",
                (json.dumps(document, ensure_ascii=False, sort_keys=True), event_id),
            )
            updated += 1
        return updated

    def chain_block_hash(self, block_number: int) -> str | None:
        row = self.connection.execute(
            "SELECT block_hash FROM canonical_blocks WHERE block_number=?", (block_number,)
        ).fetchone()
        return row[0] if row else None

    def rewind_chain(self, block_number: int, block_hash: str) -> tuple[int, int]:
        orphan_hashes = {row[0] for row in self.connection.execute(
            "SELECT block_hash FROM canonical_blocks WHERE block_number>?", (block_number,)
        )}
        orphaned_signals = 0
        for event_id, payload in self.connection.execute("SELECT event_id,payload FROM signals").fetchall():
            document = json.loads(payload)
            if document.get("evidence", {}).get("block_hash", "").lower() not in orphan_hashes:
                continue
            document["canonical_status"] = "orphaned"
            document["evidence"]["canonicality"] = "orphaned_by_reorg"
            self.connection.execute(
                "UPDATE signals SET payload=?,updated_at=CURRENT_TIMESTAMP WHERE event_id=?",
                (json.dumps(document, ensure_ascii=False, sort_keys=True), event_id),
            )
            orphaned_signals += 1
        tx_rows = self.connection.execute(
            "SELECT tx_hash FROM candidate_inclusions WHERE block_number>?", (block_number,)
        ).fetchall()
        for (tx_hash,) in tx_rows:
            self.connection.execute("""UPDATE candidates SET status='pending',attempts=0,
                next_attempt_at=0,last_error='reorg_recheck',updated_at=CURRENT_TIMESTAMP
                WHERE tx_hash=?""", (tx_hash,))
            self.connection.execute(
                "DELETE FROM solver_order_evidence WHERE tx_hash=?", (tx_hash,))
        self.connection.execute("DELETE FROM candidate_inclusions WHERE block_number>?", (block_number,))
        self.connection.execute("DELETE FROM canonical_blocks WHERE block_number>?", (block_number,))
        self.connection.execute("""UPDATE chain_cursors SET block_number=?,block_hash=?,
            updated_at=CURRENT_TIMESTAMP WHERE name='canonical_l2'""",
            (block_number, block_hash.lower()))
        self.connection.commit()
        return orphaned_signals, len(tx_rows)

    def put(self, signal: Signal) -> bool:
        payload = json.dumps(signal.to_dict(), ensure_ascii=False, sort_keys=True)
        rank = STAGE_RANK[signal.stage]
        old = self.connection.execute("SELECT stage_rank, payload FROM signals WHERE event_id=?", (signal.event_id,)).fetchone()
        if old and (old[0] > rank or old[1] == payload):
            return False
        self.connection.execute("""INSERT INTO signals(event_id, tx_hash, stage_rank, payload) VALUES(?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET stage_rank=excluded.stage_rank,
            payload=excluded.payload, updated_at=CURRENT_TIMESTAMP""", (signal.event_id, signal.tx_hash, rank, payload))
        if (signal.behavior == "INTENT_DEPOSIT"
                and signal.evidence.get("solver_order_status") == "source_deposit_evidenced"):
            order_id = signal.evidence.get("order_id")
            self.connection.execute("""INSERT OR IGNORE INTO solver_order_evidence(
                evidence_id,order_id,kind,wallet,tx_hash,payload) VALUES(?,?,'source_deposit',?,?,?)""",
                (signal.event_id, order_id, signal.wallet, signal.tx_hash, payload))
        self.connection.commit()
        return True

    def record_solver_delivery(self, order_id: str, wallet: str, tx_hash: str,
                               evidence: dict) -> str:
        """Persist explicit delivery evidence; never infer an order from recipient alone."""
        if not (isinstance(order_id, str) and len(order_id) == 66 and order_id.startswith("0x")):
            raise ValueError("invalid solver order id")
        source = self.connection.execute("""SELECT wallet FROM solver_order_evidence
            WHERE order_id=? AND kind='source_deposit'""", (order_id.lower(),)).fetchall()
        if len(source) != 1:
            return "source_order_not_uniquely_evidenced"
        if source[0][0] != wallet.lower():
            return "delivery_wallet_mismatch"
        evidence_id = f"solver-delivery:{order_id.lower()}:{tx_hash.lower()}:{wallet.lower()}"
        payload = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
        self.connection.execute("""INSERT OR IGNORE INTO solver_order_evidence(
            evidence_id,order_id,kind,wallet,tx_hash,payload)
            VALUES(?,?,'destination_delivery',?,?,?)""",
            (evidence_id, order_id.lower(), wallet.lower(), tx_hash.lower(), payload))
        self.connection.commit()
        return "order_delivery_linked"

    def solver_order(self, order_id: str) -> list[dict]:
        return [{"kind": kind, "wallet": wallet, "tx_hash": tx_hash,
                 "evidence": json.loads(payload)}
                for kind, wallet, tx_hash, payload in self.connection.execute(
                    """SELECT kind,wallet,tx_hash,payload FROM solver_order_evidence
                       WHERE order_id=? ORDER BY kind,tx_hash""", (order_id.lower(),))]

    def rows(self):
        for (payload,) in self.connection.execute("SELECT payload FROM signals ORDER BY event_id"):
            yield json.loads(payload)

    def close(self):
        self.connection.close()
