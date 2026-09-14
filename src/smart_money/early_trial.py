"""Durable bounded-trial gates, not an authorization to sign or broadcast."""
from __future__ import annotations

import json
import math
import re
import time

from .models import address
from .registry import NATIVE

TRIAL_SECONDS = 24 * 60 * 60
TRIAL_LIMIT = 100


def _now(value=None):
    value = time.time() if value is None else value
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
        raise ValueError("invalid trial time")
    return float(value)


def _id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", value):
        raise ValueError("invalid early trial id")
    return value


class EarlyTrialStore:
    def start_early_trial(self, trial_id, follower, relationships, now=None):
        """Explicit operator call only. Reusing an ID never resets time or count."""
        trial_id, follower, now = _id(trial_id), address(follower), _now(now)
        if follower == NATIVE or not isinstance(relationships, (list, tuple)) or not 1 <= len(relationships) <= 64:
            raise ValueError("invalid early trial scope")
        if any(isinstance(r, bool) or not isinstance(r, (str, int))
               or not re.fullmatch(r"[1-9][0-9]{0,19}", str(r)) for r in relationships):
            raise ValueError("invalid early trial relationships")
        scope = sorted(str(r) for r in relationships)
        if len(set(scope)) != len(scope):
            raise ValueError("duplicate early trial relationships")
        payload = json.dumps(scope, separators=(",", ":"))
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute("""SELECT follower_wallet,relationships_payload
                FROM early_trials WHERE trial_id=?""", (trial_id,)).fetchone()
            if existing:
                if existing != (follower, payload):
                    raise ValueError("early trial id scope mismatch")
                self.connection.rollback()
                return self.early_trial_status(trial_id, now)
            # One trial per follower; starting another one requires an explicit
            # future operator workflow. Never auto-renew an expired trial.
            previous = self.connection.execute(
                "SELECT trial_id FROM early_trials WHERE follower_wallet=?", (follower,)).fetchone()
            if previous:
                raise ValueError("follower already has a trial; automatic renewal forbidden")
            self.connection.execute("""INSERT INTO early_trials
                (trial_id,follower_wallet,relationships_payload,started_at,expires_at,consumed_slots,status)
                VALUES(?,?,?,?,?,0,'active')""", (trial_id, follower, payload, now, now + TRIAL_SECONDS))
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return self.early_trial_status(trial_id, now)

    def early_trial_status(self, trial_id, now=None):
        now = _now(now)
        row = self.connection.execute("""SELECT follower_wallet,relationships_payload,
            started_at,expires_at,consumed_slots,status FROM early_trials WHERE trial_id=?""",
            (_id(trial_id),)).fetchone()
        if row is None:
            return None
        if (row[3] - row[2] != TRIAL_SECONDS or not 0 <= row[4] <= TRIAL_LIMIT):
            raise ValueError("early trial limits are inconsistent")
        reason = ("trial_stopped" if row[5] != "active" else
                  "trial_clock_before_start" if now < row[2] else
                  "trial_expired" if now >= row[3] else
                  "trial_limit_reached" if row[4] >= TRIAL_LIMIT else None)
        return {"trial_id": trial_id, "follower_wallet": row[0],
                "relationships": json.loads(row[1]), "started_at": row[2],
                "expires_at": row[3], "consumed_slots": row[4], "limit": TRIAL_LIMIT,
                "eligible": reason is None, "reason": reason,
                "count_basis": "durable_broadcast_attempt_upper_bound"}

    def _check_early_trial(self, attribution, now=None):
        if "early_trial_id" not in attribution:
            return None
        if "copy_operation_order_id" not in attribution:
            raise ValueError("early trial requires operation ownership")
        if attribution.get("source_behavior") not in {"BUY", "SELL"}:
            raise ValueError("early trial only counts copy BUY or SELL")
        trial = self.early_trial_status(attribution["early_trial_id"], now)
        if trial is None:
            raise ValueError("early trial missing")
        if (attribution.get("follower_wallet") != trial["follower_wallet"]
                or str(attribution.get("relationship_id")) not in trial["relationships"]):
            raise ValueError("early trial scope mismatch")
        if not trial["eligible"]:
            raise ValueError(trial["reason"])
        return trial

    def _consume_early_trial_slot(self, attribution, operation_key, proposal_id, now=None):
        """Inside the SAME transaction as the send fence. No separate commit."""
        fixed_now = now
        now = _now(now)
        trial = self._check_early_trial(attribution, now)
        if trial is None:
            return
        now = _now(fixed_now)
        self.connection.execute("""INSERT INTO early_trial_operations
            (operation_key,trial_id,proposal_id,attempted_at) VALUES(?,?,?,?)""",
            (operation_key, trial["trial_id"], proposal_id, now))
        changed = self.connection.execute("""UPDATE early_trials
            SET consumed_slots=consumed_slots+1 WHERE trial_id=?
            AND status='active' AND started_at<=? AND expires_at>?
            AND consumed_slots<?""", (trial["trial_id"], now, now, TRIAL_LIMIT)).rowcount
        if changed != 1:
            raise ValueError("early trial changed or exhausted")

    def check_early_trial_proposal(self, proposal_id, now=None):
        proposal = self.paper_proposal(proposal_id)
        if proposal is None or proposal["status"] != "reserved":
            raise ValueError("early trial proposal unavailable")
        return self._check_early_trial(proposal["attribution"], now)

    def check_early_trial_send_fence(self, proposal_id, now=None):
        """Recheck immediately before RPC, including a stop after slot allocation.

        The 100th reserved slot may send even though no NEW slots remain.
        """
        proposal = self.paper_proposal(proposal_id)
        if proposal is None or proposal["status"] != "reserved":
            raise ValueError("early trial proposal unavailable")
        trial_id = proposal["attribution"].get("early_trial_id")
        trial = self.early_trial_status(trial_id, now)
        if trial is None or trial["reason"] not in {None, "trial_limit_reached"}:
            raise ValueError("early trial is no longer active")
        row = self.connection.execute("""SELECT c.status FROM early_trial_operations t
            JOIN copy_operation_claims c ON c.operation_key=t.operation_key
            AND c.proposal_id=t.proposal_id WHERE t.trial_id=? AND t.proposal_id=?""",
            (trial_id, proposal_id)).fetchone()
        if row != ("broadcast_attempted",):
            raise ValueError("early trial send fence missing")
        return True

    def stop_early_trial(self, trial_id):
        """Stops new early entries only; never cancels pending trades or sells lots."""
        self.connection.execute("""UPDATE early_trials SET status='stopped'
            WHERE trial_id=? AND status='active'""", (_id(trial_id),))
        self.connection.commit()
