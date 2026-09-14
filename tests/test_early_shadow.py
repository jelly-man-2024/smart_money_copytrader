"""No-network regression tests for the explicitly opted-in shadow collector."""
import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from smart_money import registry as R
from smart_money.early_intent import WRAPPER_SIGNATURE_HINT, parse_candidates
from smart_money.early_replay import _snapshot, transaction_from_record
from smart_money.early_shadow import JsonlSink, ShadowCollector
from smart_money.early_shadow_mysql import BoundedReadCursor, ShadowBusinessReader
from smart_money.paper_config import load_paper_config, parse_paper_config
from scripts.observe_early_feed import main
from scripts.summarize_early_shadow import summarize
from eth_utils import keccak

ROOT = Path(__file__).resolve().parents[1]


def fixture(side="BUY"):
    rows = json.loads((ROOT / "data/early_feed_public_samples_2026-09-14.json").read_text())["cases"]
    row = next(r for r in rows if r["expected_side"] == side)
    tx = transaction_from_record(row["transaction"])
    return replace(tx, received_at=time.time(), timestamp=int(time.time()), fresh=True), row["wallet"]


class ShadowTests(unittest.TestCase):
    def test_default_disabled_does_not_load_endpoints_or_mysql(self):
        with patch("scripts.observe_early_feed.capture") as network, patch("builtins.print"):
            self.assertEqual(main([]), 0)
            network.assert_not_called()

    def test_exclusive_output_and_size_limit(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "test.jsonl"
            sink = JsonlSink(path, max_bytes=16)
            sink.write({"x": 1})
            with self.assertRaises(ValueError):
                sink.write({"oversize": "x" * 20})
            sink.close()
            with self.assertRaises(FileExistsError):
                JsonlSink(path)
            self.assertEqual(json.loads(path.read_text()), {"x": 1})

    def test_readonly_rollback_and_close_on_query_failure(self):
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        reader = ShadowBusinessReader(lambda: connection)
        def fail(q):
            q.execute("DELETE FROM signals")
        with self.assertRaises(PermissionError):
            reader.read(fail)
        cursor.execute.assert_called_once_with("START TRANSACTION READ ONLY")
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_read_cursor_snapshot_timeout(self):
        cursor = BoundedReadCursor(MagicMock())
        cursor.deadline = 0
        with self.assertRaises(TimeoutError):
            cursor.execute("SELECT 1")

    def test_business_snapshot_binds_budget_configuration_and_same_order(self):
        tx, wallet = fixture()
        candidate = parse_candidates(tx, wallet).candidates[0]
        template = json.loads((ROOT / "config/paper.example.json").read_text())
        p = template["wallets"][0]
        row = {k: template[k] for k in ("strategy_version", "trigger_mode", "shadow_trigger_modes",
                                        "quote_policy", "allowed_protocols", "allowed_assets", "allowed_routes")}
        row.update(id=7, enabled=True, follower_wallet="0x" + "11" * 20,
                   smart_wallet=wallet, smart_wallet_label="test", run_mode="paper")
        for prefix, rule in (("usdg", p["buy_rules"]["USDG"]),
                             ("eth", p["buy_rules"]["ETH_WETH"]), ("sell", p["sell_rule"])):
            row[prefix + "_rule_mode"] = rule["mode"]
            row[prefix + "_ratio_ppm"] = rule.get("ratio_ppm")
            row[prefix + "_fixed_amount_raw"] = rule.get("fixed_amount_raw")
        row.update(usdg_budget_limit_raw=p["budget_limits"]["USDG"],
                   eth_budget_limit_raw=p["budget_limits"]["ETH_WETH"])
        connection = MagicMock()
        cursor = connection.cursor.return_value.__enter__.return_value
        cursor.fetchall.side_effect = [[row], [{"limit_raw": "100", "invested_raw": "40",
                                                "reserved_raw": "10"}], [],
                                      [{"source_tx_hash": "0x" + "ab" * 32,
                                        "payload": json.dumps({"evidence": {"relay_order_id": candidate.order_id}})}], []]
        result = ShadowBusinessReader(lambda: connection).capture(candidate)
        snap = result["relationships"][0]["snapshots"]
        self.assertEqual(snap["portfolio"]["payload"]["budget_available_raw"], "50")
        self.assertEqual(len(snap["portfolio"]["payload"]["consumed_operation_keys"]), 1)
        self.assertEqual(snap["policy"]["payload"]["config_snapshot_hash"],
                         snap["portfolio"]["payload"]["config_snapshot_hash"])
        queries = [c.args[0] for c in cursor.execute.call_args_list]
        self.assertTrue(all(q.lstrip().startswith("SELECT") for q in queries[1:]))
        self.assertTrue(any("$.source_event_id" in q for q in queries))
        connection.rollback.assert_called_once()
        connection.close.assert_called_once()

    def test_policy_parser_preserves_snapshot_hash(self):
        path = ROOT / "config/paper.example.json"
        self.assertEqual(load_paper_config(path), parse_paper_config(json.loads(path.read_text())))

    def test_slow_database_capture_is_not_fresh(self):
        snapshots = {"policy": {"payload": {}, "observed_at": 100,
                                "capture_started_at": 90, "provenance": "test"}}
        with self.assertRaisesRegex(ValueError, "capture_expired"):
            _snapshot(snapshots, "policy", 100, 3)

    def test_signature_hint_is_correct_but_not_verification(self):
        self.assertEqual(keccak(text=WRAPPER_SIGNATURE_HINT)[:4].hex(), "998b5942")
        t = json.loads((ROOT / "data/relay_0a2b8f36_sample_a_2026-09-14.json").read_text())["transaction"]
        tx, _ = fixture()
        c = parse_candidates(replace(tx, hash=t["hash"], data=bytes.fromhex(t["input"][2:])),
                             "0x1cfbe3af88266ccca29372661f45261c7d19be09").candidates[0]
        self.assertFalse(c.blockers)
        self.assertIn("required_runtime_code_hash", c.metadata)
        self.assertFalse(c.metadata["signature_hint_is_contract_verification"])
        self.assertEqual(len(c.metadata["routes"]), 3)
        self.assertTrue(all(r["selected_or_executed"] == "unknown" for r in c.metadata["routes"]))


class AsyncShadowTests(unittest.IsolatedAsyncioTestCase):
    def collector(self, **kwargs):
        self.records = []
        sink = MagicMock()
        sink.write.side_effect = lambda row: self.records.append(deepcopy(row))
        self.rpc = MagicMock(call=AsyncMock())
        self.relay = MagicMock(lookup_by_order=AsyncMock(return_value={"requests": []}))
        self.business = MagicMock()
        self.business.capture.return_value = {"relationships": []}
        self.business.truth.return_value = []
        collector = ShadowCollector(self.rpc, self.relay, self.business, sink, **kwargs)
        collector.write_lock = asyncio.Lock()
        collector.truth_queue = asyncio.Queue(256)
        return collector

    async def test_queue_drop_and_dedup_are_visible(self):
        c = self.collector(queue_size=1)
        tx, wallet = fixture()
        c.enqueue(tx, [wallet])
        c.enqueue(tx, [wallet])
        c.enqueue(replace(tx, hash="0x" + "aa" * 32), [wallet])
        self.assertEqual(c.stats["duplicate_transactions"], 1)
        self.assertEqual(c.stats["queue_dropped_transactions"], 1)

    async def test_buy_records_missing_order_without_using_truth(self):
        c = self.collector()
        tx, wallet = fixture()
        await c.observe(tx, wallet)
        self.business.truth.assert_not_called()
        candidate = parse_candidates(tx, wallet).candidates[0]
        self.relay.lookup_by_order.assert_awaited_once_with(
            candidate.order_id, candidate.metadata.get("request_hint"))
        row = next(r for r in self.records if r["record_type"] == "early_case")
        self.assertEqual(row["case"]["transaction"]["value"], "0")
        self.assertNotIn("truth", row["case"])
        self.assertLessEqual(row["case"]["snapshots"]["order"]["observed_at"], row["case"]["decision_at"])
        self.assertFalse(row["result"]["decision_passed"])
        self.assertFalse(row["copy_eligible"])
        self.assertFalse(row["result"]["preparation_passed"])

    async def test_sell_code_is_block_hash_pinned(self):
        c = self.collector()
        tx, wallet = fixture("SELL")
        block_hash = "0x" + "ab" * 32
        self.rpc.call.side_effect = [{"hash": block_hash, "number": "0x20"},
                                     "0xef0100" + R.SIMPLE_ACCOUNT[2:]]
        await c.observe(tx, wallet)
        self.rpc.call.assert_any_await("eth_getCode", [wallet, {"blockHash": block_hash,
                                                               "requireCanonical": True}])
        self.relay.lookup_by_order.assert_not_called()
        row = next(r for r in self.records if r["record_type"] == "early_case")
        self.assertEqual(row["result"]["checks"]["attribution"]["status"], "pass")

    async def test_wrapper_captures_real_code_and_requires_hash_match(self):
        t = json.loads((ROOT / "data/relay_0a2b8f36_sample_a_2026-09-14.json").read_text())["transaction"]
        f = json.loads((ROOT / "data/relay_race_runtime_2026-09-14.json").read_text())
        for code, status in ((f["runtime_code"], "pass"), ("0x00", "reject")):
            c = self.collector()
            tx, _ = fixture()
            tx = replace(tx, data=bytes.fromhex(t["input"][2:]), hash=t["hash"])
            self.rpc.call.side_effect = [{"hash": "0x" + "ab" * 32, "number": "0x20"}, code]
            await c.observe(tx, "0x1cfbe3af88266ccca29372661f45261c7d19be09")
            row = next(r for r in self.records if r["record_type"] == "early_case")
            self.assertEqual(row["result"]["checks"]["deployment"]["status"], status)
            self.assertEqual(row["case"]["snapshots"]["deployment"]["payload"]["code"], code)
            self.assertFalse(row["result"]["decision_passed"])

    async def test_transport_error_redacts_sensitive_message(self):
        c = self.collector()
        self.relay.lookup_by_order.side_effect = ValueError("https://secret.invalid/key")
        tx, wallet = fixture()
        await c.observe(tx, wallet)
        encoded = json.dumps(self.records)
        self.assertNotIn("secret.invalid", encoded)
        self.assertIn('"order": "ValueError"', encoded)

    async def test_reconciliation_separate_and_reorg_can_replace_match(self):
        c = self.collector(reconcile_seconds=0)
        tx, wallet = fixture("SELL")
        candidate = parse_candidates(tx, wallet).candidates[0]
        truth = {"tx_hash": tx.hash, "wallet": wallet, "path": candidate.path,
                 "canonical_status": "orphaned"}
        self.business.truth.return_value = [truth]
        await c.truth_queue.put(("test", candidate, 0))
        worker = asyncio.create_task(c.truth_worker())
        await asyncio.wait_for(c.truth_queue.join(), 1)
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
        self.assertEqual(self.records[0]["label"], "source_orphaned")
        self.assertEqual(self.records[0]["record_type"], "reconciliation")

    async def test_run_is_bounded_and_finishes_without_frames(self):
        c = self.collector()
        async def frames():
            await asyncio.Event().wait()
            yield "unreachable"
        result = await c.run(frames(), [], seconds=.02)
        self.assertEqual(result["queued_at_stop"], 0)
        self.assertEqual(self.records[-1]["record_type"], "run_finished")

    async def test_feed_to_snapshot_end_to_end_without_network(self):
        c = self.collector()
        tx, wallet = fixture()
        async def frames():
            yield "fake-frame"
            await asyncio.Event().wait()
        with patch("smart_money.early_shadow.envelopes", return_value=[(b"public", {})]), \
             patch("smart_money.early_shadow.decode_raw", return_value=tx):
            result = await c.run(frames(), [wallet], seconds=.1)
        self.assertEqual(result["capture_completed"], 1)
        self.assertEqual(result["candidate_relationships"], 1)
        self.assertTrue(any(r["record_type"] == "early_case" for r in self.records))

    async def test_writer_failure_stops_run(self):
        c = self.collector()
        tx, wallet = fixture()
        original_write = c.sink.write.side_effect
        def write(row):
            if row["record_type"] == "candidate_started":
                raise OSError("disk full")
            original_write(row)
        c.sink.write.side_effect = write
        async def frames():
            yield "frame"
            await asyncio.Event().wait()
        with patch("smart_money.early_shadow.envelopes", return_value=[(b"public", {})]), \
             patch("smart_money.early_shadow.decode_raw", return_value=tx):
            with self.assertRaises(OSError):
                await asyncio.wait_for(c.run(frames(), [wallet], seconds=5), 1)

    async def test_summary_pending_is_not_misfollow(self):
        c = self.collector()
        tx, wallet = fixture()
        await c.observe(tx, wallet)
        report = summarize(self.records)
        self.assertIsNone(report["actual_misfollow_rate"])
        self.assertEqual(report["real_trades_submitted"], 0)
        self.assertEqual(report["candidate_relationships"], 1)


if __name__ == "__main__":
    unittest.main()
