"""No sockets: exercise transport ownership/limits with deterministic fake connections."""
import asyncio
import json
import threading
import io
import http.client
import unittest
from unittest.mock import Mock, patch

from smart_money.http_pool import JsonConnectionPool, HttpPoolError


class Response:
    status = 200
    will_close = False
    def __init__(self, body=b'{"ok":true}'):
        self.body = body
    def read1(self, count):
        chunk, self.body = self.body[:count], self.body[count:]
        return chunk
    def close(self): pass


class Connection:
    def __init__(self, *args, **kwargs):
        self.sock = None
        self.requests = 0
        self.responses = []
        self.closed = False
    def connect(self): self.sock = Mock()
    def request(self, *args, **kwargs): self.requests += 1
    def getresponse(self): return self.responses.pop(0) if self.responses else Response()
    def close(self): self.closed, self.sock = True, None


class PoolTests(unittest.TestCase):
    def test_failure_diagnostics_keep_phase_reuse_and_original_type_not_secrets(self):
        for phase, error in [("connect", TimeoutError("secret-url")),
                             ("response_headers", http.client.RemoteDisconnected("secret-url")),
                             ("response_body", ConnectionResetError("secret-url"))]:
            with self.subTest(phase=phase):
                connection = Connection()
                with patch('http.client.HTTPSConnection', return_value=connection):
                    pool = JsonConnectionPool('https://example.invalid/secret-url', capacity=1)
                    self.addCleanup(pool.close)
                    if phase == "connect":
                        connection.connect = Mock(side_effect=error)
                    else:
                        pool.request()
                        if phase == "response_headers":
                            connection.getresponse = Mock(side_effect=error)
                        else:
                            response = Response()
                            response.read1 = Mock(side_effect=error)
                            connection.responses = [response]
                    with self.assertRaises(HttpPoolError) as caught:
                        pool.request()
                    diagnostic = caught.exception.diagnostic
                    self.assertEqual(diagnostic["phase"], phase)
                    self.assertEqual(diagnostic["exception_type"], type(error).__name__)
                    self.assertEqual(diagnostic["reused"], phase != "connect")
                    self.assertNotIn("secret", json.dumps(diagnostic))
                    self.assertEqual(pool.timings[-1]["failure"], diagnostic)
                    self.assertTrue(connection.closed)

    def test_http_status_and_pool_wait_have_structured_diagnostics(self):
        connection = Connection()
        response = Response()
        response.status = 429
        connection.responses = [response]
        with patch('http.client.HTTPSConnection', return_value=connection):
            pool = JsonConnectionPool('https://example.invalid', capacity=1, timeout=.01)
            self.addCleanup(pool.close)
            with self.assertRaises(HttpPoolError) as caught:
                pool.request()
            self.assertEqual(caught.exception.diagnostic["reason"], "http_status")
            self.assertEqual(caught.exception.diagnostic["status"], 429)
            slot = pool._slots.get()
            try:
                with self.assertRaises(HttpPoolError) as caught:
                    pool.request()
                self.assertEqual(caught.exception.diagnostic["phase"], "pool_wait")
                self.assertIsNone(caught.exception.diagnostic["reused"])
                self.assertEqual(pool.timings[-1]["failure"], caught.exception.diagnostic)
                self.assertFalse(pool.timings[-1]["success"])
                self.assertGreaterEqual(pool.timings[-1]["pool_wait_ms"], 0)
            finally:
                pool._slots.put(slot)

    def test_real_httpresponse_content_length_is_released_before_reuse(self):
        class Socket:
            def settimeout(self, value): pass
            def sendall(self, data): pass
            def close(self): pass
            def makefile(self, *args):
                return io.BytesIO(b'HTTP/1.1 200 OK\r\nContent-Length: 11\r\n\r\n{"ok":true}')
        class RealConnection(http.client.HTTPConnection):
            def connect(self): self.sock = Socket()
        with patch('http.client.HTTPSConnection', side_effect=lambda *args, **kwargs: RealConnection('example.invalid')) as factory:
            pool = JsonConnectionPool('https://example.invalid', capacity=1)
            try:
                self.assertEqual(pool.request(), {'ok': True})
                self.assertEqual(pool.request(), {'ok': True})
                factory.assert_called_once()
            finally:
                pool.close()

    def test_reuses_connection_and_closes_on_shutdown(self):
        connection = Connection()
        with patch('http.client.HTTPSConnection', return_value=connection) as factory:
            pool = JsonConnectionPool('https://example.invalid/secret', capacity=1)
            self.assertEqual(pool.request(), {'ok': True})
            self.assertEqual(pool.request(), {'ok': True})
            factory.assert_called_once()
            self.assertEqual([r['reused'] for r in pool.timings], [False, True])
            self.assertNotIn('secret', json.dumps(list(pool.timings)))
            pool.close()
            self.assertTrue(connection.closed)
            with self.assertRaises(HttpPoolError): pool.request()

    def test_bad_status_parse_size_disconnect_discard_without_retry(self):
        bad_status = Response()
        bad_status.status = 302
        for response in [bad_status, Response(b'no json'), Response(b'x'*20)]:
            bad, good = Connection(), Connection()
            bad.responses = [response]
            with patch('http.client.HTTPSConnection', side_effect=[bad, good]) as factory:
                pool = JsonConnectionPool('https://example.invalid', capacity=1, max_bytes=16)
                with self.assertRaises(HttpPoolError): pool.request()
                self.assertEqual(factory.call_count, 1)
                self.assertTrue(bad.closed)
                self.assertEqual(pool.request(), {'ok': True})
                pool.close()

    def test_socket_failure_does_not_retry_same_request(self):
        connection = Connection()
        connection.request = Mock(side_effect=OSError('do-not-log-url-or-api-key'))
        with patch('http.client.HTTPSConnection', return_value=connection):
            pool = JsonConnectionPool('https://example.invalid', capacity=1)
            with self.assertRaises(HttpPoolError) as caught: pool.request('POST')
            self.assertNotIn('do-not-log', str(caught.exception))
            self.assertEqual(connection.request.call_count, 1)
            self.assertTrue(connection.closed)
            pool.close()

    def test_idempotent_retries_once_on_stale_reused_connection(self):
        stale, fresh = Connection(), Connection()
        with patch('http.client.HTTPSConnection', side_effect=[stale, fresh]) as factory:
            pool = JsonConnectionPool('https://example.invalid', capacity=1)
            self.addCleanup(pool.close)
            self.assertEqual(pool.request(), {'ok': True})  # builds `stale`, reused=False
            stale.getresponse = Mock(side_effect=http.client.RemoteDisconnected('x'))
            # `stale` is reused and fails; the idempotent retry lands on `fresh`.
            self.assertEqual(pool.request(idempotent=True), {'ok': True})
            self.assertEqual(factory.call_count, 2)
            self.assertTrue(stale.closed)
            self.assertEqual([t['success'] for t in pool.timings], [True, False, True])
            self.assertEqual([t['reused'] for t in pool.timings], [False, True, False])

    def test_non_idempotent_never_retries_stale_reused_connection(self):
        stale, unused = Connection(), Connection()
        with patch('http.client.HTTPSConnection', side_effect=[stale, unused]) as factory:
            pool = JsonConnectionPool('https://example.invalid', capacity=1)
            self.addCleanup(pool.close)
            self.assertEqual(pool.request(), {'ok': True})
            stale.getresponse = Mock(side_effect=http.client.RemoteDisconnected('x'))
            with self.assertRaises(HttpPoolError):
                pool.request()  # default idempotent=False protects eth_sendRawTransaction
            self.assertEqual(factory.call_count, 1)  # no fresh connection was made
            self.assertFalse(unused.closed)

    def test_idempotent_does_not_retry_non_transport_failure(self):
        conn = Connection()
        rejected = Response()
        rejected.status = 500
        conn.responses = [rejected]
        with patch('http.client.HTTPSConnection', side_effect=[conn, Connection()]) as factory:
            pool = JsonConnectionPool('https://example.invalid', capacity=1)
            self.addCleanup(pool.close)
            with self.assertRaises(HttpPoolError) as caught:
                pool.request(idempotent=True)
            self.assertEqual(caught.exception.diagnostic["reason"], "http_status")
            self.assertEqual(factory.call_count, 1)  # HTTP 500 is not a stale-reuse retry

    def test_idempotent_retry_that_also_fails_is_raised(self):
        stale, fresh = Connection(), Connection()
        with patch('http.client.HTTPSConnection', side_effect=[stale, fresh]):
            pool = JsonConnectionPool('https://example.invalid', capacity=1)
            self.addCleanup(pool.close)
            self.assertEqual(pool.request(), {'ok': True})
            stale.getresponse = Mock(side_effect=http.client.RemoteDisconnected('x'))
            fresh.request = Mock(side_effect=http.client.RemoteDisconnected('x'))
            with self.assertRaises(HttpPoolError):
                pool.request(idempotent=True)  # retried exactly once, then surfaced
            self.assertTrue(stale.closed and fresh.closed)

    def test_idle_connection_is_dropped_before_reuse(self):
        stale, fresh = Connection(), Connection()
        with patch('http.client.HTTPSConnection', side_effect=[stale, fresh]) as factory:
            pool = JsonConnectionPool('https://example.invalid', capacity=1, idle_reuse_timeout=5.0)
            self.addCleanup(pool.close)
            self.assertEqual(pool.request(), {'ok': True})  # builds `stale`, stamps last-used
            stale._smcopy_last_used -= 10  # simulate sitting idle past the timeout
            self.assertEqual(pool.request(), {'ok': True})  # stale dropped, `fresh` built
            self.assertEqual(factory.call_count, 2)
            self.assertTrue(stale.closed)
            self.assertEqual([t['reused'] for t in pool.timings], [False, False])

    def test_connection_is_reused_within_idle_window(self):
        conn = Connection()
        with patch('http.client.HTTPSConnection', return_value=conn) as factory:
            pool = JsonConnectionPool('https://example.invalid', capacity=1, idle_reuse_timeout=5.0)
            self.addCleanup(pool.close)
            self.assertEqual(pool.request(), {'ok': True})
            self.assertEqual(pool.request(), {'ok': True})  # within the window -> reused
            factory.assert_called_once()
            self.assertEqual([t['reused'] for t in pool.timings], [False, True])

    def test_send_guard_runs_after_connect_and_before_request(self):
        connection = Connection()
        guard = Mock(side_effect=ValueError('expired'))
        with patch('http.client.HTTPSConnection', return_value=connection):
            pool = JsonConnectionPool('https://example.invalid', capacity=1)
            with self.assertRaises(HttpPoolError): pool.request('POST', before_send=guard)
            self.assertEqual(connection.requests, 0)
            self.assertTrue(connection.closed)
            pool.close()

    def test_pool_wait_is_bounded_and_closed_response_not_reused(self):
        pool = JsonConnectionPool('https://example.invalid', capacity=1, timeout=.01)
        slot = pool._slots.get()
        with self.assertRaises(HttpPoolError): pool.request()
        pool._slots.put(slot)
        connection = Connection()
        response = Response()
        response.will_close = True
        connection.responses = [response]
        with patch('http.client.HTTPSConnection', return_value=connection):
            pool.request()
            self.assertTrue(connection.closed)
        pool.close()


class CancellationTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_caller_does_not_release_worker_connection_early(self):
        entered, release = threading.Event(), threading.Event()
        class Slow(Connection):
            def getresponse(self):
                entered.set()
                release.wait(1)
                return Response()
        connection = Slow()
        with patch('http.client.HTTPSConnection', return_value=connection):
            pool = JsonConnectionPool('https://example.invalid', capacity=1, timeout=.2)
            task = asyncio.create_task(asyncio.to_thread(pool.request))
            await asyncio.to_thread(entered.wait, .5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError): await task
            self.assertEqual(pool._slots.qsize(), 0)
            pool.close()
            release.set()
            for _ in range(30):
                if pool._slots.qsize(): break
                await asyncio.sleep(.005)
            self.assertTrue(connection.closed)
            self.assertEqual(pool._slots.qsize(), 1)
