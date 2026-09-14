"""Offline analysis does not expand the runtime decoder's trading permissions."""
import copy
import json
from pathlib import Path
import unittest

from eth_abi.exceptions import DecodingError

from scripts.inspect_relay_permit2 import inspect


class RelayPermit2InspectionTests(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "data/relay_0a2b8f36_sample_a_2026-09-14.json"
        self.tx = json.loads(path.read_text())["transaction"]

    def test_sample_funding_and_delivery_are_different_parties(self):
        result = inspect(self.tx)
        self.assertEqual(result["permit"]["permitted"][0]["amount_raw"], "347809641")
        delivery = result["calls"][2]["deliveries"][0]
        self.assertEqual(delivery["recipient"], "0x1cfbe3af88266ccca29372661f45261c7d19be09")
        self.assertNotEqual(result["funding_user"], delivery["recipient"])
        self.assertEqual(delivery["amount_mode"], "full_router_balance")
        self.assertEqual(result["classification"], "inspection_only_not_a_confirmed_trade")

    def test_nested_routes_are_not_claimed_to_have_executed(self):
        wrapper = inspect(self.tx)["calls"][1]
        self.assertIn("not_verified_semantics", wrapper["abi_status"])
        self.assertEqual(len(wrapper["routes"]), 3)
        for route in wrapper["routes"]:
            self.assertEqual(route["execution_status"], "unknown_from_calldata")
        self.assertEqual(wrapper["extra_utf8"], "0x1789361354fe8863d7f7ede2c71438731189a4715eda57665660fa26ed3a7584")

    def test_metadata_is_not_order_id(self):
        result = inspect(self.tx)
        self.assertEqual(result["trailing_bytes_hex"], "0x9b99837a83e896fe12c5a14916c0e7e0b51152f8d65abae8a980fd280e6bcf5c")
        self.assertNotEqual(result["metadata_hex"], result["trailing_bytes_hex"])

    def test_reject_wrong_scope_selector_and_truncation(self):
        for changes in ({"chain_id": 1}, {"to": "0x" + "11" * 20},
                        {"input": "0xffffffff" + self.tx["input"][10:]},
                        {"input": self.tx["input"][:500]}):
            tx = copy.deepcopy(self.tx)
            tx.update(changes)
            with self.subTest(changes=list(changes)), self.assertRaises((ValueError, DecodingError)):
                inspect(tx)
