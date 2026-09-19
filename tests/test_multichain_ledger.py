"""Two chains share one ledger: block numbers collide, their state must not."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from smart_money import registry as R
from smart_money.store import Store

RH_HASH = "0x" + "11" * 32
ARC_HASH = "0x" + "22" * 32
PARENT = "0x" + "33" * 32
BLOCK = 65_000_000  # a height both chains really do reach


class ChainScopedCanonicalStateTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)

    def test_the_same_block_number_is_kept_per_chain(self):
        self.store.record_chain_block(BLOCK, RH_HASH, PARENT)
        self.store.record_chain_block(BLOCK, ARC_HASH, PARENT,
                                      name="arc_wallet_transfers",
                                      chain_id=R.ARC.chain_id)
        self.assertEqual(self.store.chain_block_hash(BLOCK), RH_HASH)
        self.assertEqual(self.store.chain_block_hash(BLOCK, R.ARC.chain_id), ARC_HASH)
        self.assertEqual(self.store.chain_cursor(), (BLOCK, RH_HASH))
        self.assertEqual(
            self.store.chain_cursor("arc_wallet_transfers", R.ARC.chain_id),
            (BLOCK, ARC_HASH))

    def test_a_chain_does_not_see_another_chain_cursor(self):
        self.store.set_chain_cursor(BLOCK, ARC_HASH, "arc_wallet_transfers",
                                    R.ARC.chain_id)
        self.assertIsNone(self.store.chain_cursor("arc_wallet_transfers"))
        self.assertIsNone(self.store.chain_block_hash(BLOCK, R.ARC.chain_id))

    def test_a_rewind_on_one_chain_leaves_the_other_untouched(self):
        for height in (BLOCK, BLOCK + 1):
            self.store.record_chain_block(height, RH_HASH, PARENT)
            self.store.record_chain_block(height, ARC_HASH, PARENT,
                                          name="arc_wallet_transfers",
                                          chain_id=R.ARC.chain_id)
        self.store.rewind_chain(BLOCK, ARC_HASH, name="arc_wallet_transfers",
                                chain_id=R.ARC.chain_id)
        # Arc rolled back past its own tip; Robinhood keeps both blocks.
        self.assertIsNone(self.store.chain_block_hash(BLOCK + 1, R.ARC.chain_id))
        self.assertEqual(self.store.chain_block_hash(BLOCK + 1), RH_HASH)
        self.assertEqual(self.store.chain_cursor(), (BLOCK + 1, RH_HASH))
        self.assertEqual(
            self.store.chain_cursor("arc_wallet_transfers", R.ARC.chain_id),
            (BLOCK, ARC_HASH))

    def test_rewind_protection_is_also_per_chain(self):
        self.store.record_chain_block(BLOCK + 5, RH_HASH, PARENT)
        with self.assertRaisesRegex(ValueError, "rewind requires explicit reorg"):
            self.store.record_chain_block(BLOCK, RH_HASH, PARENT)
        # A lower height on another chain is ordinary progress, not a rewind.
        self.store.record_chain_block(BLOCK, ARC_HASH, PARENT,
                                      name="arc_wallet_transfers",
                                      chain_id=R.ARC.chain_id)
        self.assertEqual(self.store.chain_block_hash(BLOCK, R.ARC.chain_id), ARC_HASH)


class LegacyLedgerUpgradeTests(unittest.TestCase):
    def test_a_single_chain_ledger_is_rebuilt_with_chain_scoped_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "legacy.sqlite3"
            legacy = sqlite3.connect(path)
            legacy.execute("""CREATE TABLE chain_cursors (
                name TEXT PRIMARY KEY, block_number INTEGER NOT NULL,
                block_hash TEXT NOT NULL,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
            legacy.execute("""CREATE TABLE canonical_blocks (
                block_number INTEGER PRIMARY KEY, block_hash TEXT NOT NULL,
                parent_hash TEXT NOT NULL)""")
            legacy.execute("INSERT INTO chain_cursors(name,block_number,block_hash)"
                           " VALUES('canonical_l2',?,?)", (BLOCK, RH_HASH))
            legacy.execute("INSERT INTO canonical_blocks VALUES(?,?,?)",
                           (BLOCK, RH_HASH, PARENT))
            legacy.commit()
            legacy.close()

            store = Store(path)
            self.addCleanup(store.close)
            # The existing rows survive and are attributed to Robinhood Chain.
            self.assertEqual(store.chain_cursor(), (BLOCK, RH_HASH))
            self.assertEqual(store.chain_block_hash(BLOCK), RH_HASH)
            # And the upgraded key now admits a second chain at the same height.
            store.record_chain_block(BLOCK, ARC_HASH, PARENT,
                                     name="arc_wallet_transfers",
                                     chain_id=R.ARC.chain_id)
            self.assertEqual(store.chain_block_hash(BLOCK, R.ARC.chain_id), ARC_HASH)
            self.assertEqual(store.chain_block_hash(BLOCK), RH_HASH)
            leftovers = [row[0] for row in store.connection.execute(
                "SELECT name FROM sqlite_master WHERE name LIKE '%_pre_chain_key'")]
            self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()


class BudgetBucketValidationTests(unittest.TestCase):
    """A chain's own bucket must be configurable, and a bogus one must not be."""

    def store(self):
        from smart_money.store import Store
        store = Store(":memory:")
        self.addCleanup(store.close)
        store.start_paper_budget_cycle("cycle-1", "test")
        return store

    def test_every_declared_bucket_is_accepted(self):
        from smart_money.paper import BUDGET_BUCKETS
        store = self.store()
        scope = "relationship:" + "a" * 64
        for bucket in sorted(BUDGET_BUCKETS):
            with self.subTest(bucket=bucket):
                store.configure_paper_budget(scope, bucket, "5000000")
                self.assertEqual(store.paper_budget(scope, bucket)["limit_raw"], "5000000")

    def test_unknown_bucket_and_bad_limit_are_refused(self):
        store = self.store()
        scope = "relationship:" + "b" * 64
        for bucket, limit in (("USDT", "5000000"), ("USDC", "0"), ("USDC", "-1"), ("USDC", "x")):
            with self.subTest(bucket=bucket, limit=limit):
                with self.assertRaisesRegex(ValueError, "invalid paper budget"):
                    store.configure_paper_budget(scope, bucket, limit)


class LedgerBudgetBucketScopeTests(unittest.TestCase):
    """The ledger's own bucket check must recognise every chain's assets."""

    def test_each_chain_settlement_asset_maps_to_its_bucket(self):
        from smart_money.store import _paper_budget_buckets
        self.assertIn("USDG", _paper_budget_buckets(R.USDG))
        self.assertIn("ETH_WETH", _paper_budget_buckets(R.WETH))
        # Arc's USDC was unknown here, so an Arc proposal was refused with
        # input_asset_budget_bucket_mismatch after every other gate had passed.
        self.assertIn("USDC", _paper_budget_buckets(R.ARC.usdc_erc20))

    def test_an_unrelated_token_belongs_to_no_bucket(self):
        from smart_money.store import _paper_budget_buckets
        self.assertEqual(_paper_budget_buckets("0x" + "11" * 20), frozenset())

    def test_the_shared_native_sentinel_is_accepted_for_either_chain(self):
        from smart_money.store import _paper_budget_buckets
        buckets = _paper_budget_buckets(R.NATIVE)
        self.assertEqual(buckets, frozenset({"ETH_WETH", "USDC"}))


class CandidateChainScopeTests(unittest.TestCase):
    """A worker claims only the chain whose receipts it can actually read."""

    def store(self):
        from smart_money.store import Store
        store = Store(":memory:")
        self.addCleanup(store.close)
        return store

    def transaction(self, chain_id, tag):
        from smart_money.models import Transaction
        return Transaction(hash="0x" + tag * 32, sender="0x" + "bb" * 20,
                           to="0x" + "cc" * 20, data=b"\x01\x02\x03\x04",
                           chain_id=chain_id, observation_source="test")

    def test_a_worker_claims_only_its_own_chain(self):
        store = self.store()
        store.put_candidate(self.transaction(R.CHAIN_ID, "a1"))
        store.put_candidate(self.transaction(R.ARC.chain_id, "a2"))
        claimed = store.claim_candidates(10, chain_id=R.CHAIN_ID)
        self.assertEqual([tx.chain_id for tx in claimed], [R.CHAIN_ID])
        # The other chain's candidate is untouched and still claimable by its own
        # worker; claiming it blindly burnt retries against the wrong node.
        arc = store.claim_candidates(10, chain_id=R.ARC.chain_id)
        self.assertEqual([tx.chain_id for tx in arc], [R.ARC.chain_id])

    def test_without_a_chain_every_candidate_is_claimable(self):
        store = self.store()
        store.put_candidate(self.transaction(R.CHAIN_ID, "b1"))
        store.put_candidate(self.transaction(R.ARC.chain_id, "b2"))
        self.assertEqual(len(store.claim_candidates(10)), 2)

    def test_the_limit_still_bounds_a_filtered_claim(self):
        store = self.store()
        for i in range(6):
            store.put_candidate(self.transaction(R.CHAIN_ID, f"c{i}"))
        self.assertEqual(len(store.claim_candidates(2, chain_id=R.CHAIN_ID)), 2)


class LotChainTests(unittest.TestCase):
    """A lot records the chain its buy ran on, never a column default.

    paper_positions.chain_id carries a Robinhood default and the insert never
    set it, so all eight Arc lots were filed under chain 4663 and nothing in
    the row could recover the truth. Lot selection matches on wallet and token
    alone, so the wrong chain cost nothing yet — and would cost everything the
    day one token address exists on both chains.
    """

    WALLET = "0x" + "a1" * 20
    FOLLOWER = "0x" + "b2" * 20
    TOKEN = "0x" + "c3" * 20

    def proposal(self, name):
        return dict(proposal_id=name, source_event_id=name, source_tx_hash=RH_HASH,
                    wallet=self.WALLET, trigger_mode="feed_intent", strategy_version="v1",
                    input_asset=R.USDG, output_asset=self.TOKEN, budget_bucket="USDG",
                    amount_in_raw="100",
                    attribution={"smart_wallet": self.WALLET,
                                 "follower_wallet": self.FOLLOWER, "relationship_id": "1"})

    def setUp(self):
        A = self.WALLET
        self.store = Store(":memory:")
        self.addCleanup(self.store.close)
        self.store.start_paper_budget_cycle("test", "synthetic")
        self.store.configure_paper_budget(A, "USDG", "1000")

    def fill(self, name, lot, **changes):
        self.assertTrue(self.store.reserve_paper_proposal(self.proposal(name))[0])
        payload = dict(order_id=f"o-{lot}", fill_id=f"f-{lot}", lot_id=lot,
                       amount_out_raw="1000", fee_asset=R.USDG, fee_amount_raw="0",
                       gas_cost_wei="0", quote_observed_at="2026-09-19T00:00:00Z",
                       filled_at="2026-09-19T00:00:01Z", chain_id=R.CHAIN_ID)
        payload.update(changes)
        return self.store.fill_paper_buy(name, payload)

    def stored_chain(self, lot):
        return self.store.connection.execute(
            "SELECT chain_id FROM paper_positions WHERE lot_id=?", (lot,)).fetchone()[0]

    def test_an_arc_buy_is_filed_under_arc_not_the_default(self):
        self.assertTrue(self.fill("arc-buy", "arc-lot", chain_id=R.ARC.chain_id))
        self.assertEqual(self.stored_chain("arc-lot"), R.ARC.chain_id)
        self.assertNotEqual(self.stored_chain("arc-lot"), R.CHAIN_ID)

    def test_a_robinhood_buy_is_still_filed_under_robinhood(self):
        self.assertTrue(self.fill("rh-buy", "rh-lot"))
        self.assertEqual(self.stored_chain("rh-lot"), R.CHAIN_ID)

    def test_a_fill_that_names_no_chain_is_refused_rather_than_defaulted(self):
        self.assertTrue(self.store.reserve_paper_proposal(self.proposal("no-chain"))[0])
        with self.assertRaisesRegex(ValueError, "invalid paper fill fields"):
            self.store.fill_paper_buy("no-chain", dict(
                order_id="o", fill_id="f", lot_id="lot", amount_out_raw="1000",
                fee_asset=R.USDG, fee_amount_raw="0", gas_cost_wei="0",
                quote_observed_at="2026-09-19T00:00:00Z",
                filled_at="2026-09-19T00:00:01Z"))

    def test_a_chain_the_registry_does_not_know_is_refused(self):
        for value in (999999, "4663", None):
            with self.subTest(chain_id=value):
                with self.assertRaisesRegex(ValueError, "invalid paper fill chain"):
                    self.fill(f"bad-{value}", f"bad-lot-{value}", chain_id=value)
