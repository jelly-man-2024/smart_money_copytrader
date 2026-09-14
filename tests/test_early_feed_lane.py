"""Single-receiver, bounded-lane regression tests; every provider is mocked."""
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from smart_money import cli, registry as R
from smart_money.early_feed_lane import EarlyEvidenceResolver, EarlyFeedLane
from smart_money.early_intent import parse_candidates
from smart_money.store import Store
from test_early_shadow import fixture
from test_early_feed import order_for


class LaneTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.tx, self.wallet = fixture()
        self.resolver = AsyncMock(return_value={"candidates": [], "copy_eligible": False})
        self.lane = EarlyFeedLane(self.store, self.resolver, queue_size=1, workers=1)
        self.lane.start()
        self.addAsyncCleanup(self.lane.close)

    def row(self, tx=None):
        return self.store.connection.execute("SELECT status,result_payload FROM early_feed_jobs WHERE tx_hash=?",
                                             ((tx or self.tx).hash,)).fetchone()

    async def test_durable_first_duplicate_and_isolated_copy(self):
        self.assertFalse(self.lane.submit(self.tx))
        self.store.put_candidate(self.tx)
        self.assertTrue(self.lane.submit(self.tx))
        self.assertFalse(self.lane.submit(self.tx))
        original_hash = self.tx.hash
        self.tx = replace(self.tx, data=b"changed-after-enqueue")
        await self.lane.queue.join()
        self.resolver.assert_awaited_once()
        passed = self.resolver.await_args.args[0]
        self.assertNotEqual(passed.data, self.tx.data)
        self.assertEqual(passed.hash, original_hash)
        self.assertEqual(self.row()[0], "done")

    async def test_full_queue_is_recorded_without_blocking_receiver(self):
        other = replace(self.tx, hash="0x" + "ab" * 32)
        for tx in (self.tx, other):
            self.store.put_candidate(tx)
        self.assertTrue(self.lane.submit(self.tx))
        self.assertFalse(self.lane.submit(other))
        self.assertEqual(self.row(other)[0], "expired")
        self.assertEqual(json.loads(self.row(other)[1])["reason"], "queue_full_strict_fallback")
        await self.lane.queue.join()

    async def test_stale_backfill_and_unhealthy_never_start_resolver(self):
        for tx in (replace(self.tx, timestamp=int(time.time()) - 10),
                   replace(self.tx, observation_source="backfill"), replace(self.tx, fresh=False)):
            self.store.put_candidate(tx)
            self.assertFalse(self.lane.submit(tx))
        self.lane.healthy = lambda: False
        self.assertFalse(self.lane.submit(self.tx))
        self.resolver.assert_not_called()

    async def test_health_rechecked_after_queue_wait(self):
        self.store.put_candidate(self.tx)
        self.lane.submit(self.tx)
        self.lane.healthy = lambda: False
        await self.lane.queue.join()
        self.assertEqual(self.row()[0], "expired")
        self.resolver.assert_not_called()

    async def test_late_resolution_is_not_eligible(self):
        async def resolve(tx):
            self.lane.healthy = lambda: False
            return {"recognized_count": 1, "copy_eligible": False}
        self.lane.resolver = resolve
        self.store.put_candidate(self.tx)
        self.lane.submit(self.tx)
        await self.lane.queue.join()
        self.assertEqual(self.row()[0], "expired")
        self.assertFalse(json.loads(self.row()[1])["eligible_after_processing"])

    async def test_error_does_not_leak_provider_url_or_kill_worker(self):
        self.resolver.side_effect = ValueError("https://secret-provider/key")
        self.store.put_candidate(self.tx)
        self.lane.submit(self.tx)
        await self.lane.queue.join()
        self.assertEqual(self.row()[0], "failed")
        self.assertNotIn("secret", self.row()[1])
        self.assertFalse(self.lane.tasks[0].done())

    async def test_shutdown_marks_pending_interrupted_and_never_replays(self):
        self.store.put_candidate(self.tx)
        self.lane.submit(self.tx)
        await self.lane.close()
        self.assertEqual(self.row()[0], "interrupted")
        self.lane.start()
        self.assertFalse(self.lane.submit(self.tx))
        # A NEW lane/process has no in-memory pending entries to replay.
        await self.lane.close()

    async def test_buy_resolver_recognizes_order_but_never_grants_execution(self):
        candidate = parse_candidates(self.tx, self.wallet).candidates[0]
        rpc = MagicMock(call=AsyncMock())
        relay = MagicMock(lookup_by_order=AsyncMock(return_value=order_for(candidate)))
        # Keep the original signed permit; replay inside its validity window.
        at = int(candidate.metadata["permit_deadline"]) - 1
        tx = replace(self.tx, timestamp=at, received_at=float(at))
        with patch("smart_money.early_feed_lane.time.time", return_value=float(at)):
            result = await EarlyEvidenceResolver(rpc, relay, [self.wallet])(tx)
        relay.lookup_by_order.assert_awaited_once_with(candidate.order_id, None)
        self.assertTrue(result["candidates"][0]["recognized_intent"])
        self.assertFalse(result["candidates"][0]["copy_eligible"])
        rpc.call.assert_not_called()

    async def test_sell_resolver_uses_code_snapshot_without_order_lookup(self):
        tx, wallet = fixture("SELL")
        rpc = MagicMock(call=AsyncMock(side_effect=[{"hash": "0x" + "ab" * 32, "number": "0xa"},
                                                  "0xef0100" + R.SIMPLE_ACCOUNT[2:]]))
        relay = MagicMock(lookup_by_order=AsyncMock())
        result = await EarlyEvidenceResolver(rpc, relay, [wallet])(tx)
        self.assertTrue(result["candidates"][0]["recognized_intent"])
        self.assertTrue(rpc.call.await_args.args[1][1]["requireCanonical"])
        relay.lookup_by_order.assert_not_called()

    async def test_expired_permit_does_not_spend_order_queries(self):
        candidate = parse_candidates(self.tx, self.wallet).candidates[0]
        at = int(candidate.metadata["permit_deadline"]) + 1
        tx = replace(self.tx, timestamp=at, received_at=float(at))
        rpc = MagicMock(call=AsyncMock())
        relay = MagicMock(lookup_by_order=AsyncMock())
        with patch("smart_money.early_feed_lane.time.time", return_value=float(at)):
            result = await EarlyEvidenceResolver(rpc, relay, [self.wallet])(tx)
        self.assertFalse(result["candidates"][0]["recognized_intent"])
        self.assertEqual(result["candidates"][0]["checks"]["freshness"]["reason"], "permit_expired")
        relay.lookup_by_order.assert_not_called()
        rpc.call.assert_not_called()

    async def test_evidence_write_failure_does_not_erase_strict_candidate(self):
        self.store.put_candidate(self.tx)
        self.store.connection.execute("""CREATE TRIGGER reject_lane BEFORE INSERT ON early_feed_jobs
            BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        with self.assertRaises(Exception):
            self.lane.submit(self.tx)
        self.assertFalse(self.store.connection.in_transaction)
        self.assertEqual(self.store.connection.execute(
            "SELECT COUNT(*) FROM candidates WHERE tx_hash=?", (self.tx.hash,)).fetchone()[0], 1)
        self.resolver.assert_not_called()


class MonitorLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_trial_uses_same_feed_and_enrolls_strict_fallback(self):
        from smart_money.paper_config import WalletPaperPolicy
        from smart_money.paper import AmountRule
        from smart_money.quotes import QuotePolicy
        tx, wallet = fixture()
        follower = "0x" + "11" * 20
        policy = WalletPaperPolicy(wallet, "synthetic", follower, "1", "mainnet_live",
            {"USDG": "1000"}, {"USDG": AmountRule("fixed", fixed_amount_raw="100")},
            AmountRule("proportional", ratio_ppm=1000000), "test", "evidenced", (), QuotePolicy(),
            frozenset({"relay_solver", "kyber"}), frozenset({R.USDG}), frozenset(), (), "a"*64, ("kyber",))
        config = SimpleNamespace(relationships=(policy,), wallets={wallet: policy},
                                 policies_for=lambda w: (policy,))
        db = Store(":memory:")
        db.start_early_trial("trial", follower, [1])
        db.start_paper_budget_cycle("test", "synthetic")
        db.configure_paper_budget(policy.ledger_scope, "USDG", "1000")
        completed = asyncio.Event()
        async def handoff(*args):
            completed.set()
        class Rpc:
            async def call(self, method, params=None):
                return hex(R.CHAIN_ID)
            async def receipt(self, tx_hash):
                await completed.wait()
                return None
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def __aiter__(self): return self.frames()
            async def frames(self):
                yield "frame"
                await asyncio.Event().wait()
        args = cli.parser().parse_args(["run", "--seconds", "0.03", "--early-trial-id", "trial"])
        scanner = MagicMock(scan_once=AsyncMock(return_value=SimpleNamespace(initialized=False)))
        with patch.object(cli, "load_endpoint_env"), patch.object(cli, "runtime_paper_config", return_value=config), \
             patch.object(cli, "runtime_store", return_value=db), patch.object(cli, "ReadOnlyRpc", return_value=Rpc()), \
             patch.object(cli, "monitoring_watchlist", return_value={wallet: {}}), \
             patch.object(cli, "validate_live_relationships"), patch.object(cli, "MySqlRelationshipGate"), \
             patch.object(cli, "MainnetBroadcaster"), patch.object(cli, "FeedHealth") as health, \
             patch.object(cli, "BlockScanner", return_value=scanner), \
             patch.object(cli, "Decoder", return_value=MagicMock(delegations={}, decode=MagicMock(return_value=[]))), \
             patch.object(cli, "envelopes", return_value=[(b"raw", {"fresh": True})]), \
             patch.object(cli, "decode_raw", return_value=tx), \
             patch.object(cli, "EarlyEvidenceResolver", return_value=AsyncMock(return_value={"candidates": []})), \
             patch.object(cli, "EarlyRuntime", return_value=handoff) as runtime, \
             patch.object(cli, "PaperEngine", wraps=cli.PaperEngine) as engines, \
             patch.object(cli.websockets, "connect", return_value=Socket()) as connect, patch.object(cli, "report"):
            health.return_value.healthy.return_value = True
            health.return_value.gap = False
            await asyncio.wait_for(cli.monitor(args), 2)
            self.assertTrue(completed.is_set())
            self.assertEqual(connect.call_count, 1)
            runtime.assert_called_once()
            self.assertTrue(engines.call_args.args[9][wallet]["operation_claims"])

    async def test_one_feed_connection_resolves_while_receipt_waits(self):
        tx, wallet = fixture()
        evidence_done = asyncio.Event()
        calls = []
        async def resolve(value):
            calls.append("evidence")
            evidence_done.set()
            return {"candidates": [], "copy_eligible": False}
        class Rpc:
            async def call(self, method, params=None):
                return hex(R.CHAIN_ID)
            async def receipt(self, tx_hash):
                await asyncio.wait_for(evidence_done.wait(), 1)
                calls.append("receipt")
                return None
        class Socket:
            async def __aenter__(self):
                return self
            async def __aexit__(self, *args):
                pass
            def __aiter__(self):
                return self.frames()
            async def frames(self):
                yield "mock-frame"
                await asyncio.Event().wait()
        with tempfile.TemporaryDirectory() as folder:
            args = cli.parser().parse_args(["monitor", "--seconds", "0.03", "--db",
                                          str(Path(folder) / "monitor.sqlite3"), "--early-feed-evidence"])
            health = MagicMock(gap=False, max_age_seconds=3)
            health.healthy.return_value = True
            scanner = MagicMock(scan_once=AsyncMock(return_value=SimpleNamespace(initialized=False)))
            decoder = MagicMock(delegations={}, decode=MagicMock(return_value=[]))
            with patch.object(cli, "load_endpoint_env"), patch.object(cli, "ReadOnlyRpc", return_value=Rpc()), \
                 patch.object(cli, "monitoring_watchlist", return_value={wallet: {}}), \
                 patch.object(cli, "Decoder", return_value=decoder), patch.object(cli, "FeedHealth", return_value=health), \
                 patch.object(cli, "BlockScanner", return_value=scanner), \
                 patch.object(cli, "envelopes", return_value=[(b"raw", {"fresh": True})]), \
                 patch.object(cli, "decode_raw", return_value=tx), \
                 patch.object(cli, "EarlyEvidenceResolver", return_value=resolve), \
                 patch.object(cli.websockets, "connect", return_value=Socket()) as connect, \
                 patch.object(cli, "report"):
                await asyncio.wait_for(cli.monitor(args), 2)
                self.assertEqual(connect.call_count, 1)
            self.assertEqual(calls[:2], ["evidence", "receipt"])
            db = Store(args.db)
            self.assertEqual(db.connection.execute("SELECT status FROM early_feed_jobs").fetchone()[0], "done")
            self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0], 0)
            db.close()

    async def test_cli_defaults_do_not_enable_evidence_or_early_trading(self):
        for command in ("monitor", "run"):
            args = cli.parser().parse_args([command])
            self.assertFalse(args.early_feed_evidence)
