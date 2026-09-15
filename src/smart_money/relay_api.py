"""Bounded read-only access to Relay's public request lookup endpoint."""
from __future__ import annotations

import asyncio
import json
import http.client
import queue
import ssl
import time
from collections import deque
import urllib.parse


class RelayApiError(RuntimeError):
    pass


class RelayNotReady(RelayApiError):
    pass


class RelayPublicClient:
    def __init__(self, endpoint: str = "https://api.relay.link", timeout: float = 10):
        parsed = urllib.parse.urlsplit(endpoint)
        if (parsed.scheme != "https" or parsed.hostname != "api.relay.link"
                or parsed.path not in {"", "/"} or parsed.port not in {None, 443}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Relay endpoint must be the official HTTPS API origin")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self._connections = queue.LifoQueue(maxsize=4)
        for _ in range(4):
            self._connections.put(None)
        self._tls = ssl.create_default_context()
        self.timings = deque(maxlen=128)
        self._closed = False

    def close(self):
        self._closed = True
        slots = []
        while True:
            try:
                connection = self._connections.get_nowait()
            except queue.Empty:
                break
            if connection is not None:
                connection.close()
            slots.append(None)
        for slot in slots:
            self._connections.put_nowait(slot)

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
        if self._closed:
            raise RelayApiError("Relay client closed")
        if field not in {"hash", "orderId", "id"}:
            raise ValueError("unsupported Relay lookup field")
        query = urllib.parse.urlencode({
            field: self._check_hash(tx_hash), "includeOrderData": "true",
            "limit": str(limit),
        })
        connection = None
        started = time.perf_counter()
        try:
            connection = self._connections.get(timeout=self.timeout)
        except queue.Empty:
            raise RelayApiError("Relay connection pool busy") from None
        try:
            if connection is None:
                connection = http.client.HTTPSConnection(
                    "api.relay.link", timeout=self.timeout, context=self._tls)
            acquired = time.perf_counter()
            reused = connection.sock is not None
            if not reused:
                connection.connect()
            connected = time.perf_counter()
            # A connection belongs to exactly one worker until the body is read.
            # No redirects or hidden retries; broken connections are discarded.
            connection.request("GET", f"/requests/v2?{query}", headers={
                "Accept": "application/json", "User-Agent": "smart-money-observer/0.1"})
            response = connection.getresponse()
            received = time.perf_counter()
            if response.status != 200:
                raise RelayApiError("Relay HTTP status " + str(response.status))
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise RelayApiError("Relay response exceeds size limit")
            document = json.loads(raw)
            finished = time.perf_counter()
            self.timings.append(dict(reused=reused, status=response.status,
                pool_wait_ms=round((acquired-started)*1000, 3),
                connect_ms=round((connected-acquired)*1000, 3),
                response_ms=round((received-connected)*1000, 3),
                body_parse_ms=round((finished-received)*1000, 3),
                total_ms=round((finished-started)*1000, 3)))
        except RelayApiError:
            if connection is not None:
                connection.close()
                connection = None
            raise
        except Exception as exc:
            if connection is not None:
                connection.close()
                connection = None
            raise RelayApiError(f"Relay lookup failed: {type(exc).__name__}") from None
        finally:
            if self._closed and connection is not None:
                connection.close()
                connection = None
            self._connections.put_nowait(connection)
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
