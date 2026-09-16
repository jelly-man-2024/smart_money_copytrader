"""Short-lived process-local evidence, never reconstructed from ledger JSON."""
from dataclasses import dataclass, field
import hashlib
import json
import os
import time
import threading


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class PreflightTicket:
    proposal_id: str
    follower: str
    relationship: str
    snapshot: str
    transaction_hash: str
    plan_hash: str
    signal_hash: str
    quote: object
    reference: object
    gas_price: str
    policy_hash: str
    evidence_json: str
    started_monotonic: float
    pid: int
    deadline: int
    window_seconds: float = 2.0
    _used: bool = field(default=False, init=False, repr=False, compare=False)
    _lock: object = field(default_factory=threading.Lock, init=False, repr=False, compare=False)

    def claim_send(self):
        with self._lock:
            self.assert_fresh()
            if self._used:
                raise ValueError("preflight ticket already submitted")
            object.__setattr__(self, "_used", True)

    def assert_fresh(self):
        age = time.monotonic() - self.started_monotonic
        if (os.getpid() != self.pid or self.window_seconds != 2.0
                or not 0 <= age <= self.window_seconds or time.time() >= self.deadline):
            raise ValueError("final preflight ticket expired or belongs to another process")

    def validate(self, row, signal, policy, *, allow_expired=False):
        from dataclasses import asdict
        if not allow_expired:
            self.assert_fresh()
        if self._used or os.getpid() != self.pid:
            raise ValueError("preflight ticket is consumed or belongs to another process")
        if (row["proposal_id"] != self.proposal_id or row["follower_wallet"] != self.follower
                or row["relationship_id"] != self.relationship
                or row["config_snapshot_hash"] != self.snapshot
                or fingerprint(row["transaction"]) != self.transaction_hash
                or fingerprint(row["unsigned_plan"]) != self.plan_hash
                or row["unsigned_plan"]["deadline"] != self.deadline
                or fingerprint(signal.to_dict()) != self.signal_hash
                or fingerprint(asdict(policy)) != self.policy_hash):
            raise ValueError("final preflight ticket binding mismatch")
        return json.loads(self.evidence_json)
