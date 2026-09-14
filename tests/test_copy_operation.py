"""Local SQLite ownership tests. No RPC, key access or live configuration."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest

from smart_money import registry as R
from smart_money.copy_operation import relationship_operation_key
from smart_money.early_intent import Candidate
from smart_money.store import Store

A = "0x" + "11" * 20
B = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
ORDER = "0x" + "ab" * 32
TX = "0x" + "cd" * 32


def proposal(name="early", **changes):
    result = dict(proposal_id=name, source_event_id=name, source_tx_hash=TX,
                  wallet=A, trigger_mode="feed_intent", strategy_version="v1",
                  input_asset=R.USDG, output_asset=TOKEN, budget_bucket="USDG",
                  amount_in_raw="100", attribution={"smart_wallet": A,
                      "follower_wallet": B, "relationship_id": "1",
                      "copy_operation_order_id": ORDER})
    result.update(changes)
    return result


class OperationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "ledger.sqlite3"
        self.store = Store(self.path)
        self.addCleanup(self.store.close)
        self.store.start_paper_budget_cycle("cycle", "test")
        self.store.configure_paper_budget(A, "USDG", "1000")

    def claims(self):
        return self.store.connection.execute(
            "SELECT operation_key,proposal_id,status FROM copy_operation_claims").fetchall()

    def fake_plan(self, name="early", status="signed"):
        # Public ledger scaffolding only; deliberately no signing or raw bytes.
        c = self.store.connection
        c.execute("""INSERT INTO execution_nonce_reservations
            (reservation_id,follower_wallet,relationship_id,proposal_id,chain_id,nonce,status)
            VALUES('nonce',?,'1',?,4663,0,'signed')""", (B, name))
        c.execute("""INSERT INTO execution_plans
            (plan_id,proposal_id,follower_wallet,relationship_id,config_snapshot_hash,
             nonce_reservation_id,status,plan_payload,preflight_payload,signed_tx_hash)
            VALUES('plan',?,?,'1','snapshot','nonce',?,'{}','{}',?)""", (name, B, status, TX))
        c.commit()

    def test_key_agrees_with_offline_candidate_and_is_relationship_scoped(self):
        c = Candidate(TX, A, "BUY", "path", "relay_direct_kyber", R.USDG, TOKEN,
                      "100", "1", ORDER, 100, 100, True, "feed")
        key = relationship_operation_key(A, ORDER, "1", B)
        self.assertEqual(key, c.relationship_key("1", B))
        self.assertNotEqual(key, relationship_operation_key(A, ORDER, "2", B))
        self.assertNotEqual(key, relationship_operation_key(A, ORDER, "1", A))

    def test_stage_config_and_repacked_transaction_cannot_duplicate(self):
        self.assertTrue(self.store.reserve_paper_proposal(proposal())[0])
        other = proposal("strict", trigger_mode="evidenced", strategy_version="v2", source_tx_hash=ORDER)
        other["attribution"]["config_snapshot_hash"] = "changed"
        self.assertEqual(self.store.reserve_paper_proposal(other),
                         (False, "copy_operation_already_claimed"))
        self.assertEqual(self.store.paper_budget(A, "USDG")["reserved_raw"], "100")
        self.assertIsNone(self.store.paper_proposal("strict"))
        self.assertEqual(self.store.reserve_paper_proposal(proposal()), (True, "proposal_already_exists"))

    def test_budget_failure_rolls_back_claim(self):
        self.assertEqual(self.store.reserve_paper_proposal(proposal(amount_in_raw="1001")),
                         (False, "budget_limit_exceeded"))
        self.assertEqual(self.claims(), [])
        self.assertTrue(self.store.reserve_paper_proposal(proposal("strict"))[0])

    def test_legacy_reserved_order_is_not_copied_again_after_upgrade(self):
        from smart_money.models import Signal
        old = proposal("legacy")
        del old["attribution"]["copy_operation_order_id"]
        self.assertTrue(self.store.reserve_paper_proposal(old)[0])
        self.store.put(Signal(TX, A, "third_party", "BUY", "strict", None, "",
            stage="relay_buy_evidenced", execution_status="success", execution_success=True,
            evidence={"relay_order_id": ORDER}))
        self.assertFalse(self.store.reserve_paper_proposal(proposal("early"))[0])
        self.assertTrue(self.store.cancel_paper_proposal("legacy", "unprepared"))
        self.assertTrue(self.store.reserve_paper_proposal(proposal("early"))[0])

    def test_insert_failure_rolls_back_claim_and_budget(self):
        self.store.connection.execute("""CREATE TRIGGER reject_reservation BEFORE INSERT
            ON paper_reservations BEGIN SELECT RAISE(ABORT, 'injected'); END""")
        self.assertFalse(self.store.reserve_paper_proposal(proposal())[0])
        self.assertEqual(self.claims(), [])
        self.assertIsNone(self.store.paper_proposal("early"))
        self.assertEqual(self.store.paper_budget(A, "USDG")["reserved_raw"], "0")

    def test_cancel_before_preparation_releases_for_strict_fallback(self):
        self.store.reserve_paper_proposal(proposal())
        self.assertTrue(self.store.cancel_paper_proposal("early", "expired"))
        self.assertEqual(self.claims()[0][2], "released")
        self.assertTrue(self.store.reserve_paper_proposal(proposal("strict", trigger_mode="evidenced"))[0])
        self.assertEqual(self.claims()[0][1:], ("strict", "held"))
        self.assertEqual(self.store.paper_proposal("early")["status"], "cancelled")

    def test_broadcast_fence_survives_restart_and_cannot_be_released(self):
        self.store.reserve_paper_proposal(proposal())
        self.fake_plan()
        self.assertTrue(self.store.mark_copy_operation_broadcast_attempted("early"))
        reopened = Store(self.path)
        self.addCleanup(reopened.close)
        with self.assertRaisesRegex(ValueError, "already attempted"):
            reopened.mark_copy_operation_broadcast_attempted("early")
        with self.assertRaisesRegex(ValueError, "outcome unresolved"):
            reopened.cancel_paper_proposal("early", "network_timeout")
        self.assertFalse(reopened.reserve_paper_proposal(proposal("strict"))[0])
        self.assertEqual(reopened.paper_budget(A, "USDG")["reserved_raw"], "100")

    def test_signed_plan_without_send_fence_is_still_not_safe_to_release(self):
        self.store.reserve_paper_proposal(proposal())
        self.fake_plan()
        with self.assertRaisesRegex(ValueError, "still active"):
            self.store.cancel_paper_proposal("early", "unknown")
        self.assertEqual(self.claims()[0][2], "held")

    def test_reverted_execution_releases_budget_but_never_operation(self):
        self.store.reserve_paper_proposal(proposal())
        self.fake_plan()
        self.store.mark_copy_operation_broadcast_attempted("early")
        self.store.connection.execute("""INSERT INTO execution_attempts
            (tx_hash,plan_id,nonce,status,public_payload,block_number,block_hash)
            VALUES(?,'plan',0,'reverted','{}',1,?)""", (TX, ORDER))
        self.store.connection.commit()
        self.assertTrue(self.store.cancel_paper_proposal("early", "live_execution_reverted"))
        self.assertEqual(self.store.paper_budget(A, "USDG")["reserved_raw"], "0")
        self.assertEqual(self.claims()[0][2], "broadcast_attempted")
        self.assertFalse(self.store.reserve_paper_proposal(proposal("strict"))[0])

    def test_cancelled_signed_plan_keeps_claim_for_operator_review(self):
        self.store.reserve_paper_proposal(proposal())
        self.fake_plan(status="cancelled")
        self.assertTrue(self.store.cancel_paper_proposal("early", "prebroadcast_rejected"))
        self.assertEqual(self.claims()[0][2], "held")
        self.assertFalse(self.store.reserve_paper_proposal(proposal("strict"))[0])

    def test_send_fence_requires_signed_plan(self):
        self.store.reserve_paper_proposal(proposal())
        with self.assertRaisesRegex(ValueError, "no signed plan"):
            self.store.mark_copy_operation_broadcast_attempted("early")
        self.assertEqual(self.claims()[0][2], "held")

    def test_legacy_proposals_do_not_query_new_table(self):
        p = proposal()
        del p["attribution"]["copy_operation_order_id"]
        statements = []
        self.store.connection.set_trace_callback(statements.append)
        self.assertTrue(self.store.reserve_paper_proposal(p)[0])
        self.assertFalse(self.store.mark_copy_operation_broadcast_attempted("early"))
        self.assertTrue(self.store.cancel_paper_proposal("early", "test"))
        self.assertFalse(any("copy_operation_claims" in q for q in statements))

    def test_two_connections_racing_reserve_only_once(self):
        barrier = threading.Barrier(2)
        def reserve(name):
            db = Store(self.path)
            try:
                barrier.wait(timeout=5)
                return db.reserve_paper_proposal(proposal(name))
            finally:
                db.close()
        with ThreadPoolExecutor(max_workers=2) as workers:
            results = list(workers.map(reserve, ["early", "strict"]))
        self.assertEqual(sum(r[0] for r in results), 1)
        self.assertEqual(len(self.claims()), 1)
        self.assertEqual(self.store.paper_budget(A, "USDG")["reserved_raw"], "100")

    def test_sell_failure_rolls_back_claim_and_buy_sell_share_key(self):
        sell = proposal("sell", input_asset=TOKEN, output_asset=R.USDG)
        self.assertEqual(self.store.reserve_paper_sell(sell), (False, "attributed_position_insufficient"))
        self.assertEqual(self.claims(), [])
        self.store.reserve_paper_proposal(proposal())
        self.assertEqual(self.store.reserve_paper_sell(sell), (False, "copy_operation_already_claimed"))

    def test_sell_success_reserves_lot_and_cancellation_hands_off_once(self):
        buy = proposal("buy")
        del buy["attribution"]["copy_operation_order_id"]
        self.store.reserve_paper_proposal(buy)
        self.store.fill_paper_buy("buy", dict(
            order_id="buy-order", fill_id="buy-fill", lot_id="lot", amount_out_raw="1000",
            fee_asset=R.USDG, fee_amount_raw="0", gas_cost_wei="0",
            quote_observed_at="2026-09-14T00:00:00Z", filled_at="2026-09-14T00:00:01Z"))
        sell = proposal("sell-early", input_asset=TOKEN, output_asset=R.USDG)
        self.assertTrue(self.store.reserve_paper_sell(sell)[0])
        later = {**sell, "proposal_id": "sell-strict", "source_event_id": "later"}
        self.assertFalse(self.store.reserve_paper_sell(later)[0])
        self.assertTrue(self.store.cancel_paper_proposal("sell-early", "expired"))
        self.assertTrue(self.store.reserve_paper_sell(later)[0])
        reserved = self.store.connection.execute("""SELECT token_amount_raw
            FROM paper_position_reservations WHERE status='active'""").fetchall()
        self.assertEqual(reserved, [("100",)])

    def test_invalid_attribution_is_rejected_without_claim(self):
        for field, value in (("copy_operation_order_id", "0x1234"),
                             ("relationship_id", True), ("relationship_id", "01"),
                             ("follower_wallet", R.NATIVE)):
            p = proposal()
            p["attribution"][field] = value
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.store.reserve_paper_proposal(p)
        self.assertEqual(self.claims(), [])
