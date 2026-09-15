"""Offline TTL boundary and pipeline wiring regressions. No keys or network."""
import ast
import inspect
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock

from smart_money import execution_pipeline, registry as R
from smart_money.execution_prep import ReadOnlyExecutionPreflight, UnsignedExecutionPlan


class PreflightQuoteAgeTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan = UnsignedExecutionPlan(
            '0x' + '11' * 20, '1', 'p', R.V2_ROUTER, '0x1234', '0', R.USDG,
            '100', '99', 200000, '100', '0', 100.0, 10, '0x' + 'ab' * 32, 200)
        async def call(method, params=None):
            return {'eth_getTransactionCount': '0x7', 'eth_getBalance': hex(10**18),
                    'eth_gasPrice': '0x32', 'eth_call': '0x64'}[method]
        self.rpc = SimpleNamespace(call=AsyncMock(side_effect=call))

    async def test_six_second_policy_accepts_three_and_six_seconds(self):
        checker = ReadOnlyExecutionPreflight(self.rpc, frozenset({R.V2_ROUTER}),
                                            '30000000', max_quote_age_seconds=6)
        for now in (103, 106):
            result = await checker.check(self.plan, now)
            self.assertEqual(result['quote_max_age_seconds'], 6)

    async def test_expired_and_future_quotes_reject_before_rpc(self):
        checker = ReadOnlyExecutionPreflight(self.rpc, frozenset({R.V2_ROUTER}),
                                            '30000000', max_quote_age_seconds=6)
        for now in (106.001, 99):
            with self.assertRaisesRegex(ValueError, 'expired'):
                await checker.check(self.plan, now)
        self.rpc.call.assert_not_awaited()

    async def test_default_remains_two_seconds_for_legacy_callers(self):
        checker = ReadOnlyExecutionPreflight(self.rpc, frozenset({R.V2_ROUTER}), '30000000')
        with self.assertRaisesRegex(ValueError, 'expired'):
            await checker.check(self.plan, 103)

    async def test_invalid_limits_rejected(self):
        for age in (0, -1, 61, True, '6', float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                ReadOnlyExecutionPreflight(self.rpc, frozenset(), '1', max_quote_age_seconds=age)

    async def test_all_four_pipeline_preflights_use_policy_not_literal(self):
        tree = ast.parse(inspect.getsource(execution_pipeline))
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Name) and node.func.id == 'ReadOnlyExecutionPreflight']
        self.assertEqual(len(calls), 4)
        for call in calls:
            args = {kw.arg: kw.value for kw in call.keywords}
            self.assertIn('max_quote_age_seconds', args)
            self.assertEqual(ast.unparse(args['max_quote_age_seconds']), 'self.quote_policy.max_age_seconds')
