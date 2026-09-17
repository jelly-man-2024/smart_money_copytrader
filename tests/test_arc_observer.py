from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from eth_abi import encode
from eth_utils import keccak

from smart_money import registry as R
from smart_money.receipts import SWAPS
from smart_money.arc_observer import (
    ARC_CURSOR, ArcCandidateRejected, ArcCanonicalMismatch, ArcObserver,
    ArcWalletSubscriber, arc_backfill_once, validate_arc_transfer_log, wallet_topic,
)
from smart_money.decode import CALLS, POOL_KEY, Decoder
from smart_money.models import Transaction
from smart_money.receipts import TRANSFER
from smart_money.store import Store


ARC_V4_SWAP_TOPIC = next(t for t, protocol in SWAPS.items() if protocol == "v4")
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


def swap_log() -> dict:
    return {
        "address": R.ARC.v4_manager,
        "transactionHash": TX_HASH,
        "blockHash": BLOCK_HASH,
        "blockNumber": "0x10",
        "logIndex": "0x0",
        "removed": False,
        "topics": [ARC_V4_SWAP_TOPIC, POOL_ID, address_topic(R.ARC.universal_router)],
        "data": "0x" + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [100, -95, 1, 1, 0, 0]).hex(),
    }


def hint(**overrides) -> dict:
    """What now triggers ingestion: the watched wallet's own token credit."""
    return {**transfer(TOKEN, R.ARC.v4_manager, WALLET, 95, 2), **overrides}


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
        self.trace_calls = 0
        self.prestate = {}
        swap = swap_log()
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
        if method == "debug_traceTransaction":
            self.trace_calls += 1
            return self.prestate
        raise AssertionError(f"unexpected RPC method {method}")

    async def receipt(self, tx_hash):
        # Another transaction in the same block gets its own receipt, which does
        # not carry this transaction's logs.
        if tx_hash != TX_HASH:
            return {**self.transaction_receipt, "transactionHash": tx_hash, "logs": []}
        return self.transaction_receipt


class BackfillRpc(FakeRpc):
    def __init__(self):
        super().__init__()
        self.latest = 15
        self.logs = []
        self.log_filters = []
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
            self.log_filters.append(params[0])
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

    def test_arc_contract_creation_does_not_false_match_none_fields(self):
        # A watchlisted wallet deploying a contract (to=None) on Arc must not
        # false-match None registry fields (Relay/Depository/EntryPoint/WETH),
        # which are None on Arc, via None == None. It decodes to no signal.
        tx = Transaction(TX_HASH, WALLET, None, bytes.fromhex("60806040" + "00" * 8),
                         chain_id=R.ARC.chain_id)
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

    async def test_transfer_touching_no_watched_wallet_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            try:
                with self.assertRaisesRegex(ValueError, "watched wallet"):
                    await ArcObserver(FakeRpc(), store, {}).observe(hint())
                self.assertEqual(store.candidate_counts()["pending"], 0)
            finally:
                store.close()

    async def test_delivery_sent_by_someone_else_is_still_a_candidate(self):
        # A cross-chain Relay fill, an airdrop or a router refund is sent by a
        # third party. The old sender gate dropped all of them; the wallet is
        # matched on the Transfer instead, and attribution stays with enrich.
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            rpc = FakeRpc()
            rpc.transaction = {**rpc.transaction, "from": "0x" + "77" * 20}
            rpc.transactions = [rpc.transaction]
            try:
                signals = await ArcObserver(rpc, store, {WALLET: {}}).observe(hint())
                self.assertTrue(signals)
                self.assertTrue(all(s.wallet == WALLET for s in signals))
                self.assertEqual(store.candidate_counts()["complete"], 1)
            finally:
                store.close()

    def test_erc721_transfer_shape_is_not_a_fungible_candidate(self):
        value = hint()
        value["topics"] = value["topics"] + ["0x" + "00" * 31 + "07"]
        with self.assertRaisesRegex(ValueError, "not an ERC-20 Transfer"):
            validate_arc_transfer_log(value, {WALLET: {}})

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
                # The second hint names a different transaction in the same
                # block, whose receipt does not carry this log: it is refused,
                # but the block itself was only fetched once.
                with self.assertRaises(ArcCandidateRejected):
                    await observer.observe(other_hint)
                self.assertEqual(rpc.block_calls, 1)
            finally:
                store.close()

    async def test_simple7702_self_batch_requires_transaction_prestate(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            rpc = FakeRpc()
            rpc.transaction = {
                **rpc.transaction,
                "to": WALLET,
                "input": "0x34fcd5be" + encode(
                    [CALLS], [[(R.ARC.universal_router, 0, arc_swap_calldata())]]
                ).hex(),
            }
            rpc.transactions = [rpc.transaction]
            rpc.prestate = {
                WALLET: {"code": "0xef0100" + R.ARC.simple_account[2:]},
            }
            try:
                signals = await ArcObserver(
                    rpc, store, {WALLET: {}}).observe(hint())
                self.assertEqual(len(signals), 1)
                self.assertEqual(
                    (signals[0].mode, signals[0].behavior),
                    ("self_account", "BUY"),
                )
                self.assertTrue(signals[0].path.startswith("call/0/"))
                self.assertEqual(
                    signals[0].evidence["account_state_source"],
                    "transaction_prestate_trace",
                )
                self.assertEqual(rpc.trace_calls, 1)
            finally:
                store.close()

    async def test_unknown_self_account_implementation_remains_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(Path(directory) / "arc.sqlite3")
            rpc = FakeRpc()
            rpc.transaction = {
                **rpc.transaction,
                "to": WALLET,
                "input": "0x34fcd5be" + encode(
                    [CALLS], [[(R.ARC.universal_router, 0, arc_swap_calldata())]]
                ).hex(),
            }
            rpc.transactions = [rpc.transaction]
            rpc.prestate = {
                WALLET: {"code": "0xef0100" + "99" * 20},
            }
            try:
                signals = await ArcObserver(
                    rpc, store, {WALLET: {}}).observe(hint())
                self.assertEqual(len(signals), 1)
                self.assertEqual(signals[0].behavior, "UNKNOWN")
                self.assertNotEqual(signals[0].stage, "swap_evidenced")
                self.assertEqual(
                    signals[0].evidence["account_state_source"],
                    "transaction_prestate_unsupported_or_absent",
                )
                self.assertEqual(rpc.trace_calls, 1)
            finally:
                store.close()

    def test_removed_log_fails_closed(self):
        value = hint()
        value["removed"] = True
        with self.assertRaisesRegex(ValueError, "canonical rescan"):
            validate_arc_transfer_log(value, {WALLET: {}})


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
                # Both directions are asked for, scoped to the watchlist and
                # to no particular token or venue.
                self.assertEqual(rpc.log_filters[-2:], [
                    {"fromBlock": "0x10", "toBlock": "0x10",
                     "topics": [TRANSFER, [wallet_topic(WALLET)], None]},
                    {"fromBlock": "0x10", "toBlock": "0x10",
                     "topics": [TRANSFER, None, [wallet_topic(WALLET)]]},
                ])
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
        self.requests = getattr(self, "requests", []) + [self.request]

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
    async def test_subscribes_to_both_wallet_directions_and_deduplicates(self):
        # One swap puts the wallet on both sides, so it arrives on both
        # subscriptions and must be ingested once.
        outgoing = json.dumps({
            "jsonrpc": "2.0", "method": "eth_subscription",
            "params": {"subscription": "0xsub1",
                       "result": transfer(R.ARC.usdc_erc20, WALLET, TOKEN, 100, 1)},
        })
        incoming = json.dumps({
            "jsonrpc": "2.0", "method": "eth_subscription",
            "params": {"subscription": "0xsub2", "result": hint()},
        })
        socket = Socket([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0xsub1"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": "0xsub2"}),
            outgoing, incoming,
        ])
        with patch("smart_money.arc_observer.websockets.connect", return_value=socket):
            logs = [item async for item in
                    ArcWalletSubscriber("wss://arc.example", {WALLET: {}}).logs()]
        self.assertEqual(len(logs), 1)
        self.assertEqual([item["params"][1] for item in socket.requests], [
            {"topics": [TRANSFER, [wallet_topic(WALLET)], None]},
            {"topics": [TRANSFER, None, [wallet_topic(WALLET)]]},
        ])

    async def test_a_log_for_an_unknown_subscription_is_refused(self):
        socket = Socket([
            json.dumps({"jsonrpc": "2.0", "id": 1, "result": "0xsub1"}),
            json.dumps({"jsonrpc": "2.0", "id": 2, "result": "0xsub2"}),
            json.dumps({"jsonrpc": "2.0", "method": "eth_subscription",
                        "params": {"subscription": "0xother", "result": hint()}}),
        ])
        with patch("smart_money.arc_observer.websockets.connect", return_value=socket):
            with self.assertRaisesRegex(ValueError, "subscription identity mismatch"):
                [item async for item in
                 ArcWalletSubscriber("wss://arc.example", {WALLET: {}}).logs()]


if __name__ == "__main__":
    unittest.main()
