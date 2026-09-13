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

    def _lookup(self, tx_hash: str) -> dict:
        if (not isinstance(tx_hash, str) or len(tx_hash) != 66
                or not tx_hash.startswith("0x")):
            raise ValueError("invalid Relay destination transaction hash")
        try:
            bytes.fromhex(tx_hash[2:])
        except ValueError:
            raise ValueError("invalid Relay destination transaction hash") from None
        query = urllib.parse.urlencode({
            "hash": tx_hash.lower(), "includeOrderData": "true", "limit": "2",
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
        if len(requests) != 1:
            raise RelayApiError("Relay destination transaction is not unique")
        return document

    async def lookup_by_destination_hash(self, tx_hash: str) -> dict:
        return await asyncio.to_thread(self._lookup, tx_hash)
