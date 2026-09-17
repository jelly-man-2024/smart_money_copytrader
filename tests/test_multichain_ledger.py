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
