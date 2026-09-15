"""Verified intent is NOT a confirmed exchange or permission to trade.

Constructed by reparsing a public Feed transaction plus timestamped observations;
never constructed from a saved `recognized_intent=true` flag. Snapshot provenance
must still come from the trusted runtime collector, not external user input.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import json

from .early_intent import Candidate, parse_candidates, timestamp
from .early_replay import evaluate_candidate
from .early_timing import EARLY_FEED_MAX_AGE_SECONDS
from .models import Signal


@dataclass(frozen=True)
class VerifiedFeedIntent:
    _candidate_json: str
    _snapshots_json: str
    verified_at: float
    _deployment_monitor: object = field(default=None, repr=False, compare=False)

    @classmethod
    def verify(cls, tx, wallet, path, snapshots, at, *, deployment_monitor=None):
        matches = [c for c in parse_candidates(tx, wallet).candidates if c.path == path]
        if len(matches) != 1:
            raise ValueError("early intent path not unique or unsupported")
        intent = cls(json.dumps(asdict(matches[0]), sort_keys=True),
                     json.dumps(snapshots, sort_keys=True), timestamp(at), deployment_monitor)
        intent.revalidate(at)
        return intent

    @property
    def candidate(self):
        return Candidate(**json.loads(self._candidate_json))

    def revalidate(self, at):
        at = timestamp(at)
        if at < self.verified_at:
            raise ValueError("early intent verification is in the future")
        candidate = self.candidate
        result = evaluate_candidate(candidate, at, json.loads(self._snapshots_json), enabled=False,
                                    feed_max_age_seconds=EARLY_FEED_MAX_AGE_SECONDS,
                                    deployment_monitor=self._deployment_monitor)
        required = ["freshness", "semantics", "attribution"]
        if candidate.route_kind == "relay_wrapper":
            required.append("deployment")
        for name in required:
            if result["checks"][name]["status"] != "pass":
                raise ValueError("early intent " + name + ": " + result["checks"][name].get("reason", "missing"))
        return result["attribution"]

    def deployment_evidence(self):
        """Frozen code identity used for this intent, not a refreshed timestamp."""
        snapshot = json.loads(self._snapshots_json).get("deployment")
        if self.candidate.route_kind != "relay_wrapper" or snapshot is None:
            return None
        from .relay_race import verify_deployment
        return dict(verify_deployment(snapshot["payload"]),
                    observed_at=snapshot["observed_at"],
                    capture_started_at=snapshot.get("capture_started_at"),
                    block_hash=snapshot["payload"]["block_hash"],
                    provenance=snapshot.get("provenance"),
                    validation_mode=("periodic_monitor" if self._deployment_monitor is not None
                                     else "per_candidate_snapshot"))

    def quote_signal(self, at):
        attribution = self.revalidate(at)
        c = self.candidate
        # Payment is a verified order amount, not a destination-chain debit.
        minimum = attribution["order_minimum_raw"] if c.side == "BUY" else c.minimum_output_raw
        return Signal(c.tx_hash, c.wallet, "third_party" if c.side == "BUY" else "bundled_account",
                      c.side, c.path, None, "", stage="intent", execution_status="pending",
                      token_in=c.token_in, token_out=c.token_out,
                      amount_in_raw=attribution["source_amount_raw"], amount_limit_raw=minimum,
                      exact_in=True, protocol="kyber", fresh=True,
                      evidence={"verified_feed_intent": True,
                                "feed_max_age_seconds": EARLY_FEED_MAX_AGE_SECONDS,
                                "copy_operation_order_id": c.order_id,
                                "amount_basis": "order_payment" if c.side == "BUY" else "declared_sell",
                                "verification_at": self.verified_at})
