"""Unsigned gas recovery: no network, signing, key source or real ledger."""
from dataclasses import replace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, Mock, patch

from smart_money.execution_pipeline import ExecutionPreparer
from smart_money.execution_prep import UnsignedExecutionPlan
from smart_money.quotes import QuotePolicy
from smart_money.simulation_diagnostics import AggregatorSimulationError


class GasRetryTests(IsolatedAsyncioTestCase):
    def setUp(self):
        self.plan = UnsignedExecutionPlan(
            '0x' + '11'*20, '1', 'p', '0x' + '22'*20, '0x1234', '0',
            '0x' + '33'*20, '100000', '99', 1218885, '100', '0',
            100, 10, '0x' + '44'*32, 220, execution_provider='kyber')
        self.preparer = ExecutionPreparer(None, None, None,
            QuotePolicy(max_gas_cost_wei='200000000'), (), (), (), 's')
        self.validate = Mock()
        self.preflight = AsyncMock(return_value={})
        self.patcher = patch('smart_money.execution_pipeline.ReadOnlyExecutionPreflight.check', self.preflight)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def error(self, category='execution_reverted', kind='rpc_failure'):
        return AggregatorSimulationError('failed', {
            'failure_kind': kind, 'rpc_error': {'message_category': category}})

    async def run_sim(self, outcomes, **kw):
        with patch('smart_money.execution_pipeline.simulate_aggregator_execution',
                   new=AsyncMock(side_effect=outcomes)) as sim:
            result = await self.preparer._simulate_with_gas_retry(self.plan, self.validate, **kw)
        return result, sim

    async def test_success_has_no_extra_call(self):
        (plan, evidence), sim = await self.run_sim([{'simulated': True}])
        self.assertEqual(plan, self.plan)
        self.assertNotIn('gas_retry', evidence)
        self.assertEqual(sim.await_count, 1)
        self.preflight.assert_not_awaited()

    async def test_retry_changes_only_gas_and_preserves_diagnostic(self):
        original = self.error()
        (plan, evidence), sim = await self.run_sim([original, {'simulated': True}])
        self.assertEqual(plan.gas_limit, 1828328)
        self.assertEqual(replace(plan, gas_limit=self.plan.gas_limit), self.plan)
        self.assertEqual(sim.await_count, 2)
        self.assertEqual(self.preflight.call_args.args[0], plan)
        self.assertEqual(self.validate.call_count, 3)
        self.assertEqual(evidence['gas_retry']['status'], 'simulation_passed')
        self.assertEqual(evidence['gas_retry']['original_simulation_failure'], original.diagnostic)

    async def test_absolute_and_fee_caps(self):
        self.plan = replace(self.plan, gas_limit=1500000)
        (plan, _), _ = await self.run_sim([self.error(), {}])
        self.assertEqual(plan.gas_limit, 2000000)
        self.preparer.quote_policy = QuotePolicy(max_gas_cost_wei='170000000')
        (plan, _), _ = await self.run_sim([self.error(), {}])
        self.assertEqual(plan.gas_limit, 1700000)

    async def test_no_headroom_no_extra_simulation(self):
        for budget in ('121888500', '100000000'):
            self.preparer.quote_policy = QuotePolicy(max_gas_cost_wei=budget)
            with self.assertRaises(AggregatorSimulationError):
                await self.run_sim([self.error()])
        self.preflight.assert_not_awaited()

    async def test_transport_missing_output_below_minimum_not_retried(self):
        for category, kind in [('timeout', 'rpc_failure'), ('insufficient_funds', 'rpc_failure'),
                               ('execution_reverted', 'below_minimum'),
                               ('execution_reverted', 'missing_output')]:
            with self.assertRaises(AggregatorSimulationError):
                await self.run_sim([self.error(category, kind)])
        self.preflight.assert_not_awaited()

    async def test_second_failure_has_both_diagnostics_without_loop(self):
        original, second = self.error(), self.error('out_of_gas')
        with self.assertRaises(AggregatorSimulationError) as caught:
            await self.run_sim([original, second])
        self.assertIs(caught.exception, second)
        self.assertEqual(second.diagnostic['gas_retry']['status'], 'failed')
        self.assertEqual(second.diagnostic['gas_retry']['original_simulation_failure'], original.diagnostic)

    async def test_expiry_and_preflight_failure_stop_before_retry(self):
        for fail_at in ('validate', 'preflight'):
            self.validate.side_effect = ValueError('expired') if fail_at == 'validate' else None
            self.preflight.side_effect = ValueError('insufficient balance') if fail_at == 'preflight' else None
            with patch('smart_money.execution_pipeline.simulate_aggregator_execution',
                       new=AsyncMock(side_effect=[self.error(), {}])) as sim:
                with self.assertRaises(ValueError):
                    await self.preparer._simulate_with_gas_retry(self.plan, self.validate)
                self.assertEqual(sim.await_count, 1)

    async def test_expiry_after_preflight_or_simulation_still_rejects(self):
        for sequence in ([None, ValueError('expired')], [None, None, ValueError('expired')]):
            self.validate.side_effect = sequence
            with self.assertRaisesRegex(ValueError, 'expired'):
                await self.run_sim([self.error(), {}])

    async def test_route_retry_cannot_get_another_gas_retry(self):
        with self.assertRaises(AggregatorSimulationError):
            await self.run_sim([self.error()], allow_retry=False)
        self.preflight.assert_not_awaited()

    async def test_explicit_out_of_gas_can_recover(self):
        (plan, evidence), sim = await self.run_sim([self.error('out_of_gas'), {}])
        self.assertGreater(plan.gas_limit, self.plan.gas_limit)
        self.assertEqual(sim.await_count, 2)
        self.assertEqual(evidence['gas_retry']['status'], 'simulation_passed')

    async def test_non_kyber_and_already_at_absolute_cap_do_not_retry(self):
        original = self.plan
        for plan in (replace(original, execution_provider='local'),
                     replace(original, gas_limit=2000000)):
            self.plan = plan
            with self.assertRaises(AggregatorSimulationError):
                await self.run_sim([self.error()])
        self.preflight.assert_not_awaited()
