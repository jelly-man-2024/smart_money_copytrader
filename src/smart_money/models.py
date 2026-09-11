from __future__ import annotations

from dataclasses import asdict, dataclass, field
import re


def address(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{40}", value):
        raise ValueError("invalid EVM address")
    return value.lower()


def number(value: int | str) -> int:
    return int(value, 16) if isinstance(value, str) and value.startswith("0x") else int(value)


@dataclass(frozen=True)
class Transaction:
    hash: str
    sender: str
    to: str | None
    data: bytes
    value: int = 0
    chain_id: int = 4663
    nonce: int = 0
    tx_type: int = 2
    sequence: int | None = None
    timestamp: int | None = None
    received_at: float | None = None
    fresh: bool = False
    observation_source: str = "unknown"

    @classmethod
    def from_rpc(cls, tx: dict, observation_source: str = "fixture") -> "Transaction":
        return cls(
            hash=tx["hash"].lower(), sender=address(tx["from"]),
            to=address(tx["to"]) if tx.get("to") else None,
            data=bytes.fromhex(tx.get("input", "0x")[2:]),
            value=number(tx.get("value", 0)), chain_id=number(tx.get("chainId", 4663)),
            nonce=number(tx.get("nonce", 0)), tx_type=number(tx.get("type", 0)),
            timestamp=tx.get("_timestamp"), observation_source=observation_source,
        )


@dataclass
class Signal:
    tx_hash: str
    wallet: str
    mode: str
    behavior: str
    path: str
    contract: str | None
    selector: str
    stage: str = "intent"
    intent_status: str = "observed"
    execution_status: str = "pending"
    canonical_status: str = "unconfirmed"
    userop_index: int | None = None
    userop_nonce: str | None = None
    token_in: str | None = None
    token_out: str | None = None
    amount_in_raw: str | None = None
    amount_out_raw: str | None = None
    amount_limit_raw: str | None = None
    recipient: str | None = None
    protocol: str | None = None
    pool_id: str | None = None
    exact_in: bool | None = None
    execution_success: bool | None = None
    fresh: bool = False
    reasons: list[str] = field(default_factory=list)
    evidence: dict = field(default_factory=dict)
    copy_eligible: bool = False

    @property
    def event_id(self) -> str:
        return f"4663:{self.tx_hash}:{self.wallet}:{self.path}"

    def to_dict(self) -> dict:
        return {"event_id": self.event_id, **asdict(self)}
