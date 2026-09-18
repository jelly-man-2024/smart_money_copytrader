"""Bounded, exclusive HTTP/1.1 connections; no redirects or implicit retries.

The synchronous worker owns its slot through body parsing, including when its
async caller is cancelled. URLs, headers and response bodies are never logged.
"""
from __future__ import annotations

from collections import deque
import http.client
import json
import queue
import ssl
import time
from urllib.parse import urlsplit


class HttpPoolError(ValueError):
    def __init__(self, message, *, diagnostic=None):
        super().__init__(message)
        self.diagnostic = diagnostic


def safe_exception_type(exc):
    # Never expose arbitrary exception messages (which may contain credentials).
    known = {"TimeoutError", "OSError", "ConnectionError", "ConnectionResetError",
             "ConnectionRefusedError", "BrokenPipeError", "RemoteDisconnected",
             "ResponseNotReady", "CannotSendRequest", "BadStatusLine", "IncompleteRead",
             "SSLError", "SSLCertVerificationError", "JSONDecodeError", "UnicodeDecodeError",
             "ValueError", "PermissionError", "HttpPoolError"}
    name = type(exc).__name__
    return name if name in known else "Exception"


# Transport failures that mean a reused keep-alive connection was closed by the
# server before the exchange completed (the request most likely never reached it).
# Retrying ONCE on a fresh connection is safe only for idempotent requests
# (reads/quotes) — never for eth_sendRawTransaction, which could double-broadcast.
_RETRYABLE_STALE_REUSE = frozenset({
    "RemoteDisconnected", "ConnectionResetError", "ConnectionError",
    "BrokenPipeError", "ResponseNotReady", "CannotSendRequest", "BadStatusLine",
})


class JsonConnectionPool:
    def __init__(self, endpoint, *, capacity=4, timeout=10, max_bytes=2*1024*1024,
                 idle_reuse_timeout=5.0):
        url = urlsplit(endpoint)
        if (not url.hostname or url.username or url.password or url.fragment
                or (url.scheme != "https" and not
                    (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}))):
            raise ValueError("invalid HTTP pool endpoint")
        if type(capacity) is not int or not 1 <= capacity <= 16 or not 0 < timeout <= 30:
            raise ValueError("invalid HTTP pool bounds")
        self.url, self.timeout, self.max_bytes = url, timeout, max_bytes
        self._idle_reuse_timeout = idle_reuse_timeout
        self._tls = ssl.create_default_context()
        self._slots = queue.LifoQueue(capacity)
        for _ in range(capacity):
            self._slots.put(None)
        self._closed = False
        self.timings = deque(maxlen=128)

    def close(self):
        self._closed = True
        slots = []
        while True:
            try:
                connection = self._slots.get_nowait()
            except queue.Empty:
                break
            if connection is not None:
                connection.close()
            slots.append(None)
        for item in slots:
            self._slots.put_nowait(item)

    def warm(self) -> bool:
        """Open a connection without sending anything.

        A first send otherwise pays the TLS handshake inside whatever deadline
        the caller is holding. This only establishes the socket: it exposes no
        request, and a failure here is not the caller's problem, because the
        real request will surface it.
        """
        if self._closed:
            return False
        try:
            connection = self._slots.get(timeout=self.timeout)
        except queue.Empty:
            return False
        try:
            if connection is None or connection.sock is None:
                cls = (http.client.HTTPSConnection if self.url.scheme == "https"
                       else http.client.HTTPConnection)
                kwargs = {"context": self._tls} if self.url.scheme == "https" else {}
                connection = cls(self.url.hostname, self.url.port,
                                 timeout=self.timeout, **kwargs)
                connection.connect()
            connection._smcopy_last_used = time.monotonic()
            return True
        except Exception:
            if connection is not None:
                connection.close()
            connection = None
            return False
        finally:
            self._slots.put_nowait(connection)

    def request(self, method="GET", *, path=None, body=None, headers=None,
                before_send=None, deadline=None, idempotent=False):
        """One HTTP call. With idempotent=True a single stale-reused-connection
        transport failure is retried once on a fresh connection. Never enable it
        for a non-idempotent request (e.g. eth_sendRawTransaction): a retry there
        could broadcast the same signed transaction twice."""
        try:
            return self._request_once(method, path=path, body=body, headers=headers,
                                      before_send=before_send, deadline=deadline)
        except HttpPoolError as exc:
            diag = exc.diagnostic or {}
            if (idempotent and diag.get("reused") is True
                    and diag.get("reason") == "request_failed"
                    and diag.get("exception_type") in _RETRYABLE_STALE_REUSE):
                # The stale connection was already discarded on failure, so this
                # second attempt establishes a fresh one within the same deadline.
                return self._request_once(method, path=path, body=body, headers=headers,
                                          before_send=before_send, deadline=deadline)
            raise

    def _request_once(self, method="GET", *, path=None, body=None, headers=None,
                      before_send=None, deadline=None):
        start = time.monotonic()
        end = min(start + self.timeout, deadline) if deadline is not None else start + self.timeout
        phase, reused, status = "pool_wait", None, None
        failure = None
        def diagnostic(reason, exception_type="HttpPoolError"):
            return dict(reason=reason, exception_type=exception_type, phase=phase,
                        reused=reused, status=status)
        def fail(message, reason):
            return HttpPoolError(message, diagnostic=diagnostic(reason))
        def remaining():
            left = end - time.monotonic()
            if left <= 0:
                raise fail("HTTP request deadline exceeded", "deadline_exceeded")
            return left
        if self._closed:
            raise fail("HTTP pool closed", "pool_closed")
        target = path if path is not None else (self.url.path or "/") + (
            "?" + self.url.query if self.url.query else "")
        if not target.startswith("/") or target.startswith("//") or "\r" in target or "\n" in target:
            raise fail("invalid HTTP request path", "invalid_path")
        try:
            connection = self._slots.get(timeout=remaining())
        except queue.Empty:
            exc = fail("HTTP connection pool busy", "pool_busy")
            finished = time.monotonic()
            self.timings.append(dict(reused=None, success=False, status=None,
                failure=exc.diagnostic,
                pool_wait_ms=round((finished-start)*1000, 3),
                connect_ms=0.0, response_ms=0.0, body_parse_ms=0.0,
                total_ms=round((finished-start)*1000, 3)))
            raise exc from None
        acquired = time.monotonic()
        reused = connection is not None and connection.sock is not None
        if reused and acquired - getattr(connection, "_smcopy_last_used", acquired) > self._idle_reuse_timeout:
            # Proactively drop a connection idle longer than the provider is likely
            # to keep alive, so we never reuse one it has already closed. This is
            # what causes RemoteDisconnected, which only ever hits reused stale
            # connections; a fresh connection is opened below instead.
            connection.close()
            connection = None
            reused = False
        status, connected, received = None, acquired, acquired
        ok = False
        try:
            phase = "connect"
            if self._closed:
                raise fail("HTTP pool closed", "pool_closed")
            if connection is None:
                cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
                kwargs = {"context": self._tls} if self.url.scheme == "https" else {}
                connection = cls(self.url.hostname, self.url.port, timeout=remaining(), **kwargs)
            if connection.sock is None:
                connection.connect()
            connected = time.monotonic()
            connection.sock.settimeout(remaining())
            phase = "before_send"
            if before_send is not None:
                before_send()
            remaining()
            phase = "request"
            connection.request(method, target, body=body, headers=headers or {})
            connection.sock.settimeout(remaining())
            phase = "response_headers"
            response = connection.getresponse()
            received, status = time.monotonic(), response.status
            if status != 200:
                raise fail(f"HTTP status {status}", "http_status")
            phase = "response_body"
            chunks, size = [], 0
            while True:
                if connection.sock is not None:
                    connection.sock.settimeout(remaining())
                chunk = response.read1(min(65536, self.max_bytes + 1 - size))
                remaining()
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > self.max_bytes:
                    raise fail("HTTP response exceeds size limit", "response_size_limit")
            if getattr(response, "length", None) not in (None, 0):
                raise fail("HTTP response body truncated", "response_truncated")
            # Python 3.10 read1() reaches Content-Length zero without closing the
            # response file. getresponse() otherwise sees the previous response
            # as active and raises ResponseNotReady on the next use of the socket.
            response.close()
            phase = "parse_json"
            result = json.loads(b"".join(chunks))
            remaining()
            ok = True
            if response.will_close:
                connection.close()
                connection = None
            else:
                connection._smcopy_last_used = time.monotonic()
            return result
        except HttpPoolError as exc:
            failure = exc.diagnostic
            raise
        except Exception as exc:
            failure = diagnostic("request_failed", safe_exception_type(exc))
            raise HttpPoolError(f"HTTP request failed: {failure['exception_type']}",
                                diagnostic=failure) from None
        finally:
            finished = time.monotonic()
            self.timings.append(dict(reused=reused, success=ok, status=status,
                failure=failure,
                pool_wait_ms=round((acquired-start)*1000, 3),
                connect_ms=round((connected-acquired)*1000, 3),
                response_ms=round((received-connected)*1000, 3),
                body_parse_ms=round((finished-received)*1000, 3),
                total_ms=round((finished-start)*1000, 3)))
            if connection is not None and (not ok or self._closed):
                connection.close()
                connection = None
            self._slots.put_nowait(connection)
