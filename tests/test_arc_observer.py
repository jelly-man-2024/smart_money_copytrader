from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eth_abi import encode
from eth_utils import keccak

from smart_money import registry as R
from smart_money.arc_observer import (
    ARC_CURSOR, ARC_SWAP_TOPIC, ArcCanonicalMismatch, ArcObserver,
    ArcSwapSubscriber, arc_backfill_once, validate_arc_swap_log,
)
from smart_money.decode import POOL_KEY, Decoder
from smart_money.models import Transaction
from smart_money.receipts import TRANSFER
from smart_money.store import Store


WALLET = "0x" + "11" * 20
TOKEN = "0x" + "44" * 20
TX_HASH = "0x" + "aa" * 32
BLOCK_HASH = "0x" + "bb" * 32
POOL_KEY_VALUE = (R.ARC.usdc_erc20, TOKEN, 3000, 60, R.NATIVE)
POOL_ID = "0x" + keccak(encode([POOL_KEY], [POOL_KEY_VALUE])).hex()


def address_topic(value: str) -> str:
    return "0x" + value[2:].rjust(64, "0")


def arc_swap_calldata() -> bytes:
    swap = encode(
        [f"({POOL_KEY},bool,uint128,uint128,uint256,bytes)"],
        [(POOL_KEY_VALUE, True, 100, 90, 0, b"")],
    )
    settle = encode(["address", "uint256", "bool"], [R.ARC.usdc_erc20, 100, True])
    take = encode(["address", "address", "uint256"], [TOKEN, WALLET, 0])
    actions = encode(["bytes", "bytes[]"], [b"\x06\x0b\x0e", [swap, settle, take]])
    return bytes.fromhex("3593564c") + encode(
        ["bytes", "bytes[]", "uint256"], [b"\x10", [actions], 2_000_000_000])


def hint() -> dict:
    return {
        "address": R.ARC.v4_manager,
        "transactionHash": TX_HASH,
        "blockHash": BLOCK_HASH,
        "blockNumber": "0x10",
        "logIndex": "0x0",
        "removed": False,
        "topics": [ARC_SWAP_TOPIC, POOL_ID, address_topic(R.ARC.universal_router)],
        "data": "0x" + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [100, -95, 1, 1, 0, 0]).hex(),
    }


def transfer(token: str, sender: str, recipient: str, amount: int, index: int) -> dict:
    return {
        "address": token, "transactionHash": TX_HASH, "blockHash": BLOCK_HASH,
        "blockNumber": "0x10", "logIndex": hex(index), "removed": False,
        "topics": [TRANSFER, address_topic(sender), address_topic(recipient)],
        "data": "0x" + encode(["uint256"], [amount]).hex(),
    }


class FakeRpc:
    def __init__(self):
        self.transaction = {
            "hash": TX_HASH, "from": WALLET, "to": R.ARC.universal_router,
            "input": "0x" + arc_swap_calldata().hex(), "value": "0x0",
            "chainId": hex(R.ARC.chain_id), "nonce": "0x1", "type": "0x2",
        }
        self.transactions = [self.transaction]
        self.block_calls = 0
        swap = hint()
        self.transaction_receipt = {
            "transactionHash": TX_HASH, "blockHash": BLOCK_HASH,
            "blockNumber": "0x10", "status": "0x1", "gasUsed": "0x1",
            "effectiveGasPrice": "0x1",
            "logs": [
                swap,
                transfer(R.ARC.usdc_erc20, WALLET, R.ARC.v4_manager, 100, 1),
                transfer(TOKEN, R.ARC.v4_manager, WALLET, 95, 2),
            ],
        }

    async def call(self, method, params=None):
        if method == "eth_getTransactionByHash":
            return self.transaction
        if method == "eth_getBlockByNumber":
            self.block_calls += 1
            return {"number": "0x10", "hash": BLOCK_HASH,
                    "parentHash": "0x" + "cc" * 32,
                    "transactions": self.transactions}
        if method == "eth_getCode":
            self.last_code_params = params
            return "0x01"
        raise AssertionError(f"unexpected RPC method {method}")

    async def receipt(self, tx_hash):
        if tx_hash != TX_HASH:
            raise AssertionError("wrong receipt hash")
        return self.transaction_receipt


class BackfillRpc(FakeRpc):
    def __init__(self):
        super().__init__()
        self.latest = 15
        self.logs = []
        self.headers = {
            15: {"number": "0xf", "hash": "0x" + "cc" * 32,
                 "parentHash": "0x" + "dd" * 32, "transactions": []},
            16: {"number": "0x10", "hash": BLOCK_HASH,
                 "parentHash": "0x" + "cc" * 32,
                 "transactions": [self.transaction]},
        }

    async def call(self, method, params=None):
        if method == "eth_blockNumber":
            return hex(self.latest)
        if method == "eth_getBlockByNumber":
            return self.headers[int(params[0], 16)]
        if method == "eth_getLogs":
            self.last_log_filter = params[0]
            return self.logs
        return await super().call(method, params)


class ArcDecodeTests(unittest.TestCase):
    def test_arc_v4_uses_arc_quotes_and_chain_scoped_event_id(self):
        tx = Transaction(
            TX_HASH, WALLET, R.ARC.universal_router, arc_swap_calldata(),
            chain_id=R.ARC.chain_id, fresh=True)
        signal = Decoder({WALLET: {}}, chain_id=R.ARC.chain_id).decode(tx)[0]
        self.assertEqual((signal.chain_id, signal.behavior, signal.protocol),
                         (R.ARC.chain_id, "BUY", "v4"))
        self.assertTrue(signal.event_id.startswith(f"{R.ARC.chain_id}:"))

    def test_arc_decoder_rejects_robinhood_transaction(self):
        tx = Transaction(TX_HASH, WALLET, R.ARC.universal_router,
                         arc_swap_calldata(), chain_id=R.ROBINHOOD.chain_id)
        self.assertEqual(
            Decoder({WALLET: {}}, chain_id=R.ARC.chain_id).decode(tx), [])


class ArcObserverTests(unittest.IsolatedAsyncioTestCase):
    async def test_receipt_confirmed_arc_swap_is_persisted_read_only(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            observed = []
            try:
                signals = await ArcObserver(
                    FakeRpc(), store, {WALLET: {}}, observed.append).observe(hint())
                self.assertEqual(len(signals), 1)
                self.assertEqual(signals[0].stage, "swap_evidenced")
                self.assertEqual(signals[0].chain_id, R.ARC.chain_id)
                self.assertEqual(len(observed), 1)
                self.assertEqual(store.candidate_counts()["complete"], 1)
            finally:
                store.close()

    async def test_unwatched_sender_is_not_a_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            try:
                signals = await ArcObserver(FakeRpc(), store, {}).observe(hint())
                self.assertEqual(signals, [])
                self.assertEqual(store.candidate_counts()["pending"], 0)
            finally:
                store.close()

    async def test_live_logs_share_one_full_block_rpc_lookup(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            rpc = FakeRpc()
            other_hash = "0x" + "dd" * 32
            rpc.transactions.append({
                **rpc.transaction, "hash": other_hash,
                "from": "0x" + "55" * 20,
            })
            other_hint = {**hint(), "transactionHash": other_hash, "logIndex": "0x3"}
            observer = ArcObserver(rpc, store, {WALLET: {}})
            try:
                await observer.observe(hint())
                self.assertEqual(await observer.observe(other_hint), [])
                self.assertEqual(rpc.block_calls, 1)
            finally:
                store.close()

    def test_removed_log_fails_closed(self):
        value = hint()
        value["removed"] = True
        with self.assertRaisesRegex(ValueError, "canonical rescan"):
            validate_arc_swap_log(value)


class ArcBackfillTests(unittest.IsolatedAsyncioTestCase):
    async def test_initializes_at_head_then_backfills_and_confirms_block(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            rpc = BackfillRpc()
            observer = ArcObserver(rpc, store, {WALLET: {}})
            try:
                initialized = await arc_backfill_once(rpc, store, observer)
                self.assertTrue(initialized["initialized"])
                self.assertEqual(store.chain_cursor(ARC_CURSOR),
                                 (15, "0x" + "cc" * 32))

                rpc.latest = 16
                rpc.logs = [hint()]
                progress = await arc_backfill_once(rpc, store, observer)
                self.assertEqual((progress["from_block"], progress["to_block"],
                                  progress["logs"], progress["rejected"]),
                                 (16, 16, 1, 0))
                self.assertEqual(store.chain_cursor(ARC_CURSOR), (16, BLOCK_HASH))
                self.assertEqual(rpc.last_log_filter, {
                    "fromBlock": "0x10", "toBlock": "0x10",
                    "address": R.ARC.v4_manager, "topics": [ARC_SWAP_TOPIC],
                })
                payload = json.loads(store.connection.execute(
                    "SELECT payload FROM signals").fetchone()[0])
                self.assertEqual(payload["canonical_status"], "safe_head_confirmed")
                self.assertEqual(
                    payload["evidence"]["canonicality"],
                    "safe_head_hash_rechecked_not_l1_finality")
            finally:
                store.close()

    async def test_cursor_hash_change_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            rpc = BackfillRpc()
            observer = ArcObserver(rpc, store, {WALLET: {}})
            try:
                await arc_backfill_once(rpc, store, observer)
                rpc.headers[15]["hash"] = "0x" + "ee" * 32
                with self.assertRaisesRegex(ArcCanonicalMismatch, "manual review"):
                    await arc_backfill_once(rpc, store, observer)
                self.assertEqual(store.chain_cursor(ARC_CURSOR),
                                 (15, "0x" + "cc" * 32))
            finally:
                store.close()


class Socket:
    def __init__(self, messages):
        self.messages = iter(messages)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def send(self, raw):
        self.request = json.loads(raw)

    async def recv(self):
        return next(self.messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.messages)
        except StopIteration:
            raise StopAsyncIteration


class ArcSubscriberTests(unittest.IsolatedAsyncioTestCase):
    async def test_subscribes_only_to_manager_swap_and_deduplicates_transaction(self):
        notification = json.dumps({
            "jsonrpc": "2.0", "method": "eth_subscription",
            "params": {"subscription": "0xsub", "result": hint()},
        })
        socket = Socket([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0xsub"}),
            notification, notification,
        ])
        with patch("smart_money.arc_observer.websockets.connect", return_value=socket):
            logs = [item async for item in ArcSwapSubscriber("wss://arc.example").logs()]
        self.assertEqual(len(logs), 1)
        self.assertEqual(socket.request["method"], "eth_subscribe")
        self.assertEqual(socket.request["params"][1], {
            "address": R.ARC.v4_manager, "topics": [ARC_SWAP_TOPIC],
        })


if __name__ == "__main__":
    unittest.main()
