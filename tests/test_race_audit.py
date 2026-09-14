from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from smart_money.race_audit import audit_deployments

ROOT = Path(__file__).resolve().parents[1]


class RaceAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        cases = json.loads((ROOT / "data/early_feed_public_samples_2026-09-14.json").read_text())["cases"]
        self.case = deepcopy(next(c for c in cases if c["expected_side"] == "BUY"))
        tx = json.loads((ROOT / "data/relay_0a2b8f36_sample_a_2026-09-14.json").read_text())["transaction"]
        self.case["transaction"].update(data=tx["input"], hash=tx["hash"])
        self.case["wallet"] = "0x1cfbe3af88266ccca29372661f45261c7d19be09"
        self.case["truth"] = {"tx_hash": tx["hash"], "wallet": self.case["wallet"],
                              "evidence": {"block_hash": "0x" + "ab" * 32}}
        self.runtime = json.loads((ROOT / "data/relay_race_runtime_2026-09-14.json").read_text())["runtime_code"]

    async def test_audit_reuses_block_and_never_fills_early_snapshots(self):
        cases = [self.case, deepcopy(self.case)]
        before = deepcopy(cases)
        rpc = AsyncMock(); rpc.call.side_effect = ["0x1237", self.runtime]
        result = await audit_deployments(cases, rpc)
        self.assertEqual(result["counts"], {"matched": 2})
        self.assertEqual(result["queried_blocks"], 1)
        self.assertEqual(cases, before)
        self.assertFalse(result["early_snapshots_created"])
        self.assertTrue(rpc.call.call_args_list[1].args[1][1]["requireCanonical"])

    async def test_wrong_chain_and_runtime_mismatch(self):
        rpc = AsyncMock(); rpc.call.return_value = "0x1"
        with self.assertRaises(ValueError):
            await audit_deployments([self.case], rpc)
        rpc.call.side_effect = ["0x1237", "0x00"]
        result = await audit_deployments([self.case], rpc)
        self.assertEqual(result["counts"], {"mismatch": 1})

    async def test_missing_truth_invalid_case_and_errors_are_visible(self):
        self.case["truth"]["wallet"] = "0x" + "ff" * 20
        rpc = AsyncMock(); rpc.call.return_value = "0x1237"
        result = await audit_deployments([self.case, {"transaction": None}], rpc)
        self.assertEqual(result["counts"], {"historical_block_unavailable": 1, "invalid_case": 1})
        self.assertEqual(rpc.call.call_count, 1)
        self.case["truth"]["wallet"] = self.case["wallet"]
        rpc.call.side_effect = ["0x1237", ValueError("https://secret.endpoint/key")]
        result = await audit_deployments([self.case], rpc)
        self.assertEqual(result["counts"], {"lookup_failed": 1})
        self.assertNotIn("secret", json.dumps(result))
