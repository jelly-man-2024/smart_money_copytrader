"""Bounded Nitro feed decoding, independent of RPC and execution logic."""
from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import time

from eth_account import Account
from eth_utils import keccak
import rlp

from .models import Transaction, address
from .registry import CHAIN_ID

MAX_PAYLOAD = 8 * 1024 * 1024
MAX_TRANSACTIONS = 10000


class DecodeError(ValueError):
    pass


def signed_transactions(payload: bytes) -> list[bytes]:
    if len(payload) > MAX_PAYLOAD:
        raise DecodeError("oversize Nitro message")
    result = []

    def walk(buf: bytes, depth: int) -> None:
        if depth > 16:
            raise DecodeError("Nitro nesting limit")
        if not buf:
            raise DecodeError("empty Nitro message")
        if buf[0] == 4:
            if len(buf) == 1:
                raise DecodeError("empty signed transaction")
            result.append(buf[1:])
            if len(result) > MAX_TRANSACTIONS:
                raise DecodeError("Nitro transaction limit")
        elif buf[0] == 3:
            offset = 1
            while offset < len(buf):
                if offset + 8 > len(buf):
                    raise DecodeError("truncated Nitro length")
                size = int.from_bytes(buf[offset:offset + 8], "big")
                offset += 8
                if not size or offset + size > len(buf):
                    raise DecodeError("invalid Nitro child length")
                walk(buf[offset:offset + size], depth + 1)
                offset += size
        # Unsigned/system L2 message kinds are outside the supported wallet scope.

    walk(payload, 0)
    return result


def decode_raw(raw: bytes, **metadata) -> Transaction:
    try:
        kind = raw[0]
        if kind in (1, 2, 4):
            fields = rlp.decode(raw[1:], strict=True)
            expected = {1: 11, 2: 12, 4: 13}[kind]
            if len(fields) != expected:
                raise DecodeError("incorrect typed transaction field count")
            chain_id = int.from_bytes(fields[0], "big")
            nonce = int.from_bytes(fields[1], "big")
            destination, amount, calldata = fields[4:7] if kind == 1 else fields[5:8]
        elif kind >= 0xC0:
            fields = rlp.decode(raw, strict=True)
            if len(fields) != 9:
                raise DecodeError("incorrect legacy field count")
            v = int.from_bytes(fields[6], "big")
            if v < 35:
                raise DecodeError("unprotected legacy transaction is outside scope")
            chain_id = (v - 35) // 2
            nonce = int.from_bytes(fields[0], "big")
            destination, amount, calldata = fields[3:6]
            kind = 0
        else:
            raise DecodeError(f"unsupported transaction type {kind}")
        if chain_id != CHAIN_ID:
            raise DecodeError("wrong chain id")
        return Transaction(
            hash="0x" + keccak(raw).hex(),
            sender=address(Account.recover_transaction(raw)),
            to=address("0x" + destination.hex()) if destination else None,
            data=calldata, value=int.from_bytes(amount, "big"), chain_id=chain_id,
            nonce=nonce, tx_type=kind, **metadata,
        )
    except DecodeError:
        raise
    except Exception as exc:
        raise DecodeError("invalid signed transaction") from exc


@dataclass
class FeedHealth:
    max_age_seconds: float = 3.0
    max_silence_seconds: float = 5.0
    last_sequence: int | None = None
    last_timestamp: int | None = None
    last_received: float | None = None
    gap: bool = False

    def reset(self) -> None:
        self.last_sequence = self.last_timestamp = self.last_received = None
        self.gap = False

    def observe(self, sequence: int, timestamp: int, now: float) -> bool:
        if self.last_sequence is not None:
            if sequence <= self.last_sequence:
                return False
            if sequence != self.last_sequence + 1:
                self.gap = True
        self.last_sequence, self.last_timestamp, self.last_received = sequence, timestamp, now
        return self.healthy(now)

    def healthy(self, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        return bool(
            not self.gap and self.last_timestamp is not None and self.last_received is not None
            and -1.0 <= now - self.last_timestamp <= self.max_age_seconds
            and 0 <= now - self.last_received <= self.max_silence_seconds
        )


def envelopes(frame: str | bytes, health: FeedHealth, now: float | None = None):
    """Yield metadata and raw transactions. Confirm-only frames are harmless."""
    now = time.time() if now is None else now
    if len(frame) > MAX_PAYLOAD * 2:
        raise DecodeError("oversize feed frame")
    try:
        doc = json.loads(frame)
        if doc.get("version") != 1:
            raise DecodeError("unsupported feed version")
        messages = doc.get("messages", [])
        if not isinstance(messages, list) or len(messages) > 4096:
            raise DecodeError("invalid feed message collection")
        for item in messages:
            sequence = item["sequenceNumber"]
            message = item["message"]["message"]
            timestamp = message["header"]["timestamp"]
            if not isinstance(sequence, int) or not isinstance(timestamp, int):
                raise DecodeError("invalid feed metadata")
            # Replay/duplicates are ignored, including their entire payload.
            if health.last_sequence is not None and sequence <= health.last_sequence:
                continue
            fresh = health.observe(sequence, timestamp, now)
            if not message.get("l2Msg"):
                continue
            payload = base64.b64decode(message["l2Msg"], validate=True)
            for raw in signed_transactions(payload):
                yield raw, {"sequence": sequence, "timestamp": timestamp, "received_at": now, "fresh": fresh}
    except DecodeError:
        raise
    except Exception as exc:
        raise DecodeError("malformed feed envelope") from exc
