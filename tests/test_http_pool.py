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
