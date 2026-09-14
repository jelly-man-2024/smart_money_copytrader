"""Stage-independent Relay operation ownership. No trading authorization here."""
from __future__ import annotations

import hashlib
import json
import re

from .models import address
from .registry import CHAIN_ID, NATIVE
from .early_trial import EarlyTrialStore


def operation_key(wallet: str, order_id: str) -> str:
    if not isinstance(order_id, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", order_id):
        raise ValueError("invalid copy operation order id")
    wallet = address(wallet)
    if wallet == NATIVE:
        raise ValueError("invalid copy operation wallet")
    return _hash([CHAIN_ID, wallet, order_id.lower()])


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def relationship_operation_key(wallet, order_id, relationship_id, follower):
    if (not isinstance(relationship_id, (str, int)) or isinstance(relationship_id, bool)
            or not str(relationship_id).isascii() or not str(relationship_id).isdecimal()
            or int(relationship_id) <= 0 or str(int(relationship_id)) != str(relationship_id)):
        raise ValueError("invalid copy operation relationship")
    follower = address(follower)
    if follower == NATIVE:
        raise ValueError("invalid copy operation follower")
    return _hash([operation_key(wallet, order_id), str(relationship_id), follower])


def attribution_operation_key(attribution):
    """Opt-in until runtime migration/backfill is explicitly activated.

    The order id is parsed/verified by the caller, not inferred from a receipt
    Transfer. Old proposals remain on their existing deduplication path.
    """
    if "copy_operation_order_id" not in attribution:
        return None
    return relationship_operation_key(
        attribution.get("smart_wallet"), attribution["copy_operation_order_id"],
        attribution.get("relationship_id"), attribution.get("follower_wallet"))


class CopyOperationStore(EarlyTrialStore):
    def _legacy_operation_exists(self, proposal):
        """Bridge pre-upgrade ledger records without resetting or rewriting them."""
        wanted = proposal["attribution"]["copy_operation_order_id"].lower()
        rows = self.connection.execute("""SELECT p.proposal_id,p.status,p.attribution_payload,
            e.plan_id,s.payload FROM paper_proposals p
            LEFT JOIN execution_plans e ON e.proposal_id=p.proposal_id
            LEFT JOIN signals s ON s.tx_hash=p.source_tx_hash
            WHERE p.wallet=?""", (proposal["wallet"],)).fetchall()
        for old_id, status, payload, plan_id, raw in rows:
            if old_id == proposal["proposal_id"]:
                continue
            attr = json.loads(payload)
            if status == "cancelled" and plan_id is None:
                continue
            if attr.get("copy_operation_order_id", "").lower() == wanted:
                return True
            if raw is not None:
                source = json.loads(raw)
                evidence = source.get("evidence", {})
                order = evidence.get("relay_order_id", evidence.get("relay_deposit_order_id", ""))
                if (source.get("wallet") == proposal["attribution"].get("smart_wallet")
                        and isinstance(order, str) and order.lower() == wanted):
                    return True
        return False

    def _claim_copy_operation(self, proposal):
        """Called INSIDE the budget/lot reservation transaction. Never commits."""
        self._check_early_trial(proposal["attribution"])
        key = attribution_operation_key(proposal["attribution"])
        if key is None:
            return True
        if self._legacy_operation_exists(proposal):
            return False
        row = self.connection.execute(
            "SELECT proposal_id,status FROM copy_operation_claims WHERE operation_key=?",
            (key,)).fetchone()
        if row:
            if row[0] == proposal["proposal_id"]:
                return row[1] == "held"
            if row[1] != "released":
                return False
            self.connection.execute("""UPDATE copy_operation_claims
                SET proposal_id=?,status='held',updated_at=CURRENT_TIMESTAMP
                WHERE operation_key=? AND status='released'""", (proposal["proposal_id"], key))
        else:
            self.connection.execute("""INSERT INTO copy_operation_claims
                (operation_key,proposal_id,status) VALUES(?,?,'held')""",
                (key, proposal["proposal_id"]))
        return True

    def _release_unprepared_copy_operation(self, proposal_id, attribution):
        """Cancel can release ownership only when no execution plan ever existed.

        Even a cancelled signed plan remains conservatively held: proving its
        bytes never escaped needs a separate, audited handoff. Unknown is held.
        """
        key = attribution_operation_key(attribution)
        if key is None:
            return
        row = self.connection.execute(
            "SELECT proposal_id,status FROM copy_operation_claims WHERE operation_key=?",
            (key,)).fetchone()
        if not row or row[0] != proposal_id:
            raise ValueError("copy operation ownership mismatch")
        plan = self.connection.execute(
            "SELECT plan_id,status FROM execution_plans WHERE proposal_id=?", (proposal_id,)).fetchone()
        attempts = (self.connection.execute(
            "SELECT status FROM execution_attempts WHERE plan_id=?", (plan[0],)).fetchall()
                    if plan else [])
        reverted = (bool(attempts) and any(a[0] == "reverted" for a in attempts)
                    and all(a[0] in {"reverted", "replaced"} for a in attempts))
        if row[1] == "broadcast_attempted" and not reverted:
            raise ValueError("copy operation broadcast outcome unresolved")
        if plan and plan[1] != "cancelled" and not reverted:
            raise ValueError("copy operation execution is still active")
        if row[1] == "held" and plan is None:
            self.connection.execute("""UPDATE copy_operation_claims
                SET status='released',updated_at=CURRENT_TIMESTAMP WHERE operation_key=?""", (key,))

    def mark_copy_operation_broadcast_attempted(self, proposal_id, now=None):
        """Durable send fence, BEFORE network I/O. Failure/timeout never clears it.

        Returns False only for legacy proposals not enrolled in operation claims.
        A second send attempt raises, including after process restart.
        """
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            proposal = self.connection.execute(
                "SELECT status,attribution_payload FROM paper_proposals WHERE proposal_id=?",
                (proposal_id,)).fetchone()
            if not proposal or proposal[0] != "reserved":
                raise ValueError("copy operation proposal unavailable")
            attribution = json.loads(proposal[1])
            key = attribution_operation_key(attribution)
            if key is None:
                if "early_trial_id" in attribution:
                    raise ValueError("early trial requires operation ownership")
                self.connection.rollback()
                return False
            plan = self.connection.execute(
                "SELECT status,signed_tx_hash FROM execution_plans WHERE proposal_id=?",
                (proposal_id,)).fetchone()
            if not plan or plan[0] != "signed" or not plan[1]:
                raise ValueError("copy operation has no signed plan")
            changed = self.connection.execute("""UPDATE copy_operation_claims
                SET status='broadcast_attempted',updated_at=CURRENT_TIMESTAMP
                WHERE operation_key=? AND proposal_id=? AND status='held'""",
                (key, proposal_id)).rowcount
            if changed != 1:
                raise ValueError("copy operation already attempted or ownership lost")
            self._consume_early_trial_slot(attribution, key, proposal_id, now)
            self.connection.commit()
            return True
        except Exception:
            self.connection.rollback()
            raise
