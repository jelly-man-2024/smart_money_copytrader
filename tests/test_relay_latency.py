"""Offline transport and speculative market-data boundaries. No live services."""
import asyncio
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from smart_money import registry as R
from smart_money.early_runtime import EarlyRuntime
from smart_money.early_feed_lane import EarlyEvidenceResolver
from smart_money.early_intent import parse_candidates
from smart_money.relay_api import RelayPublicClient, RelayApiError, RelayNotReady
import test_latency_execution as latency_fixtures
from test_latency_execution import A, B, H
from test_early_shadow import fixture
from test_early_feed import order_for


class RelayPoolTests(unittest.TestCase):
    def connection(self, status=200, body=None):
        c = MagicMock(sock=None)
        c.connect.side_effect = lambda: setattr(c, "sock", object())
        c.getresponse.return_value = SimpleNamespace(status=status,
            read=lambda limit: body if body is not None else b'{"requests":[{}]}')
        return c

    def test_reuses_connection_and_records_transport_timing(self):
        c = self.connection()
        client = RelayPublicClient()
        with patch("smart_money.relay_api.http.client.HTTPSConnection", return_value=c) as factory:
            client._fetch(H, 2)
            client._fetch(H, 2)
        self.assertEqual(factory.call_count, 1)
        self.assertEqual(c.connect.call_count, 1)
        self.assertEqual([x['reused'] for x in client.timings], [False, True])
        self.assertEqual(client._connections.qsize(), 4)
        client.close()
        c.close.assert_called_once()
        with self.assertRaises(RelayApiError): client._fetch(H, 2)

    def test_redirect_errors_oversize_and_invalid_json_discard_without_retry(self):
        for status, body in ((302, b''), (429, b''), (200, b'bad'),
                             (200, b'x' * (4194304 + 1))):
            client = RelayPublicClient()
            c = self.connection(status, body)
            with patch("smart_money.relay_api.http.client.HTTPSConnection", return_value=c) as factory:
                with self.assertRaises(RelayApiError): client._fetch(H, 2)
            self.assertEqual(factory.call_count, 1)
            c.close.assert_called_once()
            self.assertEqual(client._connections.qsize(), 4)

    def test_broken_connection_is_rebuilt_on_next_lookup_only(self):
        client = RelayPublicClient()
        bad, good = self.connection(), self.connection()
        bad.request.side_effect = OSError('private provider message')
        with patch("smart_money.relay_api.http.client.HTTPSConnection", side_effect=[bad, good]) as factory:
            with self.assertRaisesRegex(RelayApiError, 'OSError') as error: client._fetch(H, 2)
            self.assertNotIn('private', str(error.exception))
            self.assertEqual(factory.call_count, 1)
            client._fetch(H, 2)
        self.assertEqual(factory.call_count, 2)

    def test_empty_order_is_not_cached_or_retried(self):
        client = RelayPublicClient()
        c = self.connection(body=b'{"requests":[]}')
        with patch("smart_money.relay_api.http.client.HTTPSConnection", return_value=c):
            with self.assertRaises(RelayNotReady): client._fetch(H, 2)
        self.assertEqual(c.request.call_count, 1)


class SharedRoutesTests(unittest.IsolatedAsyncioTestCase):
    setUp = latency_fixtures.QuoteReuseTests.setUp

    async def test_two_paths_share_inflight_and_completed_market_data(self):
        self.quoter.shared_routes_enabled = True
        async def quote(operation, signal):
            with self.quoter.execution_context(operation, A, 'policy', 6) as context:
                result = await self.quoter.quote_with_reference(signal, '100000')
                return result, context
        early = replace(self.signal, stage='intent', path='early', execution_status='pending')
        first, second = await asyncio.gather(quote('early', early), quote('strict', self.signal))
        self.assertEqual(len(self.client.amounts), 2)
        self.assertEqual(first[1]['route_requests'] + second[1]['route_requests'], 2)
        third = await quote('strict-again', self.signal)
        self.assertEqual(third[1]['shared_route_reuses'], 2)
        self.assertEqual(len(self.client.amounts), 2)
        self.assertFalse(self.quoter._route_flights)

    async def test_expiry_and_binding_changes_request_again(self):
        self.quoter.shared_routes_enabled = True
        async def one(follower=A, config='policy', amount='100', signal=None):
            with self.quoter.execution_context(H, follower, config, 6):
                return await self.quoter._request_route(signal or self.signal, amount)
        await one()
        await one(follower=B)
        await one(config='new')
        await one(amount='200')
        await one(signal=replace(self.signal, token_out=A))
        self.assertEqual(len(self.client.amounts), 5)
        self.now += 6.001
        await one()
        self.assertEqual(len(self.client.amounts), 6)

    async def test_errors_not_cached_and_cancellation_cleans_flight(self):
        self.quoter.shared_routes_enabled = True
        with self.quoter.execution_context(H, A, 'policy', 6):
            with patch.object(self.client, 'route', AsyncMock(side_effect=ValueError('failed'))):
                with self.assertRaises(ValueError): await self.quoter._request_route(self.signal, '100')
            self.assertFalse(self.quoter._route_flights)
            entered = asyncio.Event()
            async def blocked(*args):
                entered.set()
                await asyncio.Event().wait()
            with patch.object(self.client, 'route', blocked):
                task = asyncio.create_task(self.quoter._request_route(self.signal, '100'))
                await entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError): await task
            self.assertFalse(self.quoter._route_flights)
            self.assertFalse(self.quoter._shared_routes)
            await self.quoter._request_route(self.signal, '100')

    async def test_excluded_sources_and_future_cache_cannot_reuse_original(self):
        self.quoter.shared_routes_enabled = True
        route = SimpleNamespace(observed_at=self.now)
        with patch.object(self.client, 'route', AsyncMock(return_value=route)) as request:
            with self.quoter.execution_context(H, A, 'policy', 6) as context:
                await self.quoter._request_route(self.signal, '100')
                context['excluded_sources'] = ('uniswap-v4',)
                await self.quoter._request_route(self.signal, '100')
                self.assertEqual(request.call_count, 2)
                self.assertEqual(request.call_args.kwargs, {'excluded_sources': ('uniswap-v4',)})
                self.now -= 1
                await self.quoter._request_route(self.signal, '100')
                self.assertEqual(request.call_count, 3)

    async def test_shared_cache_is_bounded(self):
        self.quoter.shared_routes_enabled = True
        with self.quoter.execution_context(H, A, 'policy', 6):
            for i in range(70):
                await self.quoter._request_route(self.signal, str(i + 1))
        self.assertEqual(len(self.quoter._shared_routes), 64)

    async def test_prefetch_is_fixed_usdg_only_and_never_executes(self):
        self.quoter.shared_routes_enabled = True
        c = SimpleNamespace(side='BUY', token_in=R.USDG, token_out=B, wallet=A,
                            tx_hash=H, path='early', operation_key='order')
        rule = SimpleNamespace(mode='fixed', fixed_amount_raw='100000')
        p = SimpleNamespace(wallet=A, buy_rules={'USDG': rule}, execution_providers=('kyber',),
            allowed_assets={R.USDG}, allowed_protocols={'relay_solver'}, budget_limits={'USDG':'1000000'},
            follower_wallet=A, snapshot_hash='policy', quote_policy=SimpleNamespace(max_age_seconds=6),
            relationship_id='1')
        execute, store = AsyncMock(), MagicMock()
        runtime = EarlyRuntime(store, self.quoter, None, [p], 'trial', execute, lambda: True, MagicMock())
        await runtime.prefetch(c)
        self.assertEqual(len(self.client.amounts), 2)
        rule.mode = 'ratio'
        await runtime.prefetch(c)
        self.assertEqual(len(self.client.amounts), 2)
        execute.assert_not_called()
        self.assertFalse(store.mock_calls)


class ParallelResolverTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_prefetch_does_not_remove_valid_order_evidence(self):
        tx, wallet = fixture()
        candidate = parse_candidates(tx, wallet).candidates[0]
        at = int(candidate.metadata['permit_deadline']) - 1
        tx = replace(tx, timestamp=at, received_at=float(at))
        relay = SimpleNamespace(lookup_by_order=AsyncMock(return_value=order_for(candidate)))
        resolver = EarlyEvidenceResolver(MagicMock(), relay, [wallet])
        resolver.prefetch = AsyncMock(side_effect=ValueError('quote unavailable'))
        with patch('smart_money.early_feed_lane.time.time', return_value=float(at)):
            result = await resolver(tx)
        self.assertTrue(result['candidates'][0]['recognized_intent'])
        self.assertEqual(result['candidates'][0]['errors']['quote_prefetch'], 'ValueError')
        self.assertFalse(result['copy_eligible'])

    async def test_prefetch_overlaps_order_but_cannot_replace_invalid_attribution(self):
        tx, wallet = fixture()
        candidate = parse_candidates(tx, wallet).candidates[0]
        at = int(candidate.metadata['permit_deadline']) - 1
        tx = replace(tx, timestamp=at, received_at=float(at))
        prefetched = asyncio.Event()
        async def prefetch(c):
            prefetched.set()
        async def lookup(*args):
            await asyncio.wait_for(prefetched.wait(), 1)
            return {'requests': []}
        resolver = EarlyEvidenceResolver(MagicMock(), SimpleNamespace(lookup_by_order=lookup), [wallet])
        resolver.prefetch = prefetch
        with patch('smart_money.early_feed_lane.time.time', return_value=float(at)):
            result = await resolver(tx)
        self.assertTrue(prefetched.is_set())
        self.assertFalse(result['candidates'][0]['recognized_intent'])
        self.assertFalse(result['copy_eligible'])
