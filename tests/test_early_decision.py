"""Synthetic offline checks; no keys, RPC or real trading."""
from dataclasses import asdict, replace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import AsyncMock, MagicMock

from test_early_feed import samples, snapshot, order_for, A
from smart_money import registry as R
from smart_money.early_intent import parse_candidates
from smart_money.early_replay import transaction_from_record
from smart_money.verified_feed_intent import VerifiedFeedIntent
from smart_money.early_decision import EarlyDecisionEngine
from smart_money.quotes import Quote, QuotePolicy


class EarlyDecisionTests(IsolatedAsyncioTestCase):
    async def test_zeroex_first_and_route_error_falls_back_but_rpc_failure_does_not(self):
        from smart_money.zeroex import ZeroExApiError
        from smart_money.rpc import RpcError
        self.policy["execution_providers"] = ["zeroex", "kyber"]
        self.quoter.quote_with_reference.return_value = (
            replace(self.quote, protocol="zeroex"), replace(self.reference, protocol="zeroex"), "1")
        self.assertEqual((await self.evaluate())["source_signal"]["protocol"], "zeroex")
        self.quoter.quote_with_reference.reset_mock()
        self.quoter.quote_with_reference.side_effect = [ZeroExApiError("unavailable"),
                                                       (self.quote, self.reference, "1")]
        result = await self.evaluate()
        self.assertEqual(result["source_signal"]["protocol"], "kyber")
        self.assertEqual([c.args[0].protocol for c in self.quoter.quote_with_reference.call_args_list],
                         ["zeroex", "kyber"])
        self.quoter.quote_with_reference.reset_mock()
        self.quoter.quote_with_reference.side_effect = RpcError("public RPC unavailable")
        with self.assertRaises(RpcError): await self.evaluate()
        self.assertEqual(self.quoter.quote_with_reference.call_count, 1)

    def setUp(self):
        case = next(c for c in samples() if c["expected_side"] == "BUY")
        self.tx = replace(transaction_from_record(case["transaction"]),
                          received_at=100., timestamp=100, fresh=True)
        self.c = parse_candidates(self.tx, case["wallet"]).candidates[0]
        self.observations = {"order": snapshot(order_for(self.c))}
        self.intent = VerifiedFeedIntent.verify(self.tx, self.c.wallet, self.c.path,
                                                self.observations, 100.)
        binding = dict(relationship_id="1", follower=A, smart_wallet=self.c.wallet,
                       config_snapshot_hash="synthetic")
        self.policy = dict(binding, observed_at=100, enabled=True, stop_active=False,
            allowed_assets=[R.USDG], allowed_protocols=["relay_solver"],
            execution_providers=["kyber"], max_input_raw="1000000",
            buy_rule={"mode": "fixed", "fixed_amount_raw": "100000"},
            sell_rule={"mode": "proportional", "ratio_ppm": 1000000},
            quote_policy=asdict(QuotePolicy()))
        self.portfolio = dict(binding, observed_at=100, budget_available_raw="1000000",
                              lots=[], source_orphaned=False, consumed_operation_keys=[])
        self.quote = Quote("kyber", "synthetic", 10, "0x" + "ab" * 32, 100,
                           self.c.token_in, self.c.token_out, "100000", "200")
        self.reference = replace(self.quote, amount_in_raw="1000", amount_out_raw="2")
        self.quoter = AsyncMock()
        self.quoter.quote_with_reference.return_value = (self.quote, self.reference, "1")
        self.engine = EarlyDecisionEngine(self.quoter)

    async def evaluate(self):
        return await self.engine.evaluate(self.intent, self.policy, self.portfolio, now=100.1)

    async def test_dynamic_target_passes_without_claiming_execution_or_reserving(self):
        result = await self.evaluate()
        self.assertTrue(result["decision_checks_passed"])
        self.assertFalse(result["copy_eligible"])
        self.assertFalse(result["reservation_created"])
        self.assertEqual(result["minimum_output_raw"], "194")
        signal = result["source_signal"]
        self.assertEqual((signal["stage"], signal["execution_status"]), ("intent", "pending"))
        self.assertIsNone(signal["amount_out_raw"])
        self.assertNotIn("actual_input_debit_raw", signal["evidence"])
        self.assertEqual(result["amount_basis"], "order_payment")

    async def test_factory_reparses_and_freezes_observations(self):
        self.observations["order"]["payload"].clear()
        self.assertTrue((await self.evaluate())["decision_checks_passed"])
        with self.assertRaises(ValueError):
            VerifiedFeedIntent.verify(self.tx, A, self.c.path, {}, 100)
        with self.assertRaises(ValueError):
            await self.engine.evaluate({"recognized_intent": True}, self.policy, self.portfolio, now=100)

    async def test_policy_and_portfolio_gates(self):
        for container, key, value in [(self.policy, "enabled", False),
                (self.policy, "allowed_assets", []), (self.policy, "execution_providers", ["local"]),
                (self.policy, "max_input_raw", "1"), (self.portfolio, "budget_available_raw", "1"),
                (self.portfolio, "config_snapshot_hash", "other"),
                (self.portfolio, "observed_at", 90), (self.portfolio, "source_orphaned", True)]:
            old = container[key]
            with self.subTest(key=key), self.assertRaises(ValueError):
                container[key] = value
                await self.evaluate()
            container[key] = old
        self.quoter.quote_with_reference.assert_not_called()

    async def test_consumed_operation_and_expired_intent_rejected(self):
        result = await self.evaluate()
        self.portfolio["consumed_operation_keys"] = [result["relationship_key"]]
        with self.assertRaisesRegex(ValueError, "already_consumed"):
            await self.evaluate()
        with self.assertRaises(ValueError):
            self.intent.revalidate(107.001)

    async def test_bad_quote_and_reference_rejected(self):
        for quote, reference in [(replace(self.quote, output_asset=A), self.reference),
                (replace(self.quote, amount_in_raw="1"), self.reference),
                (replace(self.quote, amount_out_raw="99"), self.reference),
                (self.quote, replace(self.reference, observed_at=90))]:
            self.quoter.quote_with_reference.return_value = (quote, reference, "1")
            with self.assertRaises(ValueError):
                await self.evaluate()

    async def test_runtime_intent_accepts_seven_seconds_but_not_beyond(self):
        for at in (103.001, 105, 106, 107):
            with self.subTest(at=at):
                self.intent.revalidate(at)
        with self.assertRaisesRegex(ValueError, "feed_intent_expired"):
            self.intent.revalidate(107.001)
        self.assertEqual(self.intent.quote_signal(105).evidence["feed_max_age_seconds"], 7)

    async def test_offline_replay_keeps_original_three_second_default(self):
        from smart_money.early_replay import evaluate_candidate
        self.assertEqual(evaluate_candidate(self.c, 104, self.observations)
                         ["checks"]["freshness"]["reason"], "feed_intent_expired")
        for invalid in (True, 0, -1, 7.001, float("nan"), float("inf"), "7"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                evaluate_candidate(self.c, 104, self.observations, feed_max_age_seconds=invalid)

    async def test_execution_boundary_revalidates_seven_second_limit(self):
        from smart_money.execution_pipeline import check_early_execution_source
        store = MagicMock()
        store.connection.execute.return_value.fetchall.return_value = []
        signal = self.intent.quote_signal(100)
        proposal = dict(source_tx_hash=self.c.tx_hash, input_asset=self.c.token_in,
                        output_asset=self.c.token_out, proposal_id="synthetic", status="reserved",
                        attribution=dict(early_trial_id="synthetic", smart_wallet=self.c.wallet,
                                         copy_operation_order_id=self.c.order_id))
        for at in (104, 106, 107):
            check_early_execution_source(store, self.intent, signal, proposal, now=at)
        store._check_early_trial.assert_called_with(proposal["attribution"], 107)
        with self.assertRaisesRegex(ValueError, "feed_intent_expired"):
            check_early_execution_source(store, self.intent, signal, proposal, now=107.001)

    async def test_extended_intent_still_needs_fresh_quote_and_portfolio(self):
        self.policy["observed_at"] = self.portfolio["observed_at"] = 104
        self.quoter.quote_with_reference.return_value = (
            replace(self.quote, observed_at=104), replace(self.reference, observed_at=104), "1")
        result = await self.engine.evaluate(self.intent, self.policy, self.portfolio, now=105)
        self.assertTrue(result["decision_checks_passed"])
        self.quoter.quote_with_reference.return_value = (self.quote, self.reference, "1")
        with self.assertRaisesRegex(ValueError, "quote_missing_or_expired"):
            await self.engine.evaluate(self.intent, self.policy, self.portfolio, now=105)
        self.portfolio["observed_at"] = 100
        with self.assertRaisesRegex(ValueError, "snapshot expired"):
            await self.engine.evaluate(self.intent, self.policy, self.portfolio, now=105)

    async def test_proportional_buy_uses_order_payment_not_destination_actual(self):
        self.policy["buy_rule"] = {"mode": "proportional", "ratio_ppm": 100}
        self.assertEqual((await self.evaluate())["amount_in_raw"], "100000")

    async def test_sell_requires_verified_account_and_confirmed_relationship_lots(self):
        case = next(c for c in samples() if c["expected_side"] == "SELL")
        tx = replace(transaction_from_record(case["transaction"]), received_at=100., timestamp=100, fresh=True)
        c = parse_candidates(tx, case["wallet"]).candidates[0]
        obs = {"account": snapshot({"chain_id": R.CHAIN_ID, "wallet": c.wallet,
            "code": "0xef0100" + R.SIMPLE_ACCOUNT[2:], "block_hash": "0x" + "ab" * 32})}
        self.intent = VerifiedFeedIntent.verify(tx, c.wallet, c.path, obs, 100)
        self.policy.update(smart_wallet=c.wallet, allowed_protocols=["kyber"], max_input_raw=str(10 ** 19))
        self.portfolio["smart_wallet"] = c.wallet
        lot = dict(lot_id="lot", created_at=90, relationship_id="1", token=c.token_in,
                   principal_asset=R.USDG, token_remaining_raw=str(10 ** 18), reserved_raw="0",
                   source_remaining_raw=c.declared_input_raw, source_position_status="pending")
        self.portfolio["lots"] = [lot]
        with self.assertRaisesRegex(ValueError, "basis_unconfirmed"):
            await self.evaluate()
        lot["source_position_status"] = "confirmed"
        minimum = (int(c.minimum_output_raw) * 10 ** 18 + int(c.declared_input_raw) - 1) // int(c.declared_input_raw)
        output = max(100, ((minimum * 2 + 99) // 100) * 100)
        q = replace(self.quote, input_asset=c.token_in, output_asset=c.token_out,
                    amount_in_raw=str(10 ** 18), amount_out_raw=str(output))
        ref = replace(q, amount_in_raw=str(10 ** 16), amount_out_raw=str(output // 100))
        self.quoter.quote_with_reference.return_value = (q, ref, "1")
        result = await self.evaluate()
        self.assertEqual(result["amount_in_raw"], str(10 ** 18))
        self.assertEqual(result["amount_basis"], "declared_sell")
        lot["relationship_id"] = "2"
        with self.assertRaisesRegex(ValueError, "lot_relationship_mismatch"):
            await self.evaluate()
