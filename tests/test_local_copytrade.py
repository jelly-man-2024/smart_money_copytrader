from __future__ import annotations

import importlib.util
from pathlib import Path
import unittest

from eth_abi import encode
from eth_utils import keccak

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/validate_local_copytrade.py"
SPEC = importlib.util.spec_from_file_location("validate_local_copytrade", SCRIPT)
LOCAL = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(LOCAL)


class LocalCopyTradeSafetyTests(unittest.TestCase):
    def test_local_rpc_rejects_non_loopback_and_https(self):
        for url in ("https://127.0.0.1:8545", "http://rpc.example:8545",
                    "https://mainnet.example"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LOCAL.LocalRpc(url)

    def test_swap_parser_requires_pool_and_trader_attribution(self):
        trader = "0x" + "11" * 20
        pool = "0x" + "22" * 20
        token = "0x" + "33" * 20
        topic = "0x" + keccak(
            text="Swap(address,address,address,uint256,uint256)").hex()
        padded = lambda value: "0x" + "00" * 12 + value[2:]
        receipt = {"logs": [{
            "address": pool,
            "topics": [topic, padded(trader), padded(LOCAL.ZERO), padded(token)],
            "data": "0x" + encode(["uint256", "uint256"], [10, 10000]).hex(),
        }]}
        self.assertEqual(LOCAL.swap_from_receipt(receipt, pool, trader), {
            "token_in": LOCAL.ZERO, "token_out": token,
            "amount_in_raw": "10", "amount_out_raw": "10000",
        })
        with self.assertRaises(RuntimeError):
            LOCAL.swap_from_receipt(receipt, pool, "0x" + "44" * 20)

    def test_local_signal_keeps_local_chain_identity(self):
        receipt = {"transactionHash": "0x" + "aa" * 32,
                   "blockNumber": "0x2", "blockHash": "0x" + "bb" * 32}
        swap = {"token_in": LOCAL.ZERO, "token_out": "0x" + "33" * 20,
                "amount_in_raw": "10", "amount_out_raw": "10000"}
        signal = LOCAL.evidenced_signal(
            receipt, swap, "0x" + "11" * 20, "0x" + "22" * 20, "BUY")
        self.assertEqual(signal.chain_id, 31337)
        self.assertTrue(signal.event_id.startswith("31337:"))
        self.assertFalse(signal.copy_eligible)


if __name__ == "__main__":
    unittest.main()
