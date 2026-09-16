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
    pass


class JsonConnectionPool:
    def __init__(self, endpoint, *, capacity=4, timeout=10, max_bytes=2*1024*1024):
        url = urlsplit(endpoint)
        if (not url.hostname or url.username or url.password or url.fragment
                or (url.scheme != "https" and not
                    (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1"}))):
            raise ValueError("invalid HTTP pool endpoint")
        if type(capacity) is not int or not 1 <= capacity <= 16 or not 0 < timeout <= 30:
            raise ValueError("invalid HTTP pool bounds")
        self.url, self.timeout, self.max_bytes = url, timeout, max_bytes
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

    def request(self, method="GET", *, path=None, body=None, headers=None,
                before_send=None, deadline=None):
        start = time.monotonic()
        end = min(start + self.timeout, deadline) if deadline is not None else start + self.timeout
        def remaining():
            left = end - time.monotonic()
            if left <= 0:
                raise HttpPoolError("HTTP request deadline exceeded")
            return left
        if self._closed:
            raise HttpPoolError("HTTP pool closed")
        target = path if path is not None else (self.url.path or "/") + (
            "?" + self.url.query if self.url.query else "")
        if not target.startswith("/") or target.startswith("//") or "\r" in target or "\n" in target:
            raise HttpPoolError("invalid HTTP request path")
        try:
            connection = self._slots.get(timeout=remaining())
        except queue.Empty:
            raise HttpPoolError("HTTP connection pool busy") from None
        acquired = time.monotonic()
        reused = connection is not None and connection.sock is not None
        status, connected, received = None, acquired, acquired
        ok = False
        try:
            if self._closed:
                raise HttpPoolError("HTTP pool closed")
            if connection is None:
                cls = http.client.HTTPSConnection if self.url.scheme == "https" else http.client.HTTPConnection
                kwargs = {"context": self._tls} if self.url.scheme == "https" else {}
                connection = cls(self.url.hostname, self.url.port, timeout=remaining(), **kwargs)
            if connection.sock is None:
                connection.connect()
            connected = time.monotonic()
            connection.sock.settimeout(remaining())
            if before_send is not None:
                before_send()
            remaining()
            connection.request(method, target, body=body, headers=headers or {})
            connection.sock.settimeout(remaining())
            response = connection.getresponse()
            received, status = time.monotonic(), response.status
            if status != 200:
                raise HttpPoolError(f"HTTP status {status}")
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
                    raise HttpPoolError("HTTP response exceeds size limit")
            if getattr(response, "length", None) not in (None, 0):
                raise HttpPoolError("HTTP response body truncated")
            # Python 3.10 read1() reaches Content-Length zero without closing the
            # response file. getresponse() otherwise sees the previous response
            # as active and raises ResponseNotReady on the next use of the socket.
            response.close()
            result = json.loads(b"".join(chunks))
            remaining()
            ok = True
            if response.will_close:
                connection.close()
                connection = None
            return result
        except HttpPoolError:
            raise
        except Exception as exc:
            raise HttpPoolError(f"HTTP request failed: {type(exc).__name__}") from None
        finally:
            finished = time.monotonic()
            self.timings.append(dict(reused=reused, success=ok, status=status,
                pool_wait_ms=round((acquired-start)*1000, 3),
                connect_ms=round((connected-acquired)*1000, 3),
                response_ms=round((received-connected)*1000, 3),
                body_parse_ms=round((finished-received)*1000, 3),
                total_ms=round((finished-start)*1000, 3)))
            if connection is not None and (not ok or self._closed):
                connection.close()
                connection = None
            self._slots.put_nowait(connection)
