"""Verified intent is NOT a confirmed exchange or permission to trade.

Constructed by reparsing a public Feed transaction plus timestamped observations;
never constructed from a saved `recognized_intent=true` flag. Snapshot provenance
must still come from the trusted runtime collector, not external user input.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import json

from .early_intent import Candidate, parse_candidates, timestamp
from .early_replay import evaluate_candidate
from .models import Signal


@dataclass(frozen=True)
class VerifiedFeedIntent:
    _candidate_json: str
    _snapshots_json: str
    verified_at: float

    @classmethod
    def verify(cls, tx, wallet, path, snapshots, at):
        matches = [c for c in parse_candidates(tx, wallet).candidates if c.path == path]
        if len(matches) != 1:
            raise ValueError("early intent path not unique or unsupported")
        intent = cls(json.dumps(asdict(matches[0]), sort_keys=True),
                     json.dumps(snapshots, sort_keys=True), timestamp(at))
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
        result = evaluate_candidate(candidate, at, json.loads(self._snapshots_json), enabled=False)
        required = ["freshness", "semantics", "attribution"]
        if candidate.route_kind == "relay_wrapper":
            required.append("deployment")
        for name in required:
            if result["checks"][name]["status"] != "pass":
                raise ValueError("early intent " + name + ": " + result["checks"][name].get("reason", "missing"))
        return result["attribution"]

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
                                "copy_operation_order_id": c.order_id,
                                "amount_basis": "order_payment" if c.side == "BUY" else "declared_sell",
                                "verification_at": self.verified_at})
