"""Bounded read-only access to Relay's public request lookup endpoint."""
from __future__ import annotations

import asyncio
import json
import urllib.parse
import urllib.request


class RelayApiError(RuntimeError):
    pass


class RelayNotReady(RelayApiError):
    pass


class RelayPublicClient:
    def __init__(self, endpoint: str = "https://api.relay.link", timeout: float = 10):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "https" or parsed.hostname != "api.relay.link"
                or parsed.path not in {"", "/"}):
            raise ValueError("Relay endpoint must be the official HTTPS API origin")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _check_hash(tx_hash: str) -> str:
        if (not isinstance(tx_hash, str) or len(tx_hash) != 66
                or not tx_hash.startswith("0x")):
            raise ValueError("invalid Relay transaction hash")
        try:
            bytes.fromhex(tx_hash[2:])
        except ValueError:
            raise ValueError("invalid Relay transaction hash") from None
        return tx_hash.lower()

    def _fetch(self, tx_hash: str, limit: int, *, field: str = "hash") -> dict:
        if field not in {"hash", "orderId", "id"}:
            raise ValueError("unsupported Relay lookup field")
        query = urllib.parse.urlencode({
            field: self._check_hash(tx_hash), "includeOrderData": "true",
            "limit": str(limit),
        })
        request = urllib.request.Request(
            f"{self.endpoint}/requests/v2?{query}",
            headers={"Accept": "application/json",
                     "User-Agent": "smart-money-observer/0.1"},
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                if urllib.parse.urlsplit(response.geturl()).hostname != "api.relay.link":
                    raise RelayApiError("Relay response redirected outside official origin")
                raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise RelayApiError("Relay response exceeds size limit")
            document = json.loads(raw)
        except RelayApiError:
            raise
        except Exception as exc:
            raise RelayApiError(f"Relay lookup failed: {type(exc).__name__}") from None
        requests = document.get("requests") if isinstance(document, dict) else None
        if not isinstance(requests, list):
            raise RelayApiError("Relay response has no requests list")
        if not requests:
            raise RelayNotReady("Relay request is not available yet")
        return document

    def _lookup(self, tx_hash: str) -> dict:
        document = self._fetch(tx_hash, 2)
        if len(document["requests"]) != 1:
            raise RelayApiError("Relay destination transaction is not unique")
        return document

    def _lookup_many(self, tx_hash: str, limit: int) -> dict:
        if not 1 <= limit <= 20:
            raise ValueError("Relay lookup limit out of range")
        return self._fetch(tx_hash, limit)

    async def lookup_by_destination_hash(self, tx_hash: str) -> dict:
        return await asyncio.to_thread(self._lookup, tx_hash)

    def _lookup_order(self, order_id: str, request_id: str | None) -> dict:
        order_id = self._check_hash(order_id)
        lookup_id = self._check_hash(request_id) if request_id is not None else order_id
        document = self._fetch(lookup_id, 2, field="id" if request_id is not None else "orderId")
        if len(document["requests"]) != 1:
            raise RelayApiError("Relay order is not unique")
        request = document["requests"][0]
        try:
            matches = (self._check_hash(request["protocol"]["orderId"]) == order_id
                       and (request_id is None
                            or self._check_hash(request["id"]) == lookup_id))
        except (KeyError, TypeError, ValueError):
            matches = False
        if not matches:
            raise RelayApiError("Relay order lookup identity mismatch")
        return document

    async def lookup_by_order(self, order_id: str, request_id: str | None = None) -> dict:
        """Lookup before destination inclusion; requestId and orderId are distinct.

        This only verifies lookup identity. Economic attribution remains the
        caller's responsibility; returned status is not proof of a purchase.
        """
        return await asyncio.to_thread(self._lookup_order, order_id, request_id)

    async def lookup_requests_by_hash(self, tx_hash: str, limit: int = 5) -> dict:
        """Return every request Relay attributes to one transaction hash.

        A bundled Robinhood-chain transaction can carry deposits of several
        users, so a source-side lookup is not unique by construction; the
        caller must select the request by user, token, amount and order id.
        """
        return await asyncio.to_thread(self._lookup_many, tx_hash, limit)
