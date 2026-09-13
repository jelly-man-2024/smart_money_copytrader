"""Narrow mainnet broadcaster kept separate from the read-only RPC client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import urllib.parse
import urllib.request

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes

from .execution_controls import require_mainnet_broadcast_enabled
from .models import address
from .registry import CHAIN_ID


@dataclass(frozen=True)
class BroadcastResult:
    proposal_id: str
    tx_hash: str
    submitted: bool


class MainnetBroadcaster:
    """Expose only eth_sendRawTransaction after all live controls are rechecked."""

    def __init__(self, endpoint: str | None = None, timeout: float = 10.0):
        self.endpoint = endpoint or os.environ.get("ROBINHOOD_RPC_URL")
        parsed = urllib.parse.urlparse(self.endpoint or "")
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("mainnet broadcaster requires an HTTPS RPC endpoint")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
            raise ValueError("invalid broadcast timeout")
        self.timeout = float(timeout)

    def _request(self, raw_hex: str) -> str:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
            "params": [raw_hex],
        }, separators=(",", ":")).encode()
        request = urllib.request.Request(
            self.endpoint, data=body,
            headers={"Content-Type": "application/json",
                     "User-Agent": "smart-money-copytrader/0.1"},
            method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError("broadcast RPC response limit")
            document = json.loads(raw)
        except ValueError:
            raise
        except Exception as exc:
            raise RuntimeError(
                f"broadcast RPC transport failure: {type(exc).__name__}") from None
        if (not isinstance(document, dict) or document.get("jsonrpc") != "2.0"
                or document.get("id") != 1 or "error" in document
                or not isinstance(document.get("result"), str)):
            raise RuntimeError("broadcast RPC rejected transaction")
        return document["result"].lower()

    async def broadcast(self, review, raw_transaction: bytes, *, follower_wallet: str,
                        relationship_id: str, config_snapshot_hash: str) -> BroadcastResult:
        if (not isinstance(raw_transaction, bytes) or not raw_transaction
                or len(raw_transaction) > 1024 * 1024):
            raise ValueError("invalid signed transaction bytes")
        local_hash = "0x" + keccak(raw_transaction).hex()
        if (review.proposal_id is None or review.signed_tx_hash != local_hash
                or review.evidence.get("broadcast_performed") is not False):
            raise ValueError("broadcast review does not match signed transaction")
        try:
            recovered = address(Account.recover_transaction(raw_transaction))
            decoded = TypedTransaction.from_bytes(HexBytes(raw_transaction)).as_dict()
            chain_id = int(decoded["chainId"])
        except Exception:
            raise ValueError("signed transaction cannot be independently verified") from None
        if recovered != address(follower_wallet) or chain_id != CHAIN_ID:
            raise ValueError("signed transaction sender or chain mismatch")
        require_mainnet_broadcast_enabled(
            follower_wallet, relationship_id, config_snapshot_hash)
        returned_hash = await asyncio.to_thread(
            self._request, "0x" + raw_transaction.hex())
        if returned_hash != local_hash:
            raise RuntimeError("broadcast RPC returned a different transaction hash")
        return BroadcastResult(review.proposal_id, local_hash, True)
