"""Deliberately read-only RPC client. No signing or broadcast API is exposed."""
from __future__ import annotations

import asyncio
import itertools
import json
import time
from contextvars import ContextVar
from collections import deque
from urllib.parse import urlsplit

from .simulation_diagnostics import rpc_error_diagnostic
from .http_pool import JsonConnectionPool


ALLOWED_METHODS = frozenset({
    "eth_chainId", "eth_blockNumber", "eth_getCode", "eth_getTransactionByHash",
    "eth_getTransactionReceipt", "eth_getBlockByNumber", "eth_getBalance", "eth_call",
    "eth_getLogs", "eth_gasPrice", "eth_getTransactionCount",
    "debug_traceTransaction",
})


class RpcError(RuntimeError):
    def __init__(self, message: str, *, diagnostic: dict | None = None):
        super().__init__(message)
        self.diagnostic = diagnostic


class ReadOnlyRpc:
    def __init__(self, url: str, concurrency: int = 4, timeout: float = 10):
        parsed = urlsplit(url)
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}):
            raise ValueError("RPC must use HTTPS, except on localhost")
        self.url, self.timeout = url, timeout
        self.semaphore = asyncio.Semaphore(concurrency)
        self.ids = itertools.count(1)
        self._deadline = ContextVar("rpc_request_deadline", default=None)
        self.timings = deque(maxlen=128)
        self.transport = JsonConnectionPool(url, capacity=concurrency, timeout=timeout,
                                            max_bytes=16*1024*1024)

    def close(self):
        self.transport.close()

    def _request(self, method: str, params: list, request_id: int):
        body = json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}).encode()
        try:
            result = self.transport.request("POST", body=body, headers={
                "Content-Type": "application/json", "User-Agent": "smart-money-observer/0.1"},
                deadline=self._deadline.get())
            if result.get("id") != request_id:
                raise RpcError("RPC response id mismatch")
            if "error" in result:
                diagnostic = rpc_error_diagnostic(method, result["error"], self.url)
                code = diagnostic["code"]
                raise RpcError(
                    f"RPC {method} error code {code if code is not None else 'unknown'}",
                    diagnostic=diagnostic)
            if "result" not in result:
                raise RpcError("RPC result missing")
            return result["result"]
        except RpcError:
            raise
        except Exception as exc:
            # Do not leak provider credentials embedded in URLs or response bodies.
            raise RpcError(f"RPC transport failure: {type(exc).__name__}", diagnostic={
                "kind": "transport_or_response_error", "code": None,
                "exception_type": type(exc).__name__,
            }) from None

    async def call(self, method: str, params: list | None = None):
        if method not in ALLOWED_METHODS:
            raise PermissionError(f"RPC method not allowed: {method}")
        if method == "debug_traceTransaction":
            allowed = (
                {"tracer": "prestateTracer", "tracerConfig": {"diffMode": True}},
                {"tracer": "prestateTracer"},
            )
            if (not isinstance(params, list) or len(params) != 2
                    or not isinstance(params[0], str) or len(params[0]) != 66
                    or params[1] not in allowed):
                raise PermissionError("only bounded prestateTracer diffMode is allowed")
        started = time.monotonic()
        token = self._deadline.set(started + self.timeout)
        acquired = None
        try:
            try:
                await asyncio.wait_for(self.semaphore.acquire(), self.timeout)
            except asyncio.TimeoutError:
                raise RpcError("RPC concurrency wait timed out", diagnostic={
                    "kind": "transport_or_response_error", "code": None,
                    "exception_type": "TimeoutError",
                }) from None
            acquired = time.monotonic()
            return await asyncio.to_thread(self._request, method, params or [], next(self.ids))
        finally:
            if acquired is not None:
                self.semaphore.release()
            self._deadline.reset(token)
            self.timings.append(dict(method=method,
                queue_wait_ms=round(((acquired or time.monotonic())-started)*1000, 3),
                total_ms=round((time.monotonic()-started)*1000, 3)))

    async def receipt(self, tx_hash: str, attempts: int = 8, interval: float = 0.25):
        for attempt in range(attempts):
            result = await self.call("eth_getTransactionReceipt", [tx_hash])
            if result is not None:
                return result
            if attempt + 1 < attempts:
                await asyncio.sleep(interval)
        return None
