"""Local accounting tests, synthetic fills only."""
import json
import unittest
from dataclasses import replace
from test_copy_operation import A, TOKEN, ORDER, TX, proposal
from smart_money import registry as R
from smart_money.models import Signal
from smart_money.store import Store


class SourcePositionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.start_paper_budget_cycle("test", "synthetic")
        self.store.configure_paper_budget(A, "USDG", "1000")
        p = proposal()
        p["attribution"].update(source_stage="intent", source_amount_out_raw="999999",
            source_position_initial_raw="999999", source_position_remaining_raw="999999")
        self.assertTrue(self.store.reserve_paper_proposal(p)[0])
        self.assertTrue(self.store.fill_paper_buy("early", dict(
            order_id="o", fill_id="f", lot_id="lot", amount_out_raw="1000",
            fee_asset=R.USDG, fee_amount_raw="0", gas_cost_wei="0",
            quote_observed_at="2026-09-14T00:00:00Z", filled_at="2026-09-14T00:00:01Z",
            chain_id=R.CHAIN_ID)))
        self.signal = Signal(TX, A, "third_party", "BUY", "strict", None, "",
            stage="relay_buy_evidenced", execution_status="success", execution_success=True,
            token_in=R.USDG, token_out=TOKEN,
            evidence={"relay_order_id": ORDER, "actual_output_credit_raw": "5000"})

    def attr(self):
        return json.loads(self.store.connection.execute(
            "SELECT attribution_payload FROM paper_positions WHERE lot_id='lot'").fetchone()[0])

    def test_pending_estimate_cannot_be_used_for_proportional_sell(self):
        self.assertEqual(self.attr()["source_position_status"], "pending")
        self.assertNotIn("source_position_remaining_raw", self.attr())
        self.assertEqual(self.store.paper_proportional_sell_amount(A, TOKEN, "2500", 1000000),
                         (None, "source_position_basis_unconfirmed"))

    def test_strict_confirmation_replaces_basis_once_without_changing_own_assets(self):
        before = self.store.paper_budget(A, "USDG")
        self.assertEqual(self.store.reconcile_early_source_positions(A, self.signal), 1)
        self.assertEqual(self.store.reconcile_early_source_positions(A, self.signal), 0)
        self.assertEqual(self.attr()["source_position_remaining_raw"], "5000")
        self.assertEqual(self.store.paper_proportional_sell_amount(A, TOKEN, "2500", 1000000)[0], "500")
        self.assertEqual(self.store.paper_budget(A, "USDG"), before)
        self.assertEqual(self.store.connection.execute(
            "SELECT token_remaining_raw FROM paper_positions").fetchone()[0], "1000")

    def test_unrelated_unknown_or_orphaned_evidence_does_not_confirm(self):
        for s in (replace(self.signal, chain_id=1), replace(self.signal, wallet=TOKEN),
                  replace(self.signal, tx_hash=ORDER), replace(self.signal, canonical_status="orphaned"),
                  replace(self.signal, execution_success=None), replace(self.signal, stage="intent"),
                  replace(self.signal, evidence={"relay_order_id": TX, "actual_output_credit_raw": "5000"})):
            self.assertEqual(self.store.reconcile_early_source_positions(A, s), 0)
        self.assertEqual(self.attr()["source_position_status"], "pending")

    def test_invalid_actual_amount_remains_pending(self):
        for amount in (None, "0", "-1", "1.1", "９", str(2 ** 256)):
            s = replace(self.signal, evidence={"relay_order_id": ORDER, "actual_output_credit_raw": amount})
            self.assertEqual(self.store.reconcile_early_source_positions(A, s), 0)

    def test_failed_source_does_not_release_investment_or_enable_proportional_sell(self):
        s = replace(self.signal, stage="failed", execution_status="reverted", execution_success=False)
        self.assertEqual(self.store.reconcile_early_source_positions(A, s), 1)
        self.assertEqual(self.attr()["source_position_status"], "source_failed")
        self.assertEqual(self.store.paper_budget(A, "USDG")["invested_raw"], "100")
        self.assertEqual(self.store.paper_proportional_sell_amount(A, TOKEN, "1", 1000000)[0], None)

    def test_mismatched_asset_requires_review(self):
        self.store.reconcile_early_source_positions(A, replace(self.signal, token_out=R.USDG))
        self.assertEqual(self.attr()["source_position_status"], "source_mismatch")

    def test_local_partial_exit_cannot_guess_source_remaining(self):
        self.store.connection.execute("UPDATE paper_positions SET token_remaining_raw='500'")
        self.store.connection.commit()
        self.store.reconcile_early_source_positions(A, self.signal)
        self.assertEqual(self.attr()["source_position_status"], "needs_review_after_local_exit")

    def test_put_strict_signal_reconciles_existing_fill_atomically(self):
        self.store.put(self.signal)
        self.assertEqual(self.attr()["source_position_status"], "confirmed")
        self.assertEqual(self.attr()["source_position_initial_raw"], "5000")

    def test_strict_signal_before_own_fill_is_reconciled(self):
        tx, order = "0x" + "ef" * 32, "0x" + "12" * 32
        signal = replace(self.signal, tx_hash=tx, evidence={"relay_order_id": order,
                                                          "actual_output_credit_raw": "7500"})
        self.store.put(signal)
        p = proposal("second", source_tx_hash=tx)
        p["attribution"].update(source_stage="intent", copy_operation_order_id=order)
        self.assertTrue(self.store.reserve_paper_proposal(p)[0])
        self.store.fill_paper_buy("second", dict(order_id="o2", fill_id="f2", lot_id="lot2",
            amount_out_raw="2000", fee_asset=R.USDG, fee_amount_raw="0", gas_cost_wei="0",
            quote_observed_at="2026-09-14T00:00:00Z", filled_at="2026-09-14T00:00:01Z",
            chain_id=R.CHAIN_ID))
        attr = self.store.paper_position("lot2")["attribution"]
        self.assertEqual(attr["source_position_status"], "confirmed")
        self.assertEqual(attr["source_position_remaining_raw"], "7500")

    def test_orphan_invalidates_basis_without_releasing_budget_or_dedup(self):
        self.store.put(self.signal)
        self.store.put(replace(self.signal, canonical_status="orphaned"))
        self.assertEqual(self.attr()["source_position_status"], "source_orphaned")
        self.assertNotIn("source_position_remaining_raw", self.attr())
        self.assertEqual(self.store.paper_budget(A, "USDG")["invested_raw"], "100")
        self.assertFalse(self.store.reserve_paper_proposal(proposal("duplicate"))[0])

    def test_source_reconciliation_failure_rolls_back_signal_write(self):
        self.store.connection.execute("""CREATE TRIGGER reject_basis BEFORE UPDATE ON paper_positions
            BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END""")
        with self.assertRaises(Exception):
            self.store.put(self.signal)
        self.assertIsNone(self.store.signal(self.signal.event_id))
        self.assertEqual(self.attr()["source_position_status"], "pending")
