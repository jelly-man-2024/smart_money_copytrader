"""Narrow mainnet broadcaster kept separate from the read-only RPC client."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import math
import time
import urllib.parse
from .http_pool import JsonConnectionPool

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak
from hexbytes import HexBytes

from .execution_controls import require_mainnet_broadcast_enabled, _stop_controls
from .models import address
from .registry import CHAIN_ID, chain_for
from .preflight_ticket import fingerprint
from eth_utils import to_checksum_address


@dataclass(frozen=True)
class BroadcastResult:
    proposal_id: str
    tx_hash: str
    submitted: bool


class MainnetBroadcaster:
    """Expose only eth_sendRawTransaction after all live controls are rechecked."""

    def __init__(self, endpoint: str | None = None, timeout: float = 10.0,
                 chain_id: int = CHAIN_ID):
        # One broadcaster serves exactly one chain: its endpoint and the chain it
        # accepts signed transactions for are fixed together, so a transaction can
        # never be sent to the wrong chain's RPC.
        self.chain = chain_for(chain_id)
        self.chain_id = self.chain.chain_id
        self.endpoint = endpoint or os.environ.get(self.chain.rpc_env)
        parsed = urllib.parse.urlparse(self.endpoint or "")
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("mainnet broadcaster requires an HTTPS RPC endpoint")
        if not isinstance(timeout, (int, float)) or not 1 <= timeout <= 30:
            raise ValueError("invalid broadcast timeout")
        self.timeout = float(timeout)
        self.transport = JsonConnectionPool(self.endpoint, capacity=1, timeout=self.timeout,
                                            max_bytes=1024*1024)

    def warm(self) -> bool:
        """Best-effort: establish the connection before a send needs it."""
        return self.transport.warm()

    def close(self):
        self.transport.close()

    def _request(self, raw_hex: str, before_send=None) -> str:
        body = json.dumps({
            "jsonrpc": "2.0", "id": 1, "method": "eth_sendRawTransaction",
            "params": [raw_hex],
        }, separators=(",", ":")).encode()
        try:
            document = self.transport.request("POST", body=body, before_send=before_send,
                headers={"Content-Type": "application/json",
                         "User-Agent": "smart-money-copytrader/0.1"})
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
                        relationship_id: str, config_snapshot_hash: str,
                        early_trial_check=None) -> BroadcastResult:
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
        if recovered != address(follower_wallet) or chain_id != self.chain_id:
            raise ValueError("signed transaction sender or chain mismatch")
        require_mainnet_broadcast_enabled(
            follower_wallet, relationship_id, config_snapshot_hash)
        if "early_trial_id" in review.evidence:
            expires = review.evidence.get("early_trial_expires_at")
            if (isinstance(expires, bool) or not isinstance(expires, (int, float))
                    or not math.isfinite(expires) or time.time() >= expires):
                raise ValueError("early trial expired before broadcast")
            if not callable(early_trial_check) or early_trial_check() is not True:
                raise ValueError("early trial send fence not verified")
            if time.time() >= expires:
                raise ValueError("early trial expired during final check")
        # A database gate can take time after review. Never reset the market
        # clock to the review time; approval reviews have no market quote.
        if "quote_max_age_seconds" in review.evidence:
            max_age = review.evidence["quote_max_age_seconds"]
            now = time.time()
            if (isinstance(max_age, bool) or not isinstance(max_age, (int, float))
                    or not math.isfinite(max_age) or not 0 < max_age <= 60):
                raise ValueError("invalid broadcast quote age limit")
            for name in ("quote", "reference_quote"):
                quote = review.evidence.get(name)
                observed = quote.get("observed_at") if isinstance(quote, dict) else None
                if (isinstance(observed, bool) or not isinstance(observed, (int, float))
                        or not math.isfinite(observed) or not 0 <= now - observed <= max_age):
                    raise ValueError("market quote expired before broadcast")
        ticket = getattr(review, "ticket", None)
        if ticket is not None:
            signable = {k: decoded[k] for k in ("chainId", "nonce", "value", "gas",
                        "maxFeePerGas", "maxPriorityFeePerGas", "type")}
            signable.update(to=to_checksum_address(bytes(decoded["to"])),
                            data="0x" + bytes(decoded["data"]).hex())
            if (fingerprint(signable) != ticket.transaction_hash or ticket.follower != recovered
                    or ticket.relationship != relationship_id or ticket.snapshot != config_snapshot_hash
                    or ticket.proposal_id != review.proposal_id):
                raise ValueError("broadcast preflight ticket binding mismatch")
            def before_send():
                _stop_controls()
                ticket.assert_fresh()
                now = time.time()
                for q in (ticket.quote, ticket.reference):
                    if not 0 <= now-q.observed_at <= review.evidence["quote_max_age_seconds"]:
                        raise ValueError("quote expired while waiting for broadcast connection")
                for key in ("early_feed_expires_at", "early_trial_expires_at"):
                    if key in review.evidence and now >= review.evidence[key]:
                        raise ValueError("early execution expired while waiting to send")
                ticket.claim_send()
            returned_hash = await asyncio.to_thread(self._request, "0x"+raw_transaction.hex(), before_send)
        else:
            returned_hash = await asyncio.to_thread(self._request, "0x" + raw_transaction.hex())
        if returned_hash != local_hash:
            raise RuntimeError("broadcast RPC returned a different transaction hash")
        return BroadcastResult(review.proposal_id, local_hash, True)
