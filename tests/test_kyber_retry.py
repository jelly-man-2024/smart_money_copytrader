"""Offline source-filter and unsigned recovery regressions; no live transactions."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from smart_money import registry as R
from smart_money.execution_pipeline import ExecutionPreparer
from smart_money.kyber import KyberAggregatorClient, KyberApiError
from smart_money.models import Signal
from smart_money.quotes import LiveQuoter, Quote, QuotePolicy
from smart_money.simulation_diagnostics import AggregatorSimulationError
from smart_money.rpc import RpcError
from smart_money.store import Store
from eth_abi import encode

A = '0x' + '11' * 20
H = '0x' + 'ab' * 32
SAMPLE = json.loads((Path(__file__).resolve().parents[1] /
                    'data/kyber_route_build_sample_2026-09-13.json').read_text())


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.doc = deepcopy(SAMPLE['routes'])
        self.doc['data']['routeSummary']['route'] = [[{'exchange': 'uniswap-v4'}]]
        summary = self.doc['data']['routeSummary']
        self.amount = summary['amountIn']
        self.signal = Signal(H, A, 'third_party', 'BUY', 'incoming', R.RELAY_PROXY,
            '0x0a2b8f36', stage='relay_buy_evidenced', execution_status='success',
            protocol='kyber', exact_in=True, token_in=summary['tokenIn'],
            token_out=summary['tokenOut'])
        self.client = KyberAggregatorClient()
        self.route = self.client._parse_route(self.doc, self.signal.token_in,
            self.signal.token_out, self.amount, 100)
        self.quoter = LiveQuoter(SimpleNamespace(call=AsyncMock()), {'kyber': self.client})
        self.diag = {'failure_kind': 'rpc_failure', 'minimum_amount_out_raw': '100',
                     'deadline': 220, 'rpc_error': {'message_category': 'execution_reverted'}}

    async def test_filter_query_and_fail_closed_on_ignored_or_missing_metadata(self):
        with patch.object(self.client, '_request', return_value=self.doc) as request:
            for layout in ([[{'exchange': 'uniswap-v4'}]], [], [[{}]], [None]):
                self.doc['data']['routeSummary']['route'] = layout
                with self.assertRaises(KyberApiError):
                    await self.client.route(self.signal.token_in, self.signal.token_out,
                        self.amount, excluded_sources=('uniswap-v4',))
            self.doc['data']['routeSummary']['route'] = [[{'exchange': 'uniswap'}]]
            route = await self.client.route(self.signal.token_in, self.signal.token_out,
                self.amount, excluded_sources=('uniswap-v4',))
            self.assertEqual(route.sources(), frozenset({'uniswap'}))
            self.assertEqual(request.call_args.args[1]['excludedSources'], 'uniswap-v4')

    async def test_bad_filter_rejected_without_network(self):
        with patch.object(self.client, '_request') as request:
            for excluded in (('bad,value',), ('UPPER',), ['uniswap-v4']):
                with self.assertRaises(ValueError):
                    await self.client.route(self.signal.token_in, self.signal.token_out,
                                            self.amount, excluded_sources=excluded)
            request.assert_not_called()

    def seed(self, context):
        context['routes'][self.quoter._quote_key(self.signal, self.amount)] = self.route
        context['bundle'] = ('old', 'old')

    async def test_one_retry_clears_full_and_reference_and_keeps_filter_on_refresh(self):
        with self.quoter.execution_context(H, A, 'snapshot', 5) as ctx:
            self.seed(ctx)
            evidence = self.quoter.begin_simulation_route_retry(self.signal, self.amount, self.diag)
            self.assertIsNotNone(evidence)
            self.assertFalse(ctx['routes'])
            self.assertIsNone(ctx['bundle'])
            self.assertIsNone(self.quoter.begin_simulation_route_retry(self.signal, self.amount, self.diag))
            with patch.object(self.client, 'route', new_callable=AsyncMock) as route:
                await self.quoter._request_route(self.signal, self.amount)
                await self.quoter._request_route(self.signal, '1000')
                self.assertTrue(all(c.kwargs == {'excluded_sources': ('uniswap-v4',)}
                                    for c in route.call_args_list))
        with self.quoter.execution_context('new', A, 'snapshot', 5) as ctx:
            self.assertEqual(ctx['excluded_sources'], ())
            self.assertIsNone(ctx['route_retry'])

    async def test_no_retry_for_transport_gas_balance_unknown_or_non_v4(self):
        self.assertIsNone(self.quoter.begin_simulation_route_retry(self.signal, self.amount, self.diag))
        with self.quoter.execution_context(H, A, 'snapshot', 5) as ctx:
            self.seed(ctx)
            for category in ('timeout', 'out_of_gas', 'insufficient_funds', 'unspecified'):
                diagnostic = {**self.diag, 'rpc_error': {'message_category': category}}
                self.assertIsNone(self.quoter.begin_simulation_route_retry(self.signal, self.amount, diagnostic))
            for metadata in ({}, {'route': [[{'exchange': 'uniswap'}]]}):
                ctx['routes'][self.quoter._quote_key(self.signal, self.amount)] = replace(
                    self.route, route_summary=metadata)
                self.assertIsNone(self.quoter.begin_simulation_route_retry(self.signal, self.amount, self.diag))

    async def test_preparer_retries_only_once_and_passes_original_floor_deadline(self):
        store = SimpleNamespace(paper_proposal=Mock(return_value={'amount_in_raw': self.amount}),
                                execution_plan=Mock(return_value=None))
        preparer = ExecutionPreparer(store, self.quoter, None, QuotePolicy(), (), (), (), 's')
        error = AggregatorSimulationError('revert', self.diag)
        for second in ('prepared', error, ValueError('expired')):
            with self.quoter.execution_context(H, A, 'snapshot', 5) as ctx:
                self.seed(ctx)
                with patch.object(preparer, '_prepare', side_effect=[error, second]) as prepare:
                    if isinstance(second, Exception):
                        with self.assertRaises(type(second)):
                            await preparer.prepare(self.signal, 'proposal')
                        self.assertEqual(ctx['route_retry']['status'], 'failed')
                    else:
                        self.assertEqual(await preparer.prepare(self.signal, 'proposal'), second)
                        self.assertEqual(ctx['route_retry']['status'], 'prepared')
                    self.assertEqual(prepare.call_count, 2)
                    self.assertEqual(prepare.call_args.kwargs['minimum_floor'], '100')
                    self.assertEqual(prepare.call_args.kwargs['original_deadline'], 220)

    async def test_existing_plan_never_rerouted(self):
        store = SimpleNamespace(paper_proposal=Mock(return_value={'amount_in_raw': self.amount}),
                                execution_plan=Mock(return_value={'status': 'signed'}))
        preparer = ExecutionPreparer(store, self.quoter, None, QuotePolicy(), (), (), (), 's')
        with self.quoter.execution_context(H, A, 'snapshot', 5) as ctx:
            self.seed(ctx)
            with patch.object(preparer, '_prepare', side_effect=AggregatorSimulationError('revert', self.diag)) as prepare:
                with self.assertRaises(AggregatorSimulationError):
                    await preparer.prepare(self.signal, 'proposal')
                self.assertEqual(prepare.call_count, 1)
                self.assertIsNone(ctx['route_retry'])

    async def test_real_unsigned_pipeline_requotes_simulates_then_reserves_one_nonce(self):
        await self.check_unsigned_pipeline(gas_recovers=False)

    async def test_gas_recovery_persists_exact_plan_without_requoting(self):
        await self.check_unsigned_pipeline(gas_recovers=True)

    async def check_unsigned_pipeline(self, gas_recovers):
        follower = '0x3004ab92565deeea0a2eaa27e40e297bb457e1a6'
        output = int(SAMPLE['build']['data']['amountOut'])
        signal = replace(self.signal, evidence={'actual_input_debit_raw': self.amount,
                                               'actual_output_credit_raw': str(output)})
        store = Store(':memory:')
        self.addCleanup(store.close)
        store.start_paper_budget_cycle('cycle', 'test')
        store.configure_paper_budget(A, 'USDG', '10000000')
        store.put(signal)
        store.reserve_paper_proposal({
            'proposal_id': 'p', 'source_event_id': signal.event_id, 'source_tx_hash': H,
            'wallet': A, 'trigger_mode': 'evidenced', 'strategy_version': 'test',
            'input_asset': signal.token_in, 'output_asset': signal.token_out,
            'budget_bucket': 'USDG', 'amount_in_raw': self.amount,
            'attribution': {'smart_wallet': A, 'follower_wallet': follower,
                            'relationship_id': '1', 'config_snapshot_hash': 's'}})
        simulations = []
        async def rpc_call(method, params=None):
            if method == 'eth_getBlockByNumber':
                return {'number': '0xa', 'hash': H}
            if method == 'eth_call' and params[0].get('data', '').startswith('0xe21fd0e9'):
                simulations.append(params)
                if len(simulations) <= (1 if gas_recovers else 2):
                    self.assertIsNone(store.execution_plan('p'))
                    self.assertIsNone(store.execution_nonce_reservation('p'))
                    raise RpcError('reverted', diagnostic={'message_category': 'execution_reverted'})
                return '0x' + encode(['uint256', 'uint256'], [output, 200000]).hex()
            return {'eth_gasPrice': '0x1', 'eth_getTransactionCount': '0x7',
                    'eth_getBalance': hex(10**18), 'eth_call': hex(10**18)}[method]
        requests = []
        def request(path, query=None, body=None):
            requests.append((path, query, body))
            if path == 'route/build':
                return deepcopy(SAMPLE['build'])
            doc = deepcopy(self.doc)
            summary = doc['data']['routeSummary']
            summary['amountIn'] = query['amountIn']
            summary['amountOut'] = str(output * int(query['amountIn']) // int(self.amount))
            summary['route'] = [[{'exchange': 'uniswap' if query.get('excludedSources') else 'uniswap-v4'}]]
            return doc
        rpc = SimpleNamespace(call=rpc_call)
        quoter = LiveQuoter(rpc, {'kyber': self.client})
        with patch('time.time', return_value=100), patch.object(self.client, '_request', side_effect=request):
            with quoter.execution_context(H, follower, 's', 5) as ctx:
                result = await ExecutionPreparer(store, quoter, rpc, QuotePolicy(),
                    frozenset({'kyber', 'relay_solver'}), frozenset({R.USDG}), (), 's').prepare(signal, 'p')
                self.assertEqual(result.nonce, 7)
                self.assertEqual(len(simulations), 2 if gas_recovers else 3)
                self.assertEqual((ctx['route_requests'], ctx['build_requests']),
                                 (2, 1) if gas_recovers else (4, 2))
                row = store.execution_plan('p')
                self.assertEqual(row['status'], 'prepared')
                if gas_recovers:
                    self.assertIsNone(ctx['route_retry'])
                    self.assertEqual(row['preflight']['gas_retry']['status'], 'simulation_passed')
                    self.assertEqual(row['transaction']['gas'], int(simulations[1][0]['gas'], 16))
                    self.assertEqual(row['unsigned_plan']['gas_limit'], row['transaction']['gas'])
                    self.assertEqual({k: v for k, v in simulations[0][0].items() if k != 'gas'},
                                     {k: v for k, v in simulations[1][0].items() if k != 'gas'})
                else:
                    self.assertEqual(ctx['route_retry']['status'], 'prepared')
                    self.assertEqual(row['preflight']['route_retry']['status'], 'simulation_passed')
                self.assertEqual(store.execution_attempts(row['plan_id']), [])
                filtered = [q for p, q, _ in requests if p == 'routes' and q.get('excludedSources')]
                self.assertEqual(len(filtered), 0 if gas_recovers else 2)

    async def test_retry_cannot_reduce_minimum_or_pass_expired_quote(self):
        proposal = {'status': 'reserved', 'source_event_id': self.signal.event_id,
                    'amount_in_raw': self.amount, 'attribution': {
                        'follower_wallet': A, 'relationship_id': '1', 'config_snapshot_hash': 's'}}
        store = SimpleNamespace(execution_plan=Mock(return_value=None),
                                paper_proposal=Mock(return_value=proposal),
                                reserve_execution_nonce=Mock())
        quote = Quote('kyber', R.KYBER_META_AGGREGATION_ROUTER_V2, 10, H, 100,
                      self.signal.token_in, self.signal.token_out, self.amount, '200', '100')
        quoter = SimpleNamespace(quote_with_reference=AsyncMock(return_value=(quote, quote, '1')),
            build_aggregator_transaction=AsyncMock(return_value=SimpleNamespace(
                minimum_amount_out_raw='99', amount_out_raw='200', gas_estimate=100)))
        preparer = ExecutionPreparer(store, quoter, None, QuotePolicy(max_age_seconds=5), (), (), (), 's')
        with self.assertRaisesRegex(ValueError, 'below original protected minimum'):
            await preparer._prepare(self.signal, 'p', now=101, minimum_floor='100', original_deadline=220)
        quoter.build_aggregator_transaction.return_value.minimum_amount_out_raw = '100'
        with self.assertRaisesRegex(ValueError, 'requote rejected'):
            await preparer._prepare(self.signal, 'p', now=106, minimum_floor='100', original_deadline=220)
        store.reserve_execution_nonce.assert_not_called()
