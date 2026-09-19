"""A decision ledger row tells the outcome, not the first guess along the way.

An Arc sell was refused as asset_not_allowed while its Relay order was still
open, accepted two seconds later once the retry closed it, and filled. The
decision id is derived from the signal, so both passes wrote the same row —
and INSERT OR IGNORE kept the refusal. The ledger then said the sell had been
blocked, a reviewer read it that way, and reported that Arc could not sell at
all. Only a refusal may be superseded, and only by an acceptance.
"""
import unittest

from smart_money.mysql_store import MySqlConnectionCompat
from smart_money.store import Store

EVENT = "5042:0x" + "e2" * 32 + ":0x" + "89" * 20 + ":userop/0/1/relay/1"
DECISION = "d" * 64


class DecisionSupersessionTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def record(self, accepted, reason, payload=None):
        return self.store.record_paper_decision(
            DECISION, EVENT, "swap_evidenced", "paper-v1",
            accepted, reason, payload or {"pass": reason or "accepted"})

    def stored(self):
        row = self.store.connection.execute(
            "SELECT accepted,reason,payload FROM paper_decisions WHERE decision_id=?",
            (DECISION,)).fetchone()
        return row[0], row[1], row[2]

    def rows(self):
        return self.store.connection.execute(
            "SELECT COUNT(*) FROM paper_decisions").fetchone()[0]

    def test_a_late_acceptance_supersedes_the_interim_refusal(self):
        self.record(False, "asset_not_allowed")
        self.assertEqual(self.stored()[:2], (0, "asset_not_allowed"))
        self.record(True, None, {"pass": "relay_closed"})
        accepted, reason, payload = self.stored()
        self.assertEqual((accepted, reason), (1, None))
        self.assertIn("relay_closed", payload)
        self.assertEqual(self.rows(), 1)

    def test_an_acceptance_is_never_downgraded_by_a_later_refusal(self):
        self.record(True, None, {"pass": "accepted"})
        self.record(False, "price_impact_exceeded", {"pass": "later"})
        accepted, reason, payload = self.stored()
        self.assertEqual((accepted, reason), (1, None))
        self.assertIn("accepted", payload)
        self.assertNotIn("later", payload)

    def test_one_refusal_does_not_overwrite_another(self):
        # Narrow on purpose: the defect was a refusal outliving an acceptance.
        # Replacing a refusal with a different refusal buys nothing and would
        # let a late, less informative reason bury the original one.
        self.record(False, "asset_not_allowed")
        self.record(False, "price_impact_exceeded")
        self.assertEqual(self.stored()[:2], (0, "asset_not_allowed"))

    def test_repeating_the_same_acceptance_keeps_one_row(self):
        self.record(True, None)
        self.record(True, None)
        self.assertEqual(self.rows(), 1)
        self.assertEqual(self.stored()[:2], (1, None))


class MySqlTranslationTests(unittest.TestCase):
    """The same statement has to survive the SQLite-to-MySQL translation.

    MySQL evaluates ON DUPLICATE KEY UPDATE left to right and a later
    assignment sees columns already updated, so `accepted` — the column every
    guard reads — must be assigned last or the guards read the new value.
    """

    STATEMENT = """INSERT INTO paper_decisions(decision_id,accepted,reason,payload)
        VALUES(?,?,?,?)
        ON CONFLICT(decision_id) DO UPDATE SET
            reason=CASE WHEN accepted=0 AND excluded.accepted=1 THEN excluded.reason ELSE reason END,
            payload=CASE WHEN accepted=0 AND excluded.accepted=1 THEN excluded.payload ELSE payload END,
            accepted=CASE WHEN accepted=0 AND excluded.accepted=1 THEN excluded.accepted ELSE accepted END"""

    def test_upsert_becomes_on_duplicate_key_update_with_values(self):
        translated = MySqlConnectionCompat._sql(self.STATEMENT)
        self.assertIn("ON DUPLICATE KEY UPDATE", translated)
        self.assertNotIn("ON CONFLICT", translated)
        self.assertNotIn("excluded.", translated)
        self.assertIn("VALUES(accepted)", translated)

    def test_accepted_is_assigned_after_every_column_that_guards_on_it(self):
        translated = MySqlConnectionCompat._sql(self.STATEMENT)
        assignments = translated.split("ON DUPLICATE KEY UPDATE", 1)[1]
        order = [assignments.index(f"{name}=CASE")
                 for name in ("reason", "payload", "accepted")]
        self.assertEqual(order, sorted(order))

    def test_the_live_statement_keeps_that_order(self):
        import inspect
        source = inspect.getsource(Store.record_paper_decision)
        body = source.split("ON CONFLICT", 1)[1]
        self.assertLess(body.index("reason=CASE"), body.index("accepted=CASE"))
        self.assertLess(body.index("payload=CASE"), body.index("accepted=CASE"))


if __name__ == "__main__":
    unittest.main()
