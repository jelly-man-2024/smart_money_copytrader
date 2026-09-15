"""All code checks use mocked RPC and imported public bytecode; no live calls."""
import asyncio
from dataclasses import replace
import json
import unittest
from unittest.mock import AsyncMock, Mock, patch

from smart_money import registry as R
from smart_money.deployment_monitor import DeploymentMonitor
from smart_money.early_feed_lane import EarlyEvidenceResolver
from smart_money.early_intent import parse_candidates
from smart_money.early_replay import evaluate_candidate, transaction_from_record
from smart_money.relay_race import RACE_ADDRESS, RACE_CODE_HASH
from smart_money.verified_feed_intent import VerifiedFeedIntent
from test_early_feed import ROOT, samples, order_for, snapshot


class DeploymentMonitorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.code = json.loads((ROOT / "data/relay_race_runtime_2026-09-14.json").read_text())["runtime_code"]
        self.now = 90.
        self.report = Mock()
        self.rpc = Mock(call=AsyncMock(side_effect=self.call))
        self.monitor = DeploymentMonitor(self.rpc, self.report, clock=lambda: self.now)
        original = transaction_from_record(next(c for c in samples() if c["expected_side"] == "BUY")["transaction"])
        wrapper = json.loads((ROOT / "data/relay_0a2b8f36_sample_a_2026-09-14.json").read_text())["transaction"]
        self.tx = replace(original, hash=wrapper["hash"], data=bytes.fromhex(wrapper["input"][2:]),
                          timestamp=100, received_at=100., fresh=True)
        self.wallet = "0x1cfbe3af88266ccca29372661f45261c7d19be09"
        self.c = parse_candidates(self.tx, self.wallet).candidates[0]
        self.order = order_for(self.c)
        self.order["requests"][0]["id"] = self.c.metadata["request_hint"]

    async def call(self, method, params=None):
        if method == "eth_chainId":
            return hex(R.CHAIN_ID)
        if method == "eth_getBlockByNumber":
            self.assertEqual(params, ["latest", False])
            return {"hash": "0x" + "ab" * 32, "number": "0xa"}
        self.assertEqual(method, "eth_getCode")
        self.assertEqual(params, [RACE_ADDRESS, {"blockHash": "0x" + "ab" * 32, "requireCanonical": True}])
        return self.code

    def observations(self):
        return {"order": snapshot(self.order), "deployment": self.monitor.snapshot()}

    def intent(self, observations=None):
        return VerifiedFeedIntent.verify(self.tx, self.wallet, self.c.path,
            observations or self.observations(), 100., deployment_monitor=self.monitor)

    async def test_initial_failure_blocks_only_until_first_success(self):
        self.rpc.call.side_effect = TimeoutError("https://secret.invalid/key")
        await self.monitor.check_once()
        with self.assertRaisesRegex(ValueError, "not_yet_verified"):
            self.monitor.snapshot()
        self.assertNotIn("secret", str(self.report.call_args_list))
        self.rpc.call.side_effect = self.call
        await self.monitor.check_once()
        self.assertTrue(self.monitor.status()["ready"])

    async def test_errors_keep_old_success_without_ttl_or_timestamp_reset(self):
        await self.monitor.check_once()
        before = self.monitor.snapshot()
        self.rpc.call.side_effect = TimeoutError("secret")
        for _ in range(3):
            self.now += 1000
            await self.monitor.check_once()
        self.assertEqual(self.monitor.snapshot(), before)
        self.assertEqual(self.monitor.status()["seconds_since_success"], 3000)
        self.assertEqual(self.monitor.status()["consecutive_errors"], 3)
        self.rpc.call.side_effect = self.call
        await self.monitor.check_once()
        self.assertEqual(self.monitor.status()["consecutive_errors"], 0)

    async def test_changed_code_revokes_existing_intent_and_latches(self):
        await self.monitor.check_once()
        intent = self.intent()
        intent.revalidate(104.)
        original = self.code
        self.code = "0x00" + self.code[4:]
        self.now = 104.
        await self.monitor.check_once()
        with self.assertRaisesRegex(ValueError, "deployment_code_changed"):
            intent.revalidate(104.)
        self.code = original
        await self.monitor.check_once()
        with self.assertRaisesRegex(ValueError, "deployment_code_changed"):
            self.monitor.snapshot()
        self.rpc.call.side_effect = TimeoutError()
        await self.monitor.check_once()
        self.assertFalse(self.monitor.status()["ready"])

    async def test_empty_code_is_mismatch_not_transport_failure(self):
        self.code = "0x"
        await self.monitor.check_once()
        self.assertTrue(self.monitor.status()["changed"])
        self.assertIsNone(self.monitor.last_success_at)

    async def test_malformed_code_and_wrong_chain_do_not_replace_cache(self):
        await self.monitor.check_once()
        before = self.monitor.snapshot()
        for code in (None, "invalid", "0xabc", "0xaa bb"):
            self.code = code
            await self.monitor.check_once()
            self.assertEqual(self.monitor.snapshot(), before)
        self.rpc.call.side_effect = AsyncMock(return_value="0x1")
        await self.monitor.check_once()
        self.assertEqual(self.monitor.snapshot(), before)

    async def test_shared_cache_has_no_rpc_on_candidate_or_revalidation(self):
        await self.monitor.check_once()
        self.rpc.call.reset_mock()
        relay = Mock(lookup_by_order=AsyncMock(return_value=self.order))
        resolver = EarlyEvidenceResolver(self.rpc, relay, [self.wallet], deployment_monitor=self.monitor)
        with patch("smart_money.early_feed_lane.time.time", return_value=100.):
            for _ in range(2):
                result = await resolver(self.tx)
                self.assertTrue(result["candidates"][0]["recognized_intent"])
        intent = self.intent(result["candidates"][0]["snapshots"])
        intent.revalidate(107.)
        with self.assertRaisesRegex(ValueError, "feed_intent_expired"):
            intent.revalidate(107.001)
        self.rpc.call.assert_not_called()
        self.assertEqual(intent.deployment_evidence()["runtime_code_hash"], RACE_CODE_HASH)
        self.assertEqual(intent.deployment_evidence()["observed_at"], 90.)
        self.assertEqual(intent.deployment_evidence()["validation_mode"], "periodic_monitor")

    async def test_snapshot_copy_and_historical_ttl_still_enforced(self):
        await self.monitor.check_once()
        observations = self.observations()
        self.assertEqual(evaluate_candidate(self.c, 100., observations)["checks"]["deployment"]["reason"],
                         "deployment_snapshot_expired")
        intent = self.intent(observations)
        observations["deployment"]["payload"]["code"] = "0x"
        intent.revalidate(104.)
        self.assertEqual(self.monitor.snapshot()["payload"]["code"], self.code)
        with self.assertRaisesRegex(ValueError, "race_runtime_code_mismatch"):
            self.intent(observations)

    async def test_future_snapshot_never_becomes_valid_by_cache_flag(self):
        await self.monitor.check_once()
        observations = self.observations()
        observations["deployment"]["observed_at"] = 101.
        with self.assertRaisesRegex(ValueError, "not_available"):
            self.intent(observations)

    async def test_account_snapshot_keeps_three_second_ttl(self):
        await self.monitor.check_once()
        case = next(c for c in samples() if c["expected_side"] == "SELL")
        tx = replace(transaction_from_record(case["transaction"]), timestamp=100, received_at=100.)
        candidate = parse_candidates(tx, case["wallet"]).candidates[0]
        observations = {"account": snapshot({"chain_id": R.CHAIN_ID, "wallet": candidate.wallet,
            "code": "0xef0100" + R.SIMPLE_ACCOUNT[2:], "block_hash": "0x" + "ab" * 32}, at=90.)}
        result = evaluate_candidate(candidate, 100., observations, deployment_monitor=self.monitor)
        self.assertEqual(result["checks"]["attribution"]["reason"], "account_snapshot_expired")

    async def test_periodic_task_and_shutdown(self):
        # Only change the timer in the isolated test, not production configuration.
        with patch("smart_money.deployment_monitor.DEPLOYMENT_CHECK_INTERVAL_SECONDS", 0.001):
            await self.monitor.start()
            try:
                for _ in range(20):
                    if self.rpc.call.await_count >= 6:
                        break
                    await asyncio.sleep(0.001)
                self.assertGreaterEqual(self.rpc.call.await_count, 6)
            finally:
                await self.monitor.close()
        self.assertTrue(self.monitor._task.done())
        with self.assertRaisesRegex(ValueError, "closed"):
            self.monitor.snapshot()
