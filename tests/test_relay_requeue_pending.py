"""Operator re-queue of strict-channel candidates behind pending early lots.

Local SQLite only; no network, keys or broadcasts.
"""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from smart_money import cli, registry as R
from smart_money.models import Signal, Transaction
from smart_money.store import Store

SMART, FOLLOWER, TOKEN, SENDER = ("0x" + byte * 20 for byte in ("11", "22", "33", "44"))
ORDER, TX, OTHER_TX = ("0x" + byte * 32 for byte in ("ab", "cd", "ef"))
BLOCKHASH = "0x" + "bb" * 32


def transaction(tx_hash):
    return Transaction(tx_hash, SENDER, TOKEN, b"\x00", timestamp=int(time.time()),
                       received_at=time.time(), fresh=True, observation_source="feed")


def pending_early_lot(db):
    """The same handoff shape the early lane writes before strict evidence exists."""
    db.start_paper_budget_cycle("test", "isolated")
    db.configure_paper_budget(SMART, "USDG", "1000")
    db.start_early_trial("test", FOLLOWER, [1])
    ok, reason = db.reserve_paper_proposal(dict(
        proposal_id="p", source_event_id="early:p", source_tx_hash=TX, wallet=SMART,
        trigger_mode="feed_intent", strategy_version="test", input_asset=R.USDG,
        output_asset=TOKEN, budget_bucket="USDG", amount_in_raw="100",
        attribution=dict(smart_wallet=SMART, follower_wallet=FOLLOWER, relationship_id="1",
                         copy_operation_order_id=ORDER, early_trial_id="test",
                         source_behavior="BUY", source_stage="intent",
                         source_position_status="pending")))
    assert ok, reason
    assert db.fill_paper_buy("p", dict(
        order_id="o", fill_id="f", lot_id="lot", amount_out_raw="1000", fee_asset=R.USDG,
        fee_amount_raw="0", gas_cost_wei="0", quote_observed_at="2026-09-17T00:00:00Z",
        filled_at="2026-09-17T00:00:01Z", chain_id=R.CHAIN_ID))
    assert db.paper_position("lot")["attribution"]["source_position_status"] == "pending"


class RequeuePendingTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.path = str(Path(self.folder.name) / "test.sqlite3")
        db = Store(self.path)
        try:
            pending_early_lot(db)
            # Both candidates went through the worker once and finished; only TX
            # funds a pending lot. OTHER_TX must never be touched.
            for tx_hash in (TX, OTHER_TX):
                db.put_candidate(transaction(tx_hash))
            claimed = db.claim_candidates(5)
            self.assertEqual({t.hash for t in claimed}, {TX, OTHER_TX})
            db.complete_candidate(TX, 10, BLOCKHASH)
            db.fail_candidate(OTHER_TX, "relay_lookup_failed_retry_exhausted")
        finally:
            db.close()

    def tearDown(self):
        self.folder.cleanup()

    def run_command(self, confirm):
        argv = ["relay-requeue-pending", "--db", self.path] + (["--confirm"] if confirm else [])
        args = cli.parser().parse_args(argv)
        with patch.object(cli, "report") as report, redirect_stdout(io.StringIO()):
            cli.relay_requeue_pending(args)
        return report.call_args.kwargs

    def candidates(self, db):
        return {row[0]: (row[1], row[2], row[3]) for row in db.connection.execute(
            "SELECT tx_hash,status,attempts,last_error FROM candidates")}

    def test_dry_run_reports_without_writing(self):
        result = self.run_command(confirm=False)
        self.assertEqual((result["pending_source_transactions"], result["eligible"],
                          result["requeued"], result["confirmed"]), (1, 1, 0, False))
        db = Store(self.path)
        try:
            self.assertEqual(self.candidates(db)[TX], ("complete", 1, None))
            self.assertEqual(db.claim_candidates(5), [])
        finally:
            db.close()

    def test_confirm_requeues_only_pending_lot_sources_and_recovery_confirms_lot(self):
        result = self.run_command(confirm=True)
        self.assertEqual((result["eligible"], result["requeued"], result["confirmed"]), (1, 1, True))
        db = Store(self.path)
        try:
            rows = self.candidates(db)
            self.assertEqual(rows[TX], ("retry", 0, "operator_requeue_pending_early_lot"))
            self.assertEqual(rows[OTHER_TX], ("failed", 1, "relay_lookup_failed_retry_exhausted"))
            # The dispatcher picks it up with a fresh retry budget.
            self.assertEqual([t.hash for t in db.claim_candidates(5)], [TX])
            # Re-processing then yields the Relay-attributed BUY through the normal
            # signal upsert, which reconciles the pending early lot to confirmed.
            db.put(Signal(TX, SMART, "third_party", "BUY", "incoming", None, "",
                          stage="relay_buy_evidenced", execution_status="success",
                          execution_success=True, token_in=R.USDG, token_out=TOKEN,
                          protocol="relay_solver",
                          evidence={"relay_order_id": ORDER, "actual_output_credit_raw": "5000"}))
            attribution = db.paper_position("lot")["attribution"]
            self.assertEqual(attribution["source_position_status"], "confirmed")
            self.assertEqual(attribution["source_position_remaining_raw"], "5000")
            self.assertEqual(db.pending_early_source_tx_hashes(), [])
            # Nothing left to requeue afterwards, and a second run is a no-op.
        finally:
            db.close()
        again = self.run_command(confirm=True)
        self.assertEqual((again["pending_source_transactions"], again["requeued"]), (0, 0))

    def test_owned_or_unfinished_candidates_are_never_stolen(self):
        db = Store(self.path)
        try:
            db.requeue_candidates([TX], "x")
            self.assertEqual([t.hash for t in db.claim_candidates(5)], [TX])  # now queued
            self.assertEqual(db.requeue_candidates([TX], "y"), 0)
            self.assertEqual(self.candidates(db)[TX][0], "queued")
            self.assertEqual(db.candidate_statuses([TX, "0x" + "00" * 32]), {TX: "queued"})
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
