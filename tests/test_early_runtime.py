"""Offline early handoff integration: synthetic API, memory ledger, no real keys."""
from contextlib import nullcontext
from dataclasses import replace
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from eth_abi import encode
from eth_account import Account

import test_early_decision as fixtures
from smart_money import registry as R
from smart_money.decode import KYBER_SWAP_EXECUTION
from smart_money.early_runtime import EarlyRuntime
from smart_money.execution_pipeline import (ExecutionPreparer, OfflineExecutionSigner,
    ReadOnlyPreBroadcastReviewer, check_early_execution_source)
from smart_money.execution_prep import build_early_aggregator_execution_plan, build_aggregator_execution_plan
from smart_money.kyber import KyberSwapTransaction
from smart_money.paper import AmountRule
from smart_money.quotes import QuotePolicy
from smart_money.store import Store


def swap_for(signal, follower, minimum=194):
    desc = (signal.token_in, signal.token_out, [], [], [], [], follower, 100000, minimum, 0, b"")
    data = "0xe21fd0e9" + encode([KYBER_SWAP_EXECUTION], [(follower, follower, b"test", desc, b"")]).hex()
    return KyberSwapTransaction(signal.token_in, signal.token_out, "100000", "200", str(minimum),
        follower, R.KYBER_META_AGGREGATION_ROUTER_V2, data, "0", 350000, 220,
        100, "synthetic", "synthetic")


class EarlyRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_trial_start_without_operator_confirmation_never_connects(self):
        from smart_money import cli
        with patch("sys.argv", ["sm-copy", "early-trial-start", "--trial-id", "new",
                "--follower", fixtures.A, "--relationships", "1"]), \
             patch.object(cli, "load_mysql_paper_config") as load, patch.object(cli, "report"):
            with self.assertRaises(SystemExit):
                cli.main()
            load.assert_not_called()

    def setUp(self):
        self.f = fixtures.EarlyDecisionTests()
        self.f.setUp()
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.start_paper_budget_cycle("test", "synthetic")
        self.store.configure_paper_budget("relationship:test", "USDG", "1000000")
        self.policy = SimpleNamespace(relationship_id="1", follower_wallet=fixtures.A,
            wallet=self.f.c.wallet, snapshot_hash="synthetic", ledger_scope="relationship:test",
            budget_limits={"USDG": "1000000"}, buy_rules={"USDG": AmountRule("fixed", fixed_amount_raw="100000")},
            sell_rule=AmountRule("proportional", ratio_ppm=1000000),
            allowed_protocols=frozenset({"relay_solver", "kyber"}), allowed_assets=frozenset({R.USDG}),
            execution_providers=("kyber",), quote_policy=QuotePolicy(), strategy_version="test")
        self.store.start_early_trial("trial", fixtures.A, [1], now=100)
        self.quoter = SimpleNamespace(quote_with_reference=self.f.quoter.quote_with_reference,
                                     execution_context=lambda *args: nullcontext({}))
        self.execute, self.report, self.gate = AsyncMock(), Mock(), Mock()
        self.runtime = EarlyRuntime(self.store, self.quoter, self.gate, [self.policy], "trial",
                                    self.execute, lambda: True, self.report)
        self.result = {"candidates": [{"recognized_intent": True, "candidate": self.f.c.to_dict(),
                                      "snapshots": self.f.observations}]}
        self.clock = patch("smart_money.early_runtime.time.time", return_value=100.1)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.stop = patch("smart_money.early_runtime._stop_controls")
        self.stop.start()
        self.addCleanup(self.stop.stop)

    async def test_handoff_preserves_intent_and_claims_budget_once(self):
        await self.runtime(self.f.tx, self.result)
        self.execute.assert_awaited_once()
        _, signal, pid = self.execute.call_args.args
        self.assertEqual(signal.stage, "intent")
        p = self.store.paper_proposal(pid)
        self.assertEqual(p["source_event_id"], signal.event_id)
        self.assertEqual(p["attribution"]["early_trial_id"], "trial")
        self.assertEqual(self.store.paper_budget(self.policy.ledger_scope, "USDG")["reserved_raw"], "100000")
        await self.runtime(self.f.tx, self.result)
        self.execute.assert_awaited_once()
        self.assertEqual(self.store.paper_proposal(pid)["status"], "reserved")

    async def test_wrapper_handoff_keeps_periodic_code_provenance_and_live_revocation(self):
        from test_deployment_monitor import DeploymentMonitorTests
        f = DeploymentMonitorTests()
        f.setUp()
        await f.monitor.check_once()
        self.runtime.deployment_monitor = f.monitor
        self.policy.wallet = f.wallet
        self.f.quoter.quote_with_reference.return_value = (
            replace(self.f.quote, input_asset=f.c.token_in, output_asset=f.c.token_out),
            replace(self.f.reference, input_asset=f.c.token_in, output_asset=f.c.token_out), "1")
        evidence = {"candidates": [{"recognized_intent": True, "candidate": f.c.to_dict(),
                                    "snapshots": f.observations()}]}
        await self.runtime(f.tx, evidence)
        self.execute.assert_awaited_once()
        _, signal, pid = self.execute.call_args.args
        verification = self.store.paper_proposal(pid)["attribution"]["early_deployment_verification"]
        self.assertEqual(verification["validation_mode"], "periodic_monitor")
        self.assertEqual(verification["observed_at"], 90.)
        intent = self.execute.call_args.kwargs["early_intent"]
        intent.revalidate(104.)
        check_early_execution_source(self.store, intent, signal, self.store.paper_proposal(pid), now=104.)
        f.code = "0x"
        await f.monitor.check_once()
        with self.assertRaisesRegex(ValueError, "deployment_code_changed"):
            check_early_execution_source(self.store, intent, signal, self.store.paper_proposal(pid), now=104.)

    async def test_unprepared_failure_releases_budget_for_strict_fallback(self):
        self.execute.side_effect = ValueError("early_allowance_insufficient_strict_fallback")
        await self.runtime(self.f.tx, self.result)
        self.assertEqual(self.store.paper_budget(self.policy.ledger_scope, "USDG")["reserved_raw"], "0")
        self.assertEqual(self.store.connection.execute("SELECT status FROM copy_operation_claims").fetchone()[0], "released")

    async def test_expired_or_stopped_trial_never_calls_executor(self):
        self.store.stop_early_trial("trial")
        await self.runtime(self.f.tx, self.result)
        self.execute.assert_not_called()
        self.assertEqual(self.store.connection.execute("SELECT COUNT(*) FROM paper_proposals").fetchone()[0], 0)

    async def test_failure_recorded_before_handoff_blocks_execution(self):
        s = replace(self.f.intent.quote_signal(100.1), stage="failed", execution_status="reverted", execution_success=False)
        self.store.put(s)
        await self.runtime(self.f.tx, self.result)
        self.execute.assert_not_called()

    async def test_typed_builder_does_not_relax_original_builder(self):
        s = self.f.intent.quote_signal(100.1)
        swap = swap_for(s, fixtures.A)
        args = (fixtures.A, "1", "proposal", self.f.quote, "194", swap, 600000, "2", "0",
                self.policy.allowed_protocols, self.policy.allowed_assets)
        plan = build_early_aggregator_execution_plan(self.f.intent, *args, now=100.1)
        self.assertEqual(plan.minimum_amount_out_raw, "194")
        with self.assertRaises(ValueError):
            build_aggregator_execution_plan(s, *args, frozenset())
        with self.assertRaises(ValueError):
            build_early_aggregator_execution_plan(s, *args, now=100.1)
        with self.assertRaisesRegex(ValueError, "source intent"):
            build_early_aggregator_execution_plan(self.f.intent, fixtures.A, "1", "proposal",
                self.f.quote, "99", swap_for(s, fixtures.A, 99), 600000, "2", "0",
                self.policy.allowed_protocols, self.policy.allowed_assets, now=100.1)

    async def test_prepare_sign_review_with_ephemeral_unfunded_key_and_no_broadcast(self):
        # Only an ephemeral in-memory offline key. Never a DB key or RPC broadcast.
        account = Account.create()
        follower = account.address.lower()
        self.policy.follower_wallet = follower
        self.store.connection.execute("UPDATE early_trials SET follower_wallet=?", (follower,))
        self.store.connection.commit()
        await self.runtime(self.f.tx, self.result)
        self.execute.assert_awaited_once()
        _, signal, pid = self.execute.call_args.args
        intent = self.execute.call_args.kwargs["early_intent"]
        swap = swap_for(signal, follower)
        self.quoter.build_aggregator_transaction = AsyncMock(return_value=swap)
        preparer = ExecutionPreparer(self.store, self.quoter, Mock(), QuotePolicy(),
            self.policy.allowed_protocols, self.policy.allowed_assets, frozenset(), "synthetic")
        signer = OfflineExecutionSigner(self.store, self.quoter, Mock(), QuotePolicy(), "synthetic",
            signer_factory=lambda wallet: SimpleNamespace(sign_transaction=lambda tx: bytes(account.sign_transaction(tx).raw_transaction)),
            relationship_gate=self.gate)
        reviewer = ReadOnlyPreBroadcastReviewer(self.store, self.quoter, Mock(), QuotePolicy(), self.gate)
        with patch("smart_money.execution_pipeline.ReadOnlyExecutionPreflight.check",
                   new=AsyncMock(return_value={"pending_nonce": 0})), patch(
                   "smart_money.execution_pipeline.simulate_aggregator_execution", new=AsyncMock(return_value={})), patch(
                   "smart_money.execution_pipeline.require_offline_signing_enabled"):
            prepared = await preparer.prepare(signal, pid, now=100.1, early_intent=intent)
            signed = await signer.sign(signal, pid, now=100.1, early_intent=intent)
            review = await reviewer.review(signal, pid, signed.raw_transaction, now=100.1, early_intent=intent)
            self.assertEqual(review.evidence["early_trial_id"], "trial")
            self.assertEqual(prepared.nonce, 0)
            self.assertFalse(review.evidence["broadcast_performed"])
            with self.assertRaises(ValueError):
                await reviewer.review(signal, pid, signed.raw_transaction, now=106.001, early_intent=intent)
        with self.assertRaises(ValueError):
            check_early_execution_source(self.store, None, signal, self.store.paper_proposal(pid), 100.1)
