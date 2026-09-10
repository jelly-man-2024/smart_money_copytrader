from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from eth_abi import encode
from eth_account import Account
from eth_utils import to_checksum_address

from smart_money import registry as R
from smart_money.decode import CALLS, PACKED_OPS, POOL_KEY, Decoder, selector, v3_path
from smart_money.feed import DecodeError, FeedHealth, decode_raw, envelopes, signed_transactions
from smart_money.models import Signal, Transaction
from smart_money.receipts import BEFORE, SWAPS, TRANSFER, USEROP, TRADE_BEHAVIORS, enrich
from smart_money.rpc import ReadOnlyRpc
from smart_money.store import Store

ROOT = Path(__file__).resolve().parents[1]
A = "0x" + "11" * 20
B = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
TXHASH = "0x" + "aa" * 32


def tx(data, to=A, sender=A):
    return Transaction(TXHASH, sender, to, data, fresh=True)


def claim():
    return bytes.fromhex("815a4392") + encode(["address", "bool"], [A, False])


def v2_swap():
    return selector("swapExactTokensForTokens(uint256,uint256,address[],address,uint256)") + encode(
        ["uint256", "uint256", "address[]", "address", "uint256"], [100, 90, [R.USDG, TOKEN], A, 2000000000])


def v4_swap():
    key = (R.USDG, TOKEN, 3000, 60, R.NATIVE)
    param = encode([f"({POOL_KEY},bool,uint128,uint128,uint256,bytes)"], [(key, True, 100, 90, 0, b"")])
    action = encode(["bytes", "bytes[]"], [b"\x06", [param]])
    return bytes.fromhex("3593564c") + encode(["bytes", "bytes[]", "uint256"], [b"\x10", [action], 2000000000])


def batch(calls):
    return bytes.fromhex("34fcd5be") + encode([CALLS], [calls])


def op(wallet, data, nonce=1):
    return (wallet, nonce, b"", data, bytes(32), 0, bytes(32), b"", b"signature")


def bundled(ops):
    return tx(bytes.fromhex("765e827f") + encode([PACKED_OPS, "address"], [ops, B]), R.ENTRYPOINT, B)


def log(contract, topics, data="0x", index=0):
    return {"address": contract, "topics": topics, "data": data, "logIndex": hex(index)}


def addr_topic(a):
    return "0x" + a[2:].rjust(64, "0")


def transfer(token, sender, recipient, amount):
    return log(token, [TRANSFER, addr_topic(sender), addr_topic(recipient)], "0x" + encode(["uint256"], [amount]).hex())


def userop_event(wallet, nonce, success):
    return log(R.ENTRYPOINT, [USEROP, "0x" + "bb" * 32, addr_topic(wallet), addr_topic(R.NATIVE)],
               "0x" + encode(["uint256", "bool", "uint256", "uint256"], [nonce, success, 1, 1]).hex())


def receipt(logs=None, success=True):
    logs = deepcopy(logs or [])
    for i, item in enumerate(logs):
        item["logIndex"] = hex(i)
    return {"transactionHash": TXHASH, "blockNumber": "0x1", "blockHash": "0x" + "cc" * 32,
            "status": "0x1" if success else "0x0", "logs": logs}


class DecodeTests(unittest.TestCase):
    def setUp(self):
        self.decoder = Decoder({A: {}}, {A: R.SIMPLE_ACCOUNT})

    def test_direct_v2_buy(self):
        signal = self.decoder.decode(tx(v2_swap(), R.V2_ROUTER))[0]
        self.assertEqual((signal.behavior, signal.amount_in_raw), ("BUY", "100"))
        self.assertFalse(signal.copy_eligible)

    def test_v4_new_layout(self):
        signal = self.decoder.decode(tx(v4_swap(), R.UNIVERSAL_ROUTER))[0]
        self.assertEqual(signal.behavior, "BUY")
        self.assertEqual(signal.evidence["min_hop_price_x36"], "0")

    def test_claim_then_swap_preserved(self):
        data = batch([(R.RIPE_CLAIM, 0, claim()), (R.V2_ROUTER, 0, v2_swap())])
        signals = self.decoder.decode(tx(data))
        self.assertEqual([s.behavior for s in signals], ["CLAIM", "BUY"])
        self.assertNotEqual(signals[0].event_id, signals[1].event_id)

    def test_bundler_identity(self):
        data = batch([(R.V2_ROUTER, 0, v2_swap())])
        signals = self.decoder.decode(bundled([op(B, data), op(A, data)]))
        self.assertEqual(len(signals), 1)
        self.assertEqual((signals[0].wallet, signals[0].userop_index), (A, 1))

    def test_fake_entrypoint_not_trusted(self):
        source = bundled([op(A, batch([(R.V2_ROUTER, 0, v2_swap())]))])
        self.assertEqual(self.decoder.decode(replace(source, to=TOKEN)), [])

    def test_unknown_wallet_implementation(self):
        decoder = Decoder({A: {}}, {})
        self.assertEqual(decoder.decode(tx(batch([(R.V2_ROUTER, 0, v2_swap())])))[0].behavior, "UNKNOWN")

    def test_fake_router_selector_not_a_swap(self):
        self.assertEqual(self.decoder.decode(tx(v2_swap(), TOKEN))[0].behavior, "UNKNOWN")

    def test_claim_is_not_buy(self):
        self.assertEqual(self.decoder.decode(tx(claim(), R.RIPE_CLAIM))[0].behavior, "CLAIM")

    def test_malformed_call_fails_closed(self):
        signal = self.decoder.decode(tx(v2_swap()[:12], R.V2_ROUTER))[0]
        self.assertEqual(signal.behavior, "UNKNOWN")

    def test_deposit_is_not_buy(self):
        body = bytes.fromhex("e8017952") + encode(["address", "address", "uint256", "bytes32"], [A, R.USDG, 123, bytes(32)])
        self.assertEqual(self.decoder.decode(tx(body, R.DEPOSITORY))[0].behavior, "INTENT_DEPOSIT")

    def test_metamask_batch(self):
        decoder = Decoder({A: {}}, {A: R.METAMASK_ACCOUNT})
        body = bytes.fromhex("e9ae5c53") + encode(["bytes32", "bytes"], [b"\x01" + bytes(31), encode([CALLS], [[(R.RIPE_CLAIM, 0, claim())]])])
        self.assertEqual(decoder.decode(tx(body))[0].behavior, "CLAIM")

    def test_exact_out_v3_path_reversed(self):
        raw = bytes.fromhex(TOKEN[2:]) + (3000).to_bytes(3, "big") + bytes.fromhex(R.USDG[2:])
        self.assertEqual(v3_path(raw, False), (R.USDG, TOKEN))

    def test_wrong_chain(self):
        self.assertEqual(self.decoder.decode(replace(tx(v2_swap(), R.V2_ROUTER), chain_id=1)), [])


class FeedTests(unittest.TestCase):
    def test_signed_transaction_types(self):
        account = Account.create()
        for kind in (0, 1, 2, 4):
            with self.subTest(kind=kind):
                body = {"chainId": 4663, "nonce": 0, "gas": 200000, "to": to_checksum_address(A), "value": 0, "data": b"abc"}
                if kind in (0, 1):
                    body["gasPrice"] = 1000000000
                else:
                    body.update(maxFeePerGas=1000000000, maxPriorityFeePerGas=0)
                if kind:
                    body["type"] = kind
                    body["accessList"] = []
                if kind == 4:
                    auth = Account.sign_authorization({"chainId": 4663, "address": to_checksum_address(R.SIMPLE_ACCOUNT), "nonce": 1}, account.key)
                    body["authorizationList"] = [auth]
                signed = Account.sign_transaction(body, account.key)
                parsed = decode_raw(bytes(signed.raw_transaction))
                self.assertEqual(parsed.sender, account.address.lower())
                self.assertEqual(parsed.data, b"abc")
                self.assertEqual(parsed.tx_type, kind)

    def test_nested_nitro(self):
        signed = b"\x04raw"
        nested = b"\x03" + len(signed).to_bytes(8, "big") + signed
        outer = b"\x03" + len(nested).to_bytes(8, "big") + nested
        self.assertEqual(signed_transactions(outer), [b"raw"])

    def test_truncated_batch_rejected(self):
        with self.assertRaises(DecodeError):
            signed_transactions(b"\x03" + (5).to_bytes(8, "big") + b"\x04a")

    def test_unsupported_tx_rejected(self):
        with self.assertRaises(DecodeError):
            decode_raw(b"\x03stuff")

    def test_health_stale_and_recovery(self):
        health = FeedHealth()
        self.assertFalse(health.observe(1, 100, 120))
        self.assertTrue(health.observe(2, 120, 120))
        self.assertFalse(health.healthy(126))

    def test_gap_fail_closed_until_reset(self):
        health = FeedHealth()
        health.observe(1, 100, 100)
        self.assertFalse(health.observe(3, 100, 100))
        self.assertFalse(health.observe(4, 100, 100))
        health.reset()
        self.assertTrue(health.observe(5, 100, 100))

    def test_future_timestamp(self):
        self.assertFalse(FeedHealth().observe(1, 200, 100))

    def test_confirm_frame_and_duplicate(self):
        health = FeedHealth()
        self.assertEqual(list(envelopes('{"version":1,"confirmedSequenceNumberMessage":{}}', health, 100)), [])
        frame = json.dumps({"version": 1, "messages": [{"sequenceNumber": 1, "message": {"message": {
            "header": {"timestamp": 100}, "l2Msg": base64.b64encode(b"\x04raw").decode()}}}]})
        self.assertEqual(len(list(envelopes(frame, health, 100))), 1)
        self.assertEqual(list(envelopes(frame, health, 100)), [])

    def test_version_rejected(self):
        with self.assertRaises(DecodeError):
            list(envelopes('{"version":99}', FeedHealth()))


class ReceiptTests(unittest.TestCase):
    def test_other_user_swap_not_attributed(self):
        decoder = Decoder({A: {}}, {A: R.SIMPLE_ACCOUNT})
        source = bundled([op(B, b""), op(A, batch([(R.V2_ROUTER, 0, v2_swap())]))])
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v2")
        logs = [log(R.ENTRYPOINT, [BEFORE]), log(TOKEN, [swap_topic]), userop_event(B, 1, True), userop_event(A, 1, True)]
        signal = enrich(source, decoder.decode(source), receipt(logs), {A: {}})[0]
        self.assertIn("no_swap_event_in_this_operation", signal.reasons)

    def test_failed_userop_inside_successful_transaction(self):
        decoder = Decoder({A: {}}, {A: R.SIMPLE_ACCOUNT})
        source = bundled([op(A, batch([(R.V2_ROUTER, 0, v2_swap())]))])
        signal = enrich(source, decoder.decode(source), receipt([log(R.ENTRYPOINT, [BEFORE]), userop_event(A, 1, False)]), {A: {}})[0]
        self.assertEqual(signal.stage, "failed")

    def test_missing_before_execution_not_assumed(self):
        decoder = Decoder({A: {}}, {A: R.SIMPLE_ACCOUNT})
        source = bundled([op(A, batch([(R.V2_ROUTER, 0, v2_swap())]))])
        signal = enrich(source, decoder.decode(source), receipt([userop_event(A, 1, True)]), {A: {}})[0]
        self.assertIsNone(signal.execution_success)

    def test_receipt_hash_mismatch(self):
        r = receipt()
        r["transactionHash"] = "wrong"
        with self.assertRaises(ValueError):
            enrich(tx(b""), [], r, {})

    def test_failed_outer(self):
        source = tx(v2_swap(), R.V2_ROUTER)
        signals = Decoder({A: {}}).decode(source)
        self.assertEqual(enrich(source, signals, receipt(success=False), {A: {}})[0].stage, "failed")

    def test_incoming_transfer_not_buy(self):
        signals = enrich(tx(b"", TOKEN, B), [], receipt([transfer(TOKEN, B, A, 100)]), {A: {}})
        self.assertEqual(signals[0].behavior, "INCOMING_TRANSFER")
        self.assertFalse(signals[0].copy_eligible)

    def test_empty_topics_safe(self):
        source = tx(v2_swap(), R.V2_ROUTER)
        signal = enrich(source, Decoder({A: {}}).decode(source), receipt([log(TOKEN, [])]), {A: {}})[0]
        self.assertEqual(signal.stage, "needs_review")

    def test_v4_evidence_requires_wallet_flows(self):
        source = tx(v4_swap(), R.UNIVERSAL_ROUTER)
        signals = Decoder({A: {}}).decode(source)
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v4")
        swap_log = log(R.V4_MANAGER, [swap_topic, signals[0].pool_id, addr_topic(R.UNIVERSAL_ROUTER)])
        signal = enrich(source, signals, receipt([swap_log]), {A: {}})[0]
        self.assertEqual(signal.stage, "needs_review")


class FixtureTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.watch = R.load_watchlist(ROOT / 'data/fomo_watchlist.csv')
        cls.decoder = Decoder(cls.watch, R.snapshot_delegations(ROOT / 'data/account_codes.json'))
        cls.examples = json.loads((ROOT / 'data/transaction_examples.json').read_text())

    def example(self, prefix):
        row = next(r for r in self.examples['examples'] if r['transaction']['hash'].startswith(prefix))
        source = Transaction.from_rpc(row['transaction'])
        return enrich(source, self.decoder.decode(source), row['receipt'], self.watch)

    def test_watchlist_count(self):
        self.assertEqual(len(self.watch), 67)

    def test_real_claim(self):
        self.assertEqual([s.behavior for s in self.example('0x5a74b0')], ['CLAIM'])

    def test_real_direct_buy(self):
        trades = [s for s in self.example('0xd69b89') if s.behavior in TRADE_BEHAVIORS]
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].behavior, 'BUY')
        self.assertEqual(trades[0].amount_in_raw, '260000000000000000')
        self.assertIn('nonzero_hook_requires_review', trades[0].reasons)
        self.assertFalse(trades[0].copy_eligible)

    def test_real_self_sell(self):
        trades = [s for s in self.example('0x781ddd') if s.behavior in TRADE_BEHAVIORS]
        self.assertEqual(len(trades), 1)
        self.assertEqual((trades[0].behavior, trades[0].mode), ('SELL', 'self_account'))

    def test_real_live_deposit(self):
        live = self.examples['live_example']
        source = decode_raw(bytes.fromhex(live['capture']['raw'][2:]))
        signals = enrich(source, self.decoder.decode(source), live['receipt'], self.watch)
        self.assertEqual([s.behavior for s in signals], ['APPROVAL', 'INTENT_DEPOSIT'])
        self.assertEqual(signals[1].amount_in_raw, '23420301')
        self.assertTrue(signals[1].execution_success)

    def test_real_bulk_distribution_summary(self):
        row = json.loads((ROOT / 'data/bulk_distribution.json').read_text())
        source = Transaction.from_rpc(row['transaction'])
        signals = enrich(source, self.decoder.decode(source), row['receipt'], self.watch)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].behavior, 'BULK_DISTRIBUTION')
        self.assertEqual(signals[0].evidence['recipient_count'], 230)
        self.assertEqual(len(signals[0].evidence['watched_recipients']), 32)

    def test_real_external_delivery_not_buy(self):
        signals = self.example('0x542ba9')
        self.assertEqual(signals[0].behavior, 'EXTERNAL_DELIVERY_CANDIDATE')
        self.assertFalse(signals[0].copy_eligible)

    def test_real_transfer_not_sell(self):
        self.assertEqual(self.example('0x5ef512')[0].behavior, 'TRANSFER')

    def test_real_liquidity_not_buy(self):
        signals = self.example('0x365af7')
        self.assertTrue(any(s.behavior == 'LIQUIDITY' for s in signals))
        self.assertFalse(any(s.behavior in TRADE_BEHAVIORS for s in signals))

    def test_real_all_signals_no_live_eligibility(self):
        for row in self.examples['examples']:
            self.assertTrue(all(not s.copy_eligible for s in self.example(row['transaction']['hash'])))


class SafetyTests(unittest.TestCase):
    def test_rpc_broadcast_forbidden(self):
        rpc = ReadOnlyRpc('https://example.com')
        with self.assertRaises(PermissionError):
            asyncio.run(rpc.call('eth_sendRawTransaction', ['0x00']))

    def test_remote_plain_http_rejected(self):
        with self.assertRaises(ValueError):
            ReadOnlyRpc('http://example.com')

    def test_rpc_errors_do_not_leak_endpoint(self):
        rpc = ReadOnlyRpc('https://example.com/secret-token')
        with patch('urllib.request.urlopen', side_effect=OSError('secret-token')):
            with self.assertRaisesRegex(Exception, '^RPC transport failure: OSError$'):
                rpc._request('eth_chainId', [], 1)

    def test_rpc_async_dispatch(self):
        rpc = ReadOnlyRpc('https://example.com')
        with patch('asyncio.to_thread', new=AsyncMock(return_value='0x1237')) as dispatch:
            self.assertEqual(asyncio.run(rpc.call('eth_chainId')), '0x1237')
            dispatch.assert_awaited_once()

    def test_store_idempotent_and_no_stage_downgrade(self):
        store = Store(':memory:')
        signal = Signal(TXHASH, A, 'direct', 'CLAIM', 'call', R.RIPE_CLAIM, '0x815a4392')
        self.assertTrue(store.put(signal))
        self.assertFalse(store.put(signal))
        signal.stage = 'execution_observed'
        self.assertTrue(store.put(signal))
        signal.stage = 'intent'
        self.assertFalse(store.put(signal))
        self.assertEqual(len(list(store.rows())), 1)
        store.close()

    def test_duplicate_watchlist_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'watch.csv'
            path.write_text('real_evm\n' + A + '\n' + A + '\n')
            with self.assertRaises(ValueError):
                R.load_watchlist(path)


if __name__ == '__main__':
    unittest.main()
