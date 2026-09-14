import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from smart_money.early_metrics import summarize_early_trial
from smart_money.runtime_safety import runtime_instance_lock, trip_execution_stop


class EarlyMetricsTests(unittest.TestCase):
    def test_missing_samples_are_not_zero_latency_or_zero_error_evidence(self):
        report = summarize_early_trial([], [], [], "trial")
        self.assertEqual(report["latency"], {})
        self.assertIsNone(report["known_misfollow_fraction_of_acknowledged"])

    def test_timing_and_unknown_denominator_are_explicit(self):
        p = dict(proposal_id="p", source_tx_hash="tx", input_asset="usd", output_asset="token",
                 attribution=dict(early_trial_id="trial", smart_wallet="smart", copy_operation_order_id="order",
                                  source_behavior="BUY", source_amount_in_raw="100", early_received_at=100,
                                  early_checked_at=100.1))
        events = [dict(event="live_execution_broadcast", proposal_id="p", observed_at=101),
                  dict(event="source_evidence_available", source_tx_hash="tx", smart_wallet="smart",
                       order_id="order", observed_at=102, stage="relay_buy_evidenced")]
        result = summarize_early_trial([p], [], events, "trial")
        self.assertEqual(result["counts"]["acknowledged_before_strict_evidence"], 1)
        self.assertEqual(result["unknown_source_fraction"], 1)
        self.assertEqual(result["latency"]["feed_to_broadcast_ack"]["p50_ms"], 1000)
        failed = dict(tx_hash="tx", wallet="smart", execution_status="reverted", evidence={"relay_order_id": "order"})
        result = summarize_early_trial([p], [failed], events, "trial")
        self.assertEqual(result["known_misfollow_fraction_of_acknowledged"], 1)


class RuntimeSafetyTests(unittest.TestCase):
    def test_fault_remains_latched_if_stop_file_cannot_be_written(self):
        from smart_money.execution_controls import _stop_controls
        with patch("smart_money.runtime_safety._fault_latched", False), \
             patch("smart_money.runtime_safety.os.open", side_effect=PermissionError("synthetic")):
            with self.assertRaises(PermissionError):
                trip_execution_stop()
            with self.assertRaisesRegex(PermissionError, "critical runtime failure"):
                _stop_controls()

    def test_second_local_lock_fails_then_releases_after_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "runtime.lock"
            with runtime_instance_lock(path):
                with self.assertRaises(RuntimeError):
                    with runtime_instance_lock(path):
                        self.fail("second instance acquired lock")
            with runtime_instance_lock(path):
                self.assertTrue(path.exists())

    def test_critical_stop_is_persistent_and_never_overwrites_existing_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "STOP"
            with patch.dict(os.environ, {"SMART_MONEY_EMERGENCY_STOP_FILE": str(path)}), \
                 patch("smart_money.runtime_safety._fault_latched", False):
                trip_execution_stop()
                before = path.stat().st_ino
                trip_execution_stop()
                self.assertEqual(before, path.stat().st_ino)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
