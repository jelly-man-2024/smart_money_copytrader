"""Local trial-limit tests. No key access, RPC, approval or real broadcasts."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from smart_money.early_trial import TRIAL_SECONDS
from smart_money.store import Store
from test_copy_operation import A, B, ORDER, TX, proposal


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "trial.sqlite3"
        self.db = Store(self.path)
        self.addCleanup(self.db.close)
        self.now = 100.0
        self.clock = patch("smart_money.early_trial.time.time", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.db.start_paper_budget_cycle("cycle", "test")
        self.db.configure_paper_budget(A, "USDG", "1000000")
        self.db.start_early_trial("trial", B, [1, 2])

    def reserve(self, n=1, signed=True, **attr):
        p = proposal("p" + str(n))
        p["attribution"].update(early_trial_id="trial", source_behavior="BUY",
                                 copy_operation_order_id="0x" + format(n, "064x"), **attr)
        self.db.reserve_paper_proposal(p)
        if signed:
            c = self.db.connection
            c.execute("""INSERT INTO execution_nonce_reservations
                (reservation_id,follower_wallet,relationship_id,proposal_id,chain_id,nonce,status)
                VALUES(?,?,'1',?,4663,?,'signed')""", ("n" + str(n), B, p["proposal_id"], n))
            c.execute("""INSERT INTO execution_plans
                (plan_id,proposal_id,follower_wallet,relationship_id,config_snapshot_hash,
                 nonce_reservation_id,status,plan_payload,preflight_payload,signed_tx_hash)
                VALUES(?, ?,?,'1','snapshot',?,'signed','{}','{}',?)""",
                ("plan" + str(n), p["proposal_id"], B, "n" + str(n), TX))
            c.commit()
        return p

    def status(self):
        return self.db.early_trial_status("trial")

    def test_restart_and_repeated_start_never_reset_window_or_count(self):
        self.reserve()
        self.db.mark_copy_operation_broadcast_attempted("p1")
        self.now = 1000
        db = Store(self.path)
        self.addCleanup(db.close)
        result = db.start_early_trial("trial", B, [2, 1])
        self.assertEqual(result["started_at"], 100)
        self.assertEqual(result["expires_at"], 100 + TRIAL_SECONDS)
        self.assertEqual(result["consumed_slots"], 1)
        with self.assertRaisesRegex(ValueError, "renewal forbidden"):
            db.start_early_trial("new-trial", B, [1, 2])

    def test_exact_time_boundary_rejects_reserved_task_without_consuming_slot(self):
        self.reserve()
        self.now = 100 + TRIAL_SECONDS
        self.assertEqual(self.status()["reason"], "trial_expired")
        with self.assertRaisesRegex(ValueError, "trial_expired"):
            self.db.mark_copy_operation_broadcast_attempted("p1")
        self.assertEqual(self.status()["consumed_slots"], 0)
        self.assertEqual(self.db.connection.execute(
            "SELECT status FROM copy_operation_claims").fetchone()[0], "held")

    def test_one_hundredth_allowed_next_rejected(self):
        for n in range(1, 102):
            self.reserve(n)
        for n in range(1, 101):
            self.assertTrue(self.db.mark_copy_operation_broadcast_attempted("p" + str(n)))
        self.assertEqual(self.status()["reason"], "trial_limit_reached")
        with self.assertRaisesRegex(ValueError, "trial_limit_reached"):
            self.db.mark_copy_operation_broadcast_attempted("p101")
        self.assertEqual(self.status()["consumed_slots"], 100)

    def test_failed_slot_insert_rolls_back_send_fence_and_count(self):
        self.reserve()
        self.db.connection.execute("""CREATE TRIGGER fail_slot BEFORE INSERT
            ON early_trial_operations BEGIN SELECT RAISE(ABORT,'injected'); END""")
        with self.assertRaises(Exception):
            self.db.mark_copy_operation_broadcast_attempted("p1")
        self.assertEqual(self.status()["consumed_slots"], 0)
        self.assertEqual(self.db.connection.execute(
            "SELECT status FROM copy_operation_claims").fetchone()[0], "held")

    def test_duplicate_send_never_counts_twice(self):
        self.reserve()
        self.db.mark_copy_operation_broadcast_attempted("p1")
        with self.assertRaisesRegex(ValueError, "already attempted"):
            self.db.mark_copy_operation_broadcast_attempted("p1")
        self.assertEqual(self.status()["consumed_slots"], 1)

    def test_stop_after_slot_allocation_blocks_final_send_check(self):
        self.reserve()
        with self.assertRaisesRegex(ValueError, "fence missing"):
            self.db.check_early_trial_send_fence("p1")
        self.db.mark_copy_operation_broadcast_attempted("p1")
        self.assertTrue(self.db.check_early_trial_send_fence("p1"))
        self.db.stop_early_trial("trial")
        with self.assertRaisesRegex(ValueError, "no longer active"):
            self.db.check_early_trial_send_fence("p1")
        self.assertEqual(self.status()["consumed_slots"], 1)

    def test_queued_last_two_compete_for_one_slot(self):
        self.reserve(1)
        self.reserve(2)
        # Synthetic near-limit setup, not a fabricated broadcast report.
        self.db.connection.execute("UPDATE early_trials SET consumed_slots=99")
        self.db.connection.commit()
        barrier = threading.Barrier(2)
        def send(n):
            db = Store(self.path)
            try:
                barrier.wait(timeout=5)
                try:
                    return db.mark_copy_operation_broadcast_attempted("p" + str(n))
                except ValueError:
                    return False
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sum(pool.map(send, [1, 2])), 1)
        self.assertEqual(self.status()["consumed_slots"], 100)
        winner = self.db.connection.execute("SELECT proposal_id FROM early_trial_operations").fetchone()[0]
        self.assertTrue(self.db.check_early_trial_send_fence(winner))

    def test_stop_does_not_cancel_pending_or_reset_budget(self):
        self.reserve()
        self.db.mark_copy_operation_broadcast_attempted("p1")
        self.db.stop_early_trial("trial")
        self.assertEqual(self.status()["reason"], "trial_stopped")
        self.assertEqual(self.db.paper_proposal("p1")["status"], "reserved")
        self.assertEqual(self.db.paper_budget(A, "USDG")["reserved_raw"], "100")
        self.assertEqual(self.db.start_early_trial("trial", B, [1, 2])["reason"], "trial_stopped")

    def test_expired_unprepared_early_can_yield_to_strict(self):
        p = self.reserve(signed=False)
        self.now = 100 + TRIAL_SECONDS
        self.db.cancel_paper_proposal("p1", "trial_expired")
        p["proposal_id"] = p["source_event_id"] = "strict"
        p["trigger_mode"] = "evidenced"
        del p["attribution"]["early_trial_id"]
        self.assertTrue(self.db.reserve_paper_proposal(p)[0])
        self.assertEqual(self.status()["consumed_slots"], 0)

    def test_scope_mismatch_rejects_without_budget_or_claim(self):
        with self.assertRaisesRegex(ValueError, "scope mismatch"):
            self.reserve(relationship_id="3")
        self.assertEqual(self.db.paper_budget(A, "USDG")["reserved_raw"], "0")
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM copy_operation_claims").fetchone()[0], 0)

    def test_approval_is_not_a_copy_trade(self):
        p = proposal()
        p["attribution"].update(early_trial_id="trial", source_behavior="APPROVAL")
        with self.assertRaisesRegex(ValueError, "only counts copy BUY or SELL"):
            self.db.reserve_paper_proposal(p)
        self.assertEqual(self.status()["consumed_slots"], 0)

    def test_trial_without_operation_ownership_fails_closed(self):
        p = proposal()
        p["attribution"].update(early_trial_id="trial", source_behavior="BUY")
        del p["attribution"]["copy_operation_order_id"]
        with self.assertRaisesRegex(ValueError, "requires operation ownership"):
            self.db.reserve_paper_proposal(p)

    def test_backwards_or_invalid_clock_is_not_eligible(self):
        self.now = 99
        self.assertEqual(self.status()["reason"], "trial_clock_before_start")
        for value in (float("nan"), float("inf"), -1, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.db.early_trial_status("trial", value)

    def test_id_scope_and_duplicate_relationships_rejected(self):
        with self.assertRaisesRegex(ValueError, "scope mismatch"):
            self.db.start_early_trial("trial", B, [1])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.db.start_early_trial("new", A, [1, "1"])

    def test_expired_trial_rejects_new_reservations(self):
        self.now = 100 + TRIAL_SECONDS
        with self.assertRaisesRegex(ValueError, "trial_expired"):
            self.reserve(signed=False)
        self.assertEqual(self.db.paper_budget(A, "USDG")["reserved_raw"], "0")
