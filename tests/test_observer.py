from __future__ import annotations

import asyncio
import base64
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta
import hashlib
import hmac
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from eth_abi import decode, encode
from eth_account import Account
from eth_utils import to_checksum_address

from smart_money import registry as R
from smart_money.account_state import prestate_implementations
from smart_money.approval import (
    approve_relationship_token, approve_relationship_usdg,
    confirm_relationship_token_approval,
)
from smart_money.backfill import BlockScanner, ReorgDetected
from smart_money.broadcast import MainnetBroadcaster
from smart_money.cli import (
    LatencySamples, coverage_summary, dispatch_pending, execution_track,
    live_wallet_execution_locks, monitoring_watchlist, parser as cli_parser,
    prepare_runtime_budget_cycle, relay_associate, validate_live_relationships,
)
from smart_money.config import load_endpoint_env
from smart_money.decode import (
    CALLS, PACKED_OPS, POOL_KEY, RELAY_CALLS, V4_PATH_KEY, Decoder, selector,
    v3_hops, v3_path,
)
from smart_money.feed import DecodeError, FeedHealth, decode_raw, envelopes, signed_transactions
from smart_money.execution_prep import (
    ReadOnlyExecutionPreflight, UnsignedExecutionPlan, build_execution_plan,
)
from smart_money.execution_pipeline import (
    ExecutionPreparer, OfflineExecutionSigner, ReadOnlyPreBroadcastReviewer,
)
from smart_money.execution_receipts import ReadOnlyExecutionTracker
from smart_money.execution_controls import (
    require_mainnet_broadcast_enabled, require_mainnet_signing_enabled,
    require_offline_signing_enabled,
)
from smart_money.key_source import (
    LiveDatabaseSigner, OfflineDatabaseSigner, key_record_status,
    live_key_record_status,
)
from smart_money.ledger_migration import migrate_sqlite_ledger, sqlite_sha256
from smart_money.live_settlement import settle_confirmed_execution, wallet_erc20_deltas
from smart_money.models import Signal, Transaction
from smart_money.mysql_config import (
    MySqlRelationshipGate, import_watchlist_relationships,
    load_enabled_mainnet_acceptance, load_mysql_paper_config, mysql_connection,
    rows_to_document,
)
from smart_money.mysql_store import MySqlConnectionCompat
from smart_money.native_flows import verify_native_flows
from smart_money.okx import OkxError, OkxSwapClient
from smart_money.pools import V3_SWAP, discover_v3_execution_route, verify_signal_pools
from smart_money.paper import (
    AmountRule, PaperEngine, PaperExecutor, PaperValuator, budget_bucket,
    execution_quote_signal, planned_input_amount, reverse_quote_signal,
    scope_reason, signal_route_key,
    trigger_allowed,
)
from smart_money.paper_config import load_paper_config
from smart_money.quotes import LiveQuoter, Quote, QuotePolicy, assess_quote, validate_quote
from smart_money.receipts import (
    BEFORE, DEPOSIT_RECORDED, PONS_V2_SWAP, SWAPS, TRANSFER, USEROP, TRADE_BEHAVIORS,
    enrich,
)
from smart_money.rpc import ReadOnlyRpc
from smart_money.relay_api import RelayNotReady, RelayPublicClient
from smart_money.solver import (
    relay_confirmed_sell, relay_delivery_evidence, relay_passive_buy,
)
from smart_money.store import MAX_CANDIDATE_ATTEMPTS, Store

ROOT = Path(__file__).resolve().parents[1]
A = "0x" + "11" * 20
B = "0x" + "22" * 20
TOKEN = "0x" + "33" * 20
TXHASH = "0x" + "aa" * 32
OFFLINE_ENV = {
    'SMART_MONEY_EMERGENCY_STOP': '0',
    'SMART_MONEY_EXECUTION_MODE': 'offline_test',
    'SMART_MONEY_SIGNING_MODE': 'offline_test',
    'SMART_MONEY_EMERGENCY_STOP_FILE': str(
        ROOT / 'tests/.execution-stop-not-active'),
}
OFFLINE_CONTROL_KEYS = (
    'SMART_MONEY_EMERGENCY_STOP',
    'SMART_MONEY_EXECUTION_MODE',
    'SMART_MONEY_SIGNING_MODE',
)


def accepted_mainnet_relationship(policy, *, stale=False):
    accepted_at = datetime(2026, 9, 13, 0, 0, 0)
    return {
        'policy': policy,
        'accepted_at': accepted_at,
        'updated_at': accepted_at + timedelta(microseconds=1) if stale else accepted_at,
    }


def tx(data, to=A, sender=A):
    return Transaction(TXHASH, sender, to, data, fresh=True)


def claim():
    return bytes.fromhex("815a4392") + encode(["address", "bool"], [A, False])


def v2_swap():
    return selector("swapExactTokensForTokens(uint256,uint256,address[],address,uint256)") + encode(
        ["uint256", "uint256", "address[]", "address", "uint256"], [100, 90, [R.USDG, TOKEN], A, 2000000000])


def v4_swap():
    key = (TOKEN, R.USDG, 3000, 60, R.NATIVE)
    param = encode([f"({POOL_KEY},bool,uint128,uint128,uint256,bytes)"], [(key, False, 100, 90, 0, b"")])
    action = encode(["bytes", "bytes[]"], [b"\x06", [param]])
    return bytes.fromhex("3593564c") + encode(["bytes", "bytes[]", "uint256"], [b"\x10", [action], 2000000000])


def v4_settled_swap(recipient=A):
    key = (TOKEN, R.USDG, 3000, 60, R.NATIVE)
    swap_param = encode([f"({POOL_KEY},bool,uint128,uint128,uint256,bytes)"],
                        [(key, False, 100, 90, 0, b"")])
    settle = encode(["address", "uint256", "bool"], [R.USDG, 100, True])
    take = encode(["address", "address", "uint256"], [TOKEN, recipient, 0])
    action = encode(["bytes", "bytes[]"], [b"\x06\x0b\x0e", [swap_param, settle, take]])
    return bytes.fromhex("3593564c") + encode(
        ["bytes", "bytes[]", "uint256"], [b"\x10", [action], 2000000000])


def v4_multihop(exact_in=True):
    middle = "0x" + "44" * 20
    if exact_in:
        path = [(middle, 500, 10, R.NATIVE, b""), (TOKEN, 3000, 60, R.NATIVE, b"")]
        swap_param = encode(["address", f"{V4_PATH_KEY}[]", "uint256[]", "uint128", "uint128"],
                            [R.USDG, path, [], 100, 90])
        action_id = 7
    else:
        path = [(R.USDG, 500, 10, R.NATIVE, b""), (middle, 3000, 60, R.NATIVE, b"")]
        swap_param = encode(["address", f"{V4_PATH_KEY}[]", "uint256[]", "uint128", "uint128"],
                            [TOKEN, path, [], 90, 100])
        action_id = 9
    settle = encode(["address", "uint256"], [R.USDG, 100])
    take = encode(["address", "uint256"], [TOKEN, 90])
    action = encode(["bytes", "bytes[]"],
                    [bytes([action_id, 0x0c, 0x0f]), [swap_param, settle, take]])
    return bytes.fromhex("3593564c") + encode(
        ["bytes", "bytes[]", "uint256"], [b"\x10", [action], 2000000000])


def relay_deposit_all(order=b"\x01" * 32):
    return bytes.fromhex("5a1ee3ac") + encode(
        ["address", "address", "bytes32"], [A, R.USDG, order])


def relay_cleanup(deposit=None, cleanup_token=R.USDG):
    deposit = deposit or relay_deposit_all()
    return bytes.fromhex("73b7bb2f") + encode(
        ["address[]", "address[]", "bytes[]", "uint256[]"],
        [[cleanup_token], [R.DEPOSITORY], [deposit], [0]])


def zero_x_exec(token=TOKEN, amount=100):
    return bytes.fromhex("2213bc0b") + encode(
        ["address", "address", "uint256", "address", "bytes"],
        [B, token, amount, B, bytes.fromhex("1fff991f")])


def kyber_swap(token=TOKEN, amount=100, output=R.USDG,
               receiver=R.RELAY_ROUTER, minimum=90):
    description = (
        token, output, [B], [amount], [], [], receiver, amount, minimum, 0, b"")
    description_type = (
        "(address,address,address[],uint256[],address[],uint256[],address,uint256,uint256,uint256,bytes)"
    )
    execution_type = f"(address,address,bytes,{description_type},bytes)"
    execution = (B, B, b"\x12\x34\x56\x78", description, b"")
    return bytes.fromhex("e21fd0e9") + encode([execution_type], [execution])


def relay_proxy(calls):
    return bytes.fromhex("f9e4bab4") + encode(
        ["address[]", "uint256[]", RELAY_CALLS, "address", "address", "bytes"],
        [[TOKEN], [100], calls, A, A, b""])


def relay_sell(aggregator, aggregator_address):
    calls = [
        (TOKEN, False, 0, bytes.fromhex("095ea7b3")
         + encode(["address", "uint256"], [aggregator_address, 100])),
        (aggregator_address, False, 0, aggregator),
        (R.RELAY_ROUTER, False, 0, relay_cleanup()),
    ]
    return batch([(R.RELAY_PROXY, 0, relay_proxy(calls))])


def batch(calls):
    return bytes.fromhex("34fcd5be") + encode([CALLS], [calls])


def op(wallet, data, nonce=1):
    return (wallet, nonce, b"", data, bytes(32), 0, bytes(32), b"", b"signature")


def bundled(ops):
    return tx(bytes.fromhex("765e827f") + encode([PACKED_OPS, "address"], [ops, B]), R.ENTRYPOINT, B)


def log(contract, topics, data="0x", index=0):
    return {"address": contract, "topics": topics, "data": data, "logIndex": hex(index)}


def passive_candidate():
    return Signal(
        TXHASH, A, "third_party", "EXTERNAL_DELIVERY_CANDIDATE", "incoming",
        B, "0x12345678", stage="needs_review", execution_status="success",
        execution_success=True, evidence={
            "wallet_erc20_deltas_raw": {TOKEN: "90"}, "swap_event_count": 1,
        })


def relay_buy_document():
    source_hash = "0x" + "bb" * 32
    return {"requests": [{
        "id": "0x" + "02" * 32, "status": "success", "user": A,
        "recipient": A, "data": {
            "inTxs": [{"hash": source_hash, "chainId": 8453, "status": "success"}],
            "outTxs": [{
                "hash": TXHASH, "chainId": R.CHAIN_ID, "status": "success",
                "stateChanges": [{"address": A, "change": {
                    "kind": "token", "balanceDiff": "90", "data": {
                        "tokenKind": "ft", "tokenAddress": TOKEN,
                    }}}],
            }],
        }, "protocol": {
            "orderId": "0x" + "01" * 32,
            "deposit": {"origin": {
                "amount": "100", "chainId": 8453, "currency": R.USDG,
                "depositor": A, "transactionId": source_hash,
            }},
            "settlement": {"destination": {"fills": [{
                "chainId": R.CHAIN_ID, "transactionId": TXHASH,
            }]}},
            "orderData": {"output": {"payments": [{
                "currency": TOKEN, "recipient": A, "minimumAmount": "80",
            }]}},
        },
    }]}


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
        self.assertEqual((signal.intent_status, signal.execution_status, signal.canonical_status),
                         ("observed", "pending", "unconfirmed"))
        self.assertFalse(signal.copy_eligible)

    def test_v4_new_layout(self):
        signal = self.decoder.decode(tx(v4_swap(), R.UNIVERSAL_ROUTER))[0]
        self.assertEqual(signal.behavior, "BUY")
        self.assertEqual(signal.evidence["min_hop_price_x36"], "0")

    def test_v4_settlement_recipient_is_preserved(self):
        signal = self.decoder.decode(tx(v4_settled_swap(), R.UNIVERSAL_ROUTER))[0]
        self.assertEqual(signal.evidence["v4_settlement_actions"][1]["recipient"], A)

    def test_v4_exact_in_multihop_preserves_every_pool(self):
        signal = self.decoder.decode(tx(v4_multihop(True), R.UNIVERSAL_ROUTER))[0]
        self.assertEqual((signal.token_in, signal.token_out, signal.exact_in),
                         (R.USDG, TOKEN, True))
        self.assertEqual(len(signal.evidence["v4_hops"]), 2)
        self.assertEqual(len(signal.evidence["v4_pool_ids"]), 2)
        self.assertEqual(signal.evidence["v4_hops"][0]["token_out"], "0x" + "44" * 20)

    def test_v4_exact_out_multihop_restores_logical_route(self):
        signal = self.decoder.decode(tx(v4_multihop(False), R.UNIVERSAL_ROUTER))[0]
        self.assertEqual((signal.token_in, signal.token_out, signal.exact_in),
                         (R.USDG, TOKEN, False))
        self.assertEqual([hop["token_in"] for hop in signal.evidence["v4_hops"]],
                         [R.USDG, "0x" + "44" * 20])

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

    def test_relay_zero_x_sell_links_only_through_unique_cleanup_deposit(self):
        signals = self.decoder.decode(tx(relay_sell(
            zero_x_exec(), R.ZERO_X_ALLOWANCE_HOLDER)))
        trade = next(item for item in signals if item.behavior in TRADE_BEHAVIORS)
        deposit = next(item for item in signals if item.behavior == "INTENT_DEPOSIT")
        self.assertEqual((trade.behavior, trade.protocol, trade.token_in, trade.token_out),
                         ("SELL", "0x", TOKEN, R.USDG))
        self.assertEqual(trade.evidence["relay_deposit_order_id"], "0x" + "01" * 32)
        self.assertEqual(deposit.evidence["deposit_amount_source"],
                         "full_allowance_receipt_event")
        self.assertFalse(trade.copy_eligible)

    def test_relay_kyber_sell_preserves_declared_minimum(self):
        signals = self.decoder.decode(tx(relay_sell(
            kyber_swap(), R.KYBER_META_AGGREGATION_ROUTER_V2)))
        trade = next(item for item in signals if item.behavior in TRADE_BEHAVIORS)
        self.assertEqual((trade.behavior, trade.protocol, trade.amount_in_raw,
                          trade.amount_limit_raw, trade.recipient),
                         ("SELL", "kyber", "100", "90", R.RELAY_ROUTER))

    def test_unlinked_aggregator_call_stays_unknown(self):
        signal = self.decoder.decode(tx(zero_x_exec(), R.ZERO_X_ALLOWANCE_HOLDER))[0]
        self.assertEqual(signal.behavior, "UNKNOWN")
        self.assertIn("aggregator_call_not_linked_to_relay_sell", signal.reasons)

    def test_relay_cleanup_token_mismatch_stays_unknown(self):
        calls = [
            (R.ZERO_X_ALLOWANCE_HOLDER, False, 0, zero_x_exec()),
            (R.RELAY_ROUTER, False, 0, relay_cleanup(cleanup_token=B)),
        ]
        signals = self.decoder.decode(tx(batch([(R.RELAY_PROXY, 0, relay_proxy(calls))])))
        trade = next(item for item in signals if item.protocol == "0x")
        self.assertEqual(trade.behavior, "UNKNOWN")
        self.assertIn("aggregator_call_relay_deposit_identity_mismatch", trade.reasons)

    def test_relay_calls_in_different_userops_are_never_joined(self):
        aggregator_only = batch([(R.RELAY_PROXY, 0, relay_proxy([
            (R.ZERO_X_ALLOWANCE_HOLDER, False, 0, zero_x_exec()),
        ]))])
        deposit_only = batch([(R.RELAY_PROXY, 0, relay_proxy([
            (R.RELAY_ROUTER, False, 0, relay_cleanup()),
        ]))])
        signals = self.decoder.decode(bundled([
            op(A, aggregator_only, nonce=1), op(A, deposit_only, nonce=2),
        ]))
        trade = next(item for item in signals if item.protocol == "0x")
        self.assertEqual((trade.userop_index, trade.behavior), (0, "UNKNOWN"))
        self.assertIn("aggregator_call_not_linked_to_unique_relay_deposit",
                      trade.reasons)
        deposit = next(item for item in signals if item.behavior == "INTENT_DEPOSIT")
        self.assertEqual(deposit.userop_index, 1)
        self.assertFalse(any(item.behavior in TRADE_BEHAVIORS for item in signals))

    def test_relay_full_allowance_sell_closes_from_receipt_amount(self):
        source = tx(relay_sell(zero_x_exec(), R.ZERO_X_ALLOWANCE_HOLDER))
        signals = self.decoder.decode(source)
        swap_topic = next(topic for topic, protocol in SWAPS.items() if protocol == "v2")
        order = b"\x01" * 32
        logs = [
            transfer(TOKEN, A, R.RELAY_ROUTER, 100),
            log(B, [swap_topic]),
            log(R.DEPOSITORY, [DEPOSIT_RECORDED], "0x" + encode(
                ["address", "address", "uint256", "bytes32"],
                [A, R.USDG, 95, order]).hex()),
        ]
        result = enrich(source, signals, receipt(logs), {A: {}})
        trade = next(item for item in result if item.behavior == "SELL")
        deposit = next(item for item in result if item.behavior == "INTENT_DEPOSIT")
        self.assertEqual((trade.stage, trade.evidence["actual_input_debit_raw"],
                          trade.evidence["actual_output_deposit_raw"]),
                         ("relay_sell_evidenced", "100", "95"))
        self.assertEqual(deposit.amount_in_raw, "95")
        self.assertFalse(trade.copy_eligible)

    def test_weth_wrap_is_not_buy_and_preserves_raw_value(self):
        source = replace(tx(bytes.fromhex("d0e30db0"), R.WETH), value=123)
        signal = self.decoder.decode(source)[0]
        self.assertEqual((signal.behavior, signal.amount_in_raw), ("WRAP_NATIVE", "123"))
        self.assertFalse(signal.copy_eligible)

    def test_weth_unwrap_is_not_sell(self):
        data = bytes.fromhex("2e1a7d4d") + encode(["uint256"], [123])
        signal = self.decoder.decode(tx(data, R.WETH))[0]
        self.assertEqual((signal.behavior, signal.amount_in_raw), ("UNWRAP_WETH", "123"))
        self.assertNotEqual(signal.behavior, "SELL")

    def test_metamask_batch(self):
        decoder = Decoder({A: {}}, {A: R.METAMASK_ACCOUNT})
        body = bytes.fromhex("e9ae5c53") + encode(["bytes32", "bytes"], [b"\x01" + bytes(31), encode([CALLS], [[(R.RIPE_CLAIM, 0, claim())]])])
        self.assertEqual(decoder.decode(tx(body))[0].behavior, "CLAIM")

    def test_exact_out_v3_path_reversed(self):
        raw = bytes.fromhex(TOKEN[2:]) + (3000).to_bytes(3, "big") + bytes.fromhex(R.USDG[2:])
        self.assertEqual(v3_path(raw, False), (R.USDG, TOKEN))

    def test_v3_multihop_preserves_execution_order_and_fees(self):
        middle = "0x" + "44" * 20
        raw = (bytes.fromhex(R.USDG[2:]) + (500).to_bytes(3, "big") + bytes.fromhex(middle[2:])
               + (3000).to_bytes(3, "big") + bytes.fromhex(TOKEN[2:]))
        self.assertEqual(v3_hops(raw, True), [
            {"token_in": R.USDG, "token_out": middle, "fee": 500},
            {"token_in": middle, "token_out": TOKEN, "fee": 3000},
        ])
        self.assertEqual(v3_hops(raw, False)[0],
                         {"token_in": TOKEN, "token_out": middle, "fee": 3000})

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
    def test_weth_wrap_requires_exact_wallet_credit(self):
        source = replace(tx(bytes.fromhex("d0e30db0"), R.WETH), value=123)
        signal = Decoder({A: {}}).decode(source)[0]
        result = enrich(source, [signal], receipt([transfer(R.WETH, R.NATIVE, A, 123)]), {A: {}})[0]
        self.assertEqual((result.behavior, result.stage), ("WRAP_NATIVE", "execution_observed"))
        self.assertEqual(result.evidence["actual_output_credit_raw"], "123")
        self.assertFalse(result.copy_eligible)

        signal = Decoder({A: {}}).decode(source)[0]
        mismatch = enrich(source, [signal], receipt([transfer(R.WETH, R.NATIVE, A, 122)]), {A: {}})[0]
        self.assertEqual(mismatch.stage, "needs_review")

    def test_native_state_diff_closes_v4_buy_without_counting_gas(self):
        source = tx(b"", R.UNIVERSAL_ROUTER)
        pool_id = "0x" + "66" * 32
        signal = Signal(TXHASH, A, "self", "BUY", "v4/native", R.UNIVERSAL_ROUTER, "0x",
                        token_in=R.NATIVE, token_out=TOKEN, protocol="v4", pool_id=pool_id,
                        evidence={"pool_key": [R.NATIVE, TOKEN, 3000, 60, R.NATIVE],
                                  "v4_settlement_actions": [
                                      {"action": "SETTLE_ALL", "currency": R.NATIVE,
                                       "payer_is_user": True},
                                      {"action": "TAKE_ALL", "currency": TOKEN,
                                       "recipient": A}]})
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v4")
        logs = [log(R.V4_MANAGER, [swap_topic, pool_id, addr_topic(R.UNIVERSAL_ROUTER)]),
                transfer(TOKEN, R.V4_MANAGER, A, 95)]
        checks = {signal.event_id: {"verified": True, "pool_id": pool_id}}
        native = {signal.event_id: {"verified": True, "wallet_is_outer_transaction_sender": True,
                                    "wallet_native_asset_delta_raw": "-100"}}
        result = enrich(source, [signal], receipt(logs), {A: {}}, checks, native)[0]
        self.assertEqual(result.stage, "swap_evidenced")
        self.assertEqual(result.evidence["actual_input_debit_raw"], "100")
        self.assertEqual(result.evidence["actual_output_credit_raw"], "95")

    def test_bundled_native_flow_requires_matching_v4_pool_delta(self):
        source = tx(b"", R.UNIVERSAL_ROUTER, B)
        pool_id = "0x" + "66" * 32
        signal = Signal(TXHASH, A, "bundled_account", "BUY", "v4/native", R.UNIVERSAL_ROUTER, "0x",
                        token_in=R.NATIVE, token_out=TOKEN, protocol="v4", pool_id=pool_id,
                        evidence={"pool_key": [R.NATIVE, TOKEN, 3000, 60, R.NATIVE],
                                  "v4_settlement_actions": [
                                      {"action": "SETTLE_ALL", "currency": R.NATIVE,
                                       "payer_is_user": True},
                                      {"action": "TAKE_ALL", "currency": TOKEN, "recipient": A}]})
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v4")
        swap_data = "0x" + encode(
            ["int128", "int128", "uint160", "uint128", "int24", "uint24"],
            [-100, 95, 1, 1, 0, 0]).hex()
        logs = [log(R.V4_MANAGER, [swap_topic, pool_id, addr_topic(R.UNIVERSAL_ROUTER)], swap_data),
                transfer(TOKEN, R.V4_MANAGER, A, 95)]
        pools = {signal.event_id: {"verified": True, "pool_id": pool_id}}
        native = {signal.event_id: {"verified": True, "wallet_is_outer_transaction_sender": False,
                                    "wallet_native_asset_delta_raw": "-101"}}
        result = enrich(source, [signal], receipt(logs), {A: {}}, pools, native)[0]
        self.assertEqual(result.stage, "needs_review")
        self.assertEqual(result.evidence["native_flow_verification"]["reason"],
                         "bundled_native_flow_not_separable_from_gas_or_hook")

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
        result = enrich(source, signals, receipt(success=False), {A: {}})[0]
        self.assertEqual((result.stage, result.execution_status), ("failed", "reverted"))

    def test_incoming_transfer_not_buy(self):
        signals = enrich(tx(b"", TOKEN, B), [], receipt([transfer(TOKEN, B, A, 100)]), {A: {}})
        self.assertEqual(signals[0].behavior, "INCOMING_TRANSFER")
        self.assertFalse(signals[0].copy_eligible)
        self.assertEqual(signals[0].intent_status, "not_attributed")

    def test_empty_topics_safe(self):
        source = tx(v2_swap(), R.V2_ROUTER)
        signal = enrich(source, Decoder({A: {}}).decode(source), receipt([log(TOKEN, [])]), {A: {}})[0]
        self.assertEqual(signal.stage, "needs_review")

    def test_verified_v2_pool_still_requires_exact_event_and_wallet_flows(self):
        source = tx(v2_swap(), R.V2_ROUTER)
        signals = Decoder({A: {}}).decode(source)
        signal = signals[0]
        pool = "0x" + "44" * 20
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v2")
        logs = [
            log(pool, [swap_topic]),
            transfer(R.USDG, A, pool, 100),
            transfer(TOKEN, pool, A, 95),
        ]
        checks = {signal.event_id: {"verified": True, "factory": R.V2_FACTORY,
                                    "pools": [{"address": pool}]}}
        result = enrich(source, signals, receipt(logs), {A: {}}, checks)[0]
        self.assertEqual(result.stage, "swap_evidenced")
        self.assertEqual(result.evidence["actual_input_debit_raw"], "100")
        self.assertEqual(result.evidence["actual_output_credit_raw"], "95")
        self.assertFalse(result.copy_eligible)

    def test_verified_pool_does_not_accept_other_pool_swap(self):
        source = tx(v2_swap(), R.V2_ROUTER)
        signals = Decoder({A: {}}).decode(source)
        signal = signals[0]
        pool = "0x" + "44" * 20
        other_pool = "0x" + "55" * 20
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v2")
        checks = {signal.event_id: {"verified": True, "pools": [{"address": pool}]}}
        result = enrich(source, signals, receipt([log(other_pool, [swap_topic])]), {A: {}}, checks)[0]
        self.assertEqual(result.stage, "needs_review")
        self.assertIn("verified_pool_swap_events_not_exactly_matched", result.reasons)

    def test_v4_settlement_and_wallet_flows_close_attribution(self):
        source = tx(v4_settled_swap(), R.UNIVERSAL_ROUTER)
        signals = Decoder({A: {}}).decode(source)
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v4")
        logs = [
            log(R.V4_MANAGER, [swap_topic, signals[0].pool_id, addr_topic(R.UNIVERSAL_ROUTER)]),
            transfer(R.USDG, A, R.V4_MANAGER, 100),
            transfer(TOKEN, R.V4_MANAGER, A, 95),
        ]
        checks = {signals[0].event_id: {"verified": True, "manager": R.V4_MANAGER,
                                       "pool_id": signals[0].pool_id}}
        result = enrich(source, signals, receipt(logs), {A: {}}, checks)[0]
        self.assertEqual(result.stage, "swap_evidenced")
        self.assertEqual(result.recipient, A)
        self.assertFalse(result.evidence["recipient_requires_settlement_check"])

    def test_v4_take_for_other_recipient_is_not_attributed(self):
        source = tx(v4_settled_swap(B), R.UNIVERSAL_ROUTER)
        signals = Decoder({A: {}}).decode(source)
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v4")
        logs = [log(R.V4_MANAGER, [swap_topic, signals[0].pool_id,
                                   addr_topic(R.UNIVERSAL_ROUTER)])]
        checks = {signals[0].event_id: {"verified": True, "manager": R.V4_MANAGER,
                                       "pool_id": signals[0].pool_id}}
        result = enrich(source, signals, receipt(logs), {A: {}}, checks)[0]
        self.assertEqual(result.stage, "needs_review")
        self.assertIn("v4_wallet_settlement_not_uniquely_proven", result.reasons)

    def test_v4_multihop_requires_and_accepts_every_exact_pool_event(self):
        source = tx(v4_multihop(True), R.UNIVERSAL_ROUTER)
        signal = Decoder({A: {}}).decode(source)[0]
        swap_topic = next(t for t, protocol in SWAPS.items() if protocol == "v4")
        logs = [log(R.V4_MANAGER, [swap_topic, pool_id, addr_topic(R.UNIVERSAL_ROUTER)])
                for pool_id in signal.evidence["v4_pool_ids"]]
        logs.extend([transfer(R.USDG, A, R.V4_MANAGER, 100),
                     transfer(TOKEN, R.V4_MANAGER, A, 95)])
        pools = {signal.event_id: {"verified": True,
                                  "pool_ids": signal.evidence["v4_pool_ids"]}}
        result = enrich(source, [signal], receipt(logs), {A: {}}, pools)[0]
        self.assertEqual(result.stage, "swap_evidenced")
        self.assertEqual(result.evidence["matching_pool_events"], 2)

        signal = Decoder({A: {}}).decode(source)[0]
        missing = enrich(source, [signal], receipt(logs[1:]), {A: {}}, pools)[0]
        self.assertEqual(missing.stage, "needs_review")


class PoolVerificationTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def encoded_address(value):
        return "0x" + encode(["address"], [value]).hex()

    async def test_v4_pool_key_and_manager_are_verified_at_receipt_block(self):
        signal = Decoder({A: {}}).decode(tx(v4_swap(), R.UNIVERSAL_ROUTER))[0]

        async def rpc_call(method, params=None):
            self.assertEqual(method, "eth_getCode")
            self.assertEqual(params, [R.V4_MANAGER, "0x1"])
            return "0x01"

        checks = await verify_signal_pools(
            AsyncMock(call=AsyncMock(side_effect=rpc_call)), [signal], receipt())
        self.assertTrue(checks[signal.event_id]["verified"])
        self.assertEqual(checks[signal.event_id]["pool_id"], signal.pool_id)

    async def test_native_state_diff_separates_outer_sender_gas(self):
        signal = Signal(TXHASH, A, "self", "BUY", "native", R.UNIVERSAL_ROUTER, "0x",
                        token_in=R.NATIVE, token_out=TOKEN, protocol="v4")
        trace = {"pre": {A: {"balance": "0x3e8"}},
                 "post": {A: {"balance": "0x2bc"}}}
        rpc = AsyncMock(call=AsyncMock(return_value=trace))
        r = receipt()
        r.update({"gasUsed": "0xa", "effectiveGasPrice": "0x5"})
        checks = await verify_native_flows(rpc, tx(b"", R.UNIVERSAL_ROUTER), r, [signal])
        self.assertEqual(checks[signal.event_id]["wallet_native_delta_including_gas_raw"], "-300")
        self.assertEqual(checks[signal.event_id]["outer_transaction_gas_adjustment_raw"], "50")
        self.assertEqual(checks[signal.event_id]["wallet_native_asset_delta_raw"], "-250")

    async def test_transaction_prestate_recovers_only_known_delegation(self):
        unknown = "0x" + "55" * 20
        trace = {
            A: {"code": "0xef0100" + R.SIMPLE_ACCOUNT[2:]},
            B: {"balance": "0x1"},
            unknown: {"code": "0x6000"},
        }
        implementations, missing = await prestate_implementations(
            AsyncMock(call=AsyncMock(return_value=trace)), TXHASH, [A, B, unknown])
        self.assertEqual(implementations, {A: R.SIMPLE_ACCOUNT})
        self.assertEqual(missing, {unknown})

    async def test_v2_factory_pool_and_tokens_verified_at_receipt_block(self):
        signal = Decoder({A: {}}).decode(tx(v2_swap(), R.V2_ROUTER))[0]
        pool = "0x" + "44" * 20

        async def rpc_call(method, params=None):
            if method == "eth_getCode":
                return "0x01"
            target, data = params[0]["to"], params[0]["data"][:10]
            self.assertEqual(params[1], "0x1")
            if target == R.V2_ROUTER:
                return self.encoded_address(R.V2_FACTORY)
            if target == R.V2_FACTORY:
                return self.encoded_address(pool)
            if data == "0x0dfe1681":
                return self.encoded_address(R.USDG)
            return self.encoded_address(TOKEN)

        checks = await verify_signal_pools(AsyncMock(call=AsyncMock(side_effect=rpc_call)), [signal], receipt())
        self.assertTrue(checks[signal.event_id]["verified"])
        self.assertEqual(checks[signal.event_id]["pools"][0]["address"], pool)

    async def test_delivery_receipt_selects_one_directional_verified_v3_route(self):
        pool = "0x" + "44" * 20
        swap = log(pool, [V3_SWAP, "0x" + "01" * 32, "0x" + "02" * 32],
                   "0x" + encode(
                       ["int256", "int256", "uint160", "uint128", "int24"],
                       [100, -90, 1, 1, 1]).hex())

        async def rpc_call(method, params=None):
            if method == "eth_getCode":
                self.assertEqual(params, [pool, "0x1"])
                return "0x01"
            target, data = params[0]["to"], params[0]["data"][:10]
            self.assertEqual(params[1], "0x1")
            if target == R.V3_FACTORY:
                return self.encoded_address(pool)
            if data == "0x0dfe1681":
                return self.encoded_address(R.USDG)
            if data == "0xd21220a7":
                return self.encoded_address(TOKEN)
            return "0x" + encode(["uint24"], [3000]).hex()

        route = await discover_v3_execution_route(
            AsyncMock(call=AsyncMock(side_effect=rpc_call)), receipt([swap]),
            R.USDG, TOKEN, "90")
        self.assertEqual(route["assets"], [R.USDG, TOKEN])
        self.assertEqual(route["fees"], [3000])
        self.assertEqual(route["verified_pool"], pool)

        with self.assertRaisesRegex(ValueError, "does not select one"):
            await discover_v3_execution_route(
                AsyncMock(call=AsyncMock(side_effect=rpc_call)), receipt([swap]),
                TOKEN, R.USDG, "90")

    async def test_confirmed_live_buy_settles_from_canonical_wallet_deltas(self):
        follower = A
        tx_hash = TXHASH
        execution_receipt = receipt([
            transfer(R.USDG, follower, R.V3_ROUTER, 100),
            transfer(TOKEN, B, follower, 90),
        ])
        execution_receipt.update({"gasUsed": "0x5208", "effectiveGasPrice": "0x2"})
        header = {"hash": execution_receipt["blockHash"], "timestamp": "0x64"}
        rpc = AsyncMock(call=AsyncMock(side_effect=[execution_receipt, header]))
        store = MagicMock()
        store.paper_proposal.return_value = {
            "status": "reserved", "input_asset": R.USDG,
            "output_asset": TOKEN, "amount_in_raw": "100",
            "attribution": {"follower_wallet": follower, "source_behavior": "BUY"},
        }
        store.execution_plan.return_value = {
            "plan_id": "plan-1", "status": "signed",
            "unsigned_plan": {"quote_observed_at": 50.0},
        }
        store.execution_attempts.return_value = [{
            "tx_hash": tx_hash, "status": "confirmed",
            "block_number": 1, "block_hash": execution_receipt["blockHash"],
        }]
        store.fill_paper_buy.return_value = True
        result = await settle_confirmed_execution(store, rpc, "proposal-1", tx_hash)
        self.assertEqual((result["actual_input_raw"], result["actual_output_raw"]),
                         ("100", "90"))
        self.assertEqual(wallet_erc20_deltas(execution_receipt, follower),
                         {R.USDG: -100, TOKEN: 90})
        store.fill_paper_buy.assert_called_once()

    async def test_forged_pool_token_pair_is_rejected(self):
        signal = Decoder({A: {}}).decode(tx(v2_swap(), R.V2_ROUTER))[0]
        pool = "0x" + "44" * 20

        async def rpc_call(method, params=None):
            if method == "eth_getCode":
                return "0x01"
            target, data = params[0]["to"], params[0]["data"][:10]
            if target == R.V2_ROUTER:
                return self.encoded_address(R.V2_FACTORY)
            if target == R.V2_FACTORY:
                return self.encoded_address(pool)
            if data == "0x0dfe1681":
                return self.encoded_address(R.USDG)
            return self.encoded_address(B)

        checks = await verify_signal_pools(AsyncMock(call=AsyncMock(side_effect=rpc_call)), [signal], receipt())
        self.assertFalse(checks[signal.event_id]["verified"])
        self.assertEqual(checks[signal.event_id]["reason"], "pool_token_pair_mismatch")

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
        self.assertIn('nonzero_hook_requires_historical_code_check', trades[0].reasons)
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
        self.assertEqual(signals[1].evidence['solver_order_status'], 'source_deposit_evidenced')
        self.assertEqual(signals[1].evidence['matching_deposit_order_events'], 1)

    def test_deposit_order_id_requires_matching_receipt_event(self):
        signals = self.example('0xffa84e')
        deposit = next(s for s in signals if s.behavior == 'INTENT_DEPOSIT')
        self.assertEqual(deposit.evidence['solver_order_status'], 'source_deposit_evidenced')
        bad_receipt = deepcopy(next(
            row['receipt'] for row in self.examples['examples']
            if row['transaction']['hash'].startswith('0xffa84e')))
        event = next(item for item in bad_receipt['logs'] if item['address'].lower() == R.DEPOSITORY)
        event['data'] = event['data'][:-2] + ('00' if event['data'][-2:] != '00' else '01')
        row = next(row for row in self.examples['examples']
                   if row['transaction']['hash'].startswith('0xffa84e'))
        source = Transaction.from_rpc(row['transaction'])
        altered = enrich(source, self.decoder.decode(source), bad_receipt, self.watch)
        deposit = next(s for s in altered if s.behavior == 'INTENT_DEPOSIT')
        self.assertEqual(deposit.stage, 'needs_review')
        self.assertEqual(deposit.evidence['solver_order_status'], 'deposit_event_not_uniquely_proven')

    def test_real_relay_order_maps_distinct_request_and_destination_delivery(self):
        deposit = next(s for s in self.example('0xffa84e') if s.behavior == 'INTENT_DEPOSIT')
        document = json.loads((ROOT / 'data/relay_order_evidence_3ccc6f52.json').read_text())
        evidence = relay_delivery_evidence(document, deposit)[0]
        self.assertNotEqual(evidence['request_id'], evidence['order_id'])
        self.assertEqual(evidence['source_tx_hash'], deposit.tx_hash)
        self.assertEqual(evidence['destination_chain_id'], '792703809')
        self.assertEqual(evidence['destination_amount_raw'], '173879072')
        self.assertEqual(evidence['destination_chain_status'],
                         'api_reported_not_independently_rechecked')
        store = Store(':memory:')
        store.put(deposit)
        self.assertEqual(store.record_solver_delivery(
            evidence['order_id'], deposit.wallet, evidence['destination_tx_hash'], evidence),
            'order_delivery_linked')
        self.assertEqual(len(store.solver_order(evidence['order_id'])), 2)
        store.close()

    def test_relay_order_id_cannot_be_used_as_request_id(self):
        deposit = next(s for s in self.example('0xffa84e') if s.behavior == 'INTENT_DEPOSIT')
        document = json.loads((ROOT / 'data/relay_order_evidence_3ccc6f52.json').read_text())
        document['requests'][0]['id'] = deposit.evidence['order_id']
        with self.assertRaisesRegex(ValueError, 'conflated'):
            relay_delivery_evidence(document, deposit)

    def test_passive_credit_becomes_buy_only_with_exact_relay_order(self):
        candidate = passive_candidate()
        document = relay_buy_document()
        signal = relay_passive_buy(document, candidate)
        self.assertEqual((signal.behavior, signal.stage, signal.token_in,
                          signal.token_out, signal.amount_in_raw, signal.amount_out_raw),
                         ("BUY", "relay_buy_evidenced", R.USDG, TOKEN, "100", "90"))
        self.assertFalse(signal.copy_eligible)

        document["requests"][0]["recipient"] = B
        with self.assertRaisesRegex(ValueError, "not owned"):
            relay_passive_buy(document, candidate)

    def test_solana_usdc_relay_buy_uses_top_level_order_data_and_local_usdg(self):
        candidate = passive_candidate()
        document = relay_buy_document()
        request = document["requests"][0]
        origin = request["protocol"]["deposit"]["origin"]
        source_tx = "solana-signature-case-sensitive"
        source_user = "SolanaWalletCaseSensitive"
        origin.update({
            "chainId": 792703809,
            "currency": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            "depositor": source_user,
            "transactionId": source_tx,
        })
        request["user"] = source_user
        request["data"]["inTxs"] = [{
            "hash": source_tx, "chainId": 792703809, "status": "success",
        }]
        request["orderData"] = request["protocol"].pop("orderData")
        signal = relay_passive_buy(document, candidate)
        self.assertEqual(signal.token_in, R.USDG)
        self.assertEqual(signal.evidence["source_currency"], origin["currency"])
        self.assertEqual(signal.evidence["source_payer"], source_user)
        self.assertIn("operator_approved", signal.evidence["funding_normalization"])

    def test_passive_credit_without_relay_order_remains_candidate(self):
        candidate = passive_candidate()
        with self.assertRaisesRegex(ValueError, "not unique"):
            relay_passive_buy({"requests": []}, candidate)
        self.assertEqual(candidate.behavior, "EXTERNAL_DELIVERY_CANDIDATE")
        self.assertFalse(candidate.copy_eligible)

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

    def test_real_relay_passive_credit_has_exact_buy_order_attribution(self):
        summary = json.loads(
            (ROOT / 'data/relay_signal_evidence_2026-09-12.json').read_text())
        row = next(item for item in summary['samples']
                   if item['role'] == 'passive_credit_before_relay_order_association')
        candidate = Signal(
            row['tx_hash'], row['wallet'], 'third_party', 'INCOMING_TRANSFER',
            'incoming', R.RELAY_ROUTER, '0xcd6e13f7', stage='needs_review',
            execution_status='success', execution_success=True,
            evidence={'wallet_erc20_deltas_raw': {
                row['credited_token']: row['credited_amount_raw']}})
        document = json.loads(
            (ROOT / 'data/relay_passive_buy_evidence_2026-09-12.json').read_text())
        buy = relay_passive_buy(document, candidate)
        self.assertEqual((buy.behavior, buy.stage, buy.token_in, buy.token_out,
                          buy.amount_in_raw, buy.amount_out_raw), (
                              'BUY', 'relay_buy_evidenced', R.NATIVE,
                              row['credited_token'], '196852887764874',
                              row['credited_amount_raw']))
        self.assertNotEqual(buy.evidence['source_payer'], buy.wallet)
        self.assertFalse(buy.copy_eligible)

        forged = deepcopy(document)
        forged['requests'][0]['protocol']['orderData']['output']['payments'][0][
            'recipient'] = B
        with self.assertRaisesRegex(ValueError, 'uniquely authorize'):
            relay_passive_buy(forged, candidate)

    def test_real_relay_zero_x_sell_closes_token_debit_and_usdg_deposit(self):
        signals = self.example('0x23419e')
        trade = next(item for item in signals if item.behavior == 'SELL')
        deposit = next(item for item in signals if item.behavior == 'INTENT_DEPOSIT')
        self.assertEqual((trade.protocol, trade.stage), ('0x', 'relay_sell_evidenced'))
        self.assertEqual(trade.evidence['actual_input_debit_raw'], trade.amount_in_raw)
        self.assertEqual(trade.evidence['actual_output_deposit_raw'], deposit.amount_in_raw)
        self.assertEqual(trade.evidence['relay_deposit_order_id'],
                         deposit.evidence['order_id'])
        self.assertFalse(trade.copy_eligible)

    def test_real_unlinked_kyber_swap_stays_unknown(self):
        signal = next(item for item in self.example('0x714aaa')
                      if item.protocol == 'kyber')
        self.assertEqual(signal.behavior, 'UNKNOWN')
        self.assertIn('aggregator_call_not_linked_to_relay_sell', signal.reasons)
        self.assertFalse(signal.copy_eligible)

    def test_real_transfer_not_sell(self):
        self.assertEqual(self.example('0x5ef512')[0].behavior, 'TRANSFER')

    def test_real_liquidity_not_buy(self):
        signals = self.example('0x365af7')
        self.assertTrue(any(s.behavior == 'LIQUIDITY' for s in signals))
        self.assertFalse(any(s.behavior in TRADE_BEHAVIORS for s in signals))

    def test_real_all_signals_no_live_eligibility(self):
        for row in self.examples['examples']:
            self.assertTrue(all(not s.copy_eligible for s in self.example(row['transaction']['hash'])))


class QuoteTests(unittest.IsolatedAsyncioTestCase):
    async def test_decision_id_changes_when_relationship_snapshot_changes(self):
        store = Store(':memory:')
        signal = Signal(
            TXHASH, A, 'direct', 'TRANSFER', 'call', B, '0x',
            stage='receipt_success', execution_status='success')
        context = {A: {'relationship_id': '78'}}
        policy = QuotePolicy()
        first = PaperEngine(
            store, None, policy, 'snapshot-retry-v1', 'swap_evidenced',
            wallet_contexts=context, config_snapshot_hash='aa' * 32)
        second = PaperEngine(
            store, None, policy, 'snapshot-retry-v1', 'swap_evidenced',
            wallet_contexts=context, config_snapshot_hash='bb' * 32)

        first_decision = await first.propose_buy(
            signal, AmountRule('fixed', fixed_amount_raw='1'))
        second_decision = await second.propose_buy(
            signal, AmountRule('fixed', fixed_amount_raw='1'))

        self.assertNotEqual(first_decision.decision_id, second_decision.decision_id)
        self.assertEqual(
            store.paper_decision(first_decision.decision_id)['payload'][
                'config_snapshot_hash'], 'aa' * 32)
        self.assertEqual(
            store.paper_decision(second_decision.decision_id)['payload'][
                'config_snapshot_hash'], 'bb' * 32)
        store.close()

    async def test_evidenced_dynamic_meme_buy_reaches_budget_reservation(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-dynamic', 'test')
        store.configure_paper_budget(A, 'USDG', '1000')
        signal = Signal(
            TXHASH, A, 'direct', 'BUY', 'call', R.V3_ROUTER, '0x',
            stage='swap_evidenced', execution_status='success', protocol='v3',
            token_in=R.USDG, token_out=TOKEN, exact_in=True,
            evidence={
                'actual_input_debit_raw': '1000',
                'actual_output_credit_raw': '2000',
                'hops': [{'token_in': R.USDG, 'token_out': TOKEN, 'fee': 3000}],
            })

        class Quoter:
            async def quote_with_reference(self, source, amount):
                quote = Quote('v3', R.V3_QUOTER, 1, '0x' + 'aa' * 32, 100.0,
                              R.USDG, TOKEN, amount, '198')
                reference = Quote('v3', R.V3_QUOTER, 1, '0x' + 'aa' * 32, 100.0,
                                  R.USDG, TOKEN, '10', '20')
                return quote, reference, '100'

        engine = PaperEngine(
            store, Quoter(), QuotePolicy(max_adverse_deviation_bps=200,
                                         max_price_impact_bps=200,
                                         max_gas_cost_wei='40000000'),
            'dynamic-target-v1', 'swap_evidenced', frozenset({'v3'}),
            frozenset({R.NATIVE, R.WETH, R.USDG}), frozenset(),
        )
        decision = await engine.propose_buy(
            signal, AmountRule('proportional', ratio_ppm=100_000), now=101)
        self.assertTrue(decision.accepted)
        proposal = store.paper_proposal(decision.proposal_id)
        self.assertEqual((proposal['input_asset'], proposal['output_asset'],
                          proposal['amount_in_raw'], proposal['status']),
                         (R.USDG, TOKEN, '100', 'reserved'))
        self.assertEqual(store.paper_budget(A, 'USDG')['reserved_raw'], '100')
        store.close()

    async def test_relay_sell_exits_to_lot_principal_and_restores_that_cap(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-relay', 'operator_started')
        store.configure_paper_budget(A, 'ETH_WETH', '1000')
        store.reserve_paper_proposal({
            'proposal_id': 'relay-buy-p', 'source_event_id': 'relay-buy-event',
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'relay_buy_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.NATIVE, 'output_asset': TOKEN,
            'budget_bucket': 'ETH_WETH', 'amount_in_raw': '400',
            'attribution': {
                'smart_wallet': A, 'source_event_id': 'relay-buy-event',
                'source_amount_out_raw': '2000',
            },
        })
        store.fill_paper_buy('relay-buy-p', {
            'order_id': 'relay-buy-o', 'fill_id': 'relay-buy-f', 'lot_id': 'relay-lot',
            'amount_out_raw': '2000', 'fee_asset': R.NATIVE, 'fee_amount_raw': '0',
            'gas_cost_wei': '20000000',
            'quote_observed_at': '2026-09-12T00:00:00Z',
            'filled_at': '2026-09-12T00:00:01Z',
        })
        sell = Signal(
            '0x' + '77' * 32, A, 'third_party', 'SELL', 'call', R.RELAY_ROUTER,
            '0x', stage='relay_sell_evidenced', execution_status='success',
            token_in=TOKEN, token_out=R.USDG, protocol='relay_solver',
            evidence={'actual_input_debit_raw': '1000',
                      'actual_output_credit_raw': '250'})
        routes = ({'protocol': 'v3', 'assets': [R.NATIVE, TOKEN], 'fees': [500]},)

        class Quoter:
            async def quote_with_reference(self, source, amount):
                self.source = source
                return (Quote('v3', R.V3_QUOTER, 1, '0x' + 'aa' * 32, 100.0,
                              TOKEN, R.NATIVE, amount, '240'),
                        Quote('v3', R.V3_QUOTER, 1, '0x' + 'aa' * 32, 100.0,
                              TOKEN, R.NATIVE, '100', '24'), '100')

        quoter = Quoter()
        policy = QuotePolicy(max_price_impact_bps=200, max_gas_cost_wei='40000000')
        engine = PaperEngine(store, quoter, policy, 'paper-v1',
                             'relay_sell_evidenced', execution_routes=routes)
        decision = await engine.propose_sell(
            sell, AmountRule('proportional', ratio_ppm=1_000_000), now=101)
        self.assertTrue(decision.accepted)
        proposal = store.paper_proposal(decision.proposal_id)
        self.assertEqual((proposal['output_asset'], proposal['budget_bucket']),
                         (R.NATIVE, 'ETH_WETH'))
        self.assertEqual((quoter.source.token_out, quoter.source.protocol),
                         (R.NATIVE, 'v3'))
        execution = await PaperExecutor(
            store, quoter, policy, routes).execute(sell, decision.proposal_id, now=102)
        self.assertEqual(execution.status, 'filled')
        self.assertEqual(store.paper_budget(A, 'ETH_WETH')['invested_raw'], '200')
        pnl = store.paper_realized_pnl(execution.fill_id)[0]
        self.assertEqual((pnl['principal_asset'], pnl['principal_released_raw'],
                          pnl['proceeds_raw'], pnl['realized_pnl_raw']),
                         (R.NATIVE, '200', '240', '40'))
        store.close()

    async def test_relay_sell_reuses_verified_route_from_attributed_buy_lot(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-relay-route', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '10000')
        buy_source = Signal(
            TXHASH, A, 'third_party', 'BUY', 'incoming', R.RELAY_ROUTER, '0x',
            stage='relay_buy_evidenced', execution_status='success',
            token_in=R.USDG, token_out=TOKEN, protocol='relay_solver',
            evidence={
                'actual_input_debit_raw': '1500',
                'actual_output_credit_raw': '3000',
                'local_execution_route': {
                    'protocol': 'v3', 'assets': [R.USDG, TOKEN],
                    'fees': [3000], 'verified_pool': '0x' + '44' * 20,
                    'verified_block_number': '10',
                },
            })
        store.put(buy_source)
        store.reserve_paper_proposal({
            'proposal_id': 'relay-route-buy-p',
            'source_event_id': buy_source.event_id,
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.USDG, 'output_asset': TOKEN,
            'budget_bucket': 'USDG', 'amount_in_raw': '1000',
            'attribution': {
                'smart_wallet': A, 'source_event_id': buy_source.event_id,
                'source_amount_out_raw': '3000',
            },
        })
        store.fill_paper_buy('relay-route-buy-p', {
            'order_id': 'relay-route-buy-o', 'fill_id': 'relay-route-buy-f',
            'lot_id': 'relay-route-lot', 'amount_out_raw': '2000',
            'fee_asset': R.USDG, 'fee_amount_raw': '0', 'gas_cost_wei': '1',
            'quote_observed_at': '2026-09-13T00:00:00Z',
            'filled_at': '2026-09-13T00:00:01Z',
        })
        sell = Signal(
            '0x' + '77' * 32, A, 'third_party', 'SELL', 'call', R.RELAY_ROUTER,
            '0x', stage='relay_sell_evidenced', execution_status='success',
            token_in=TOKEN, token_out=R.USDG, protocol='0x',
            evidence={'actual_input_debit_raw': '3000',
                      'actual_output_credit_raw': '1500'})

        class Quoter:
            async def quote_with_reference(self, source, amount):
                self.source, self.amount = source, amount
                return (Quote('v3', R.V3_QUOTER, 10, '0x' + 'ab' * 32, 100.0,
                              TOKEN, R.USDG, amount, '1000'),
                        Quote('v3', R.V3_QUOTER, 10, '0x' + 'ab' * 32, 100.0,
                              TOKEN, R.USDG, '100', '50'), '100')

        quoter = Quoter()
        decision = await PaperEngine(
            store, quoter, QuotePolicy(max_price_impact_bps=200,
                                       max_gas_cost_wei='30000000'),
            'paper-v1', 'evidenced', execution_routes=()).propose_sell(
                sell, AmountRule('proportional', ratio_ppm=1_000_000), now=101)
        self.assertTrue(decision.accepted)
        self.assertEqual((quoter.source.protocol, quoter.source.token_in,
                          quoter.source.token_out, quoter.amount),
                         ('v3', TOKEN, R.USDG, '2000'))
        self.assertEqual(sell.evidence['local_execution_route']['verified_pool'],
                         '0x' + '44' * 20)
        store.close()

    async def test_relay_buy_uses_configured_local_v3_route_for_paper_quote(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-relay', 'operator_started')
        store.configure_paper_budget(A, 'ETH_WETH', '1000')
        signal = Signal(
            TXHASH, A, 'third_party', 'BUY', 'incoming', R.RELAY_ROUTER,
            '0xcd6e13f7', stage='relay_buy_evidenced', execution_status='success',
            token_in=R.NATIVE, token_out=TOKEN, protocol='relay_solver',
            evidence={'actual_input_debit_raw': '1000',
                      'actual_output_credit_raw': '2000'})
        routes = ({'protocol': 'v3', 'assets': [R.NATIVE, TOKEN], 'fees': [500]},)

        class Quoter:
            async def quote_with_reference(self, source, amount):
                self.source = source
                quote = Quote('v3', R.V3_QUOTER, 1, '0x' + 'aa' * 32, 100.0,
                              R.NATIVE, TOKEN, amount, '198')
                reference = Quote('v3', R.V3_QUOTER, 1, '0x' + 'aa' * 32, 100.0,
                                  R.NATIVE, TOKEN, '10', '20')
                return quote, reference, '100'

        quoter = Quoter()
        engine = PaperEngine(
            store, quoter, QuotePolicy(max_adverse_deviation_bps=200,
                                       max_price_impact_bps=200,
                                       max_gas_cost_wei='40000000'),
            'paper-relay-v1', 'relay_buy_evidenced',
            frozenset({'relay_solver'}), frozenset({R.NATIVE, TOKEN}),
            frozenset({signal_route_key(signal)}), execution_routes=routes)
        decision = await engine.propose_buy(
            signal, AmountRule('proportional', ratio_ppm=100_000), now=101)
        self.assertTrue(decision.accepted)
        self.assertEqual((quoter.source.protocol, quoter.source.evidence['hops']),
                         ('v3', [{'token_in': R.NATIVE,
                                  'token_out': TOKEN, 'fee': 500}]))
        proposal = store.paper_proposal(decision.proposal_id)
        self.assertEqual(proposal['quote']['execution_signal']['protocol'], 'v3')
        self.assertEqual(proposal['attribution']['source_stage'],
                         'relay_buy_evidenced')
        self.assertFalse(signal.copy_eligible)
        store.close()

    async def test_relay_buy_discovers_and_persists_bounded_v3_route_for_planned_amount(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-relay-discovery', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        signal = Signal(
            TXHASH, A, 'third_party', 'BUY', 'incoming', R.RELAY_ROUTER,
            '0xcd6e13f7', stage='relay_buy_evidenced', execution_status='success',
            execution_success=True, token_in=R.USDG, token_out=TOKEN,
            protocol='relay_solver', evidence={
                'actual_input_debit_raw': '500',
                'actual_output_credit_raw': '1000',
            })
        store.put(signal)

        class Quoter:
            async def discover_v3_route(self, source, amount):
                self.discovery = (source.event_id, amount)
                return {
                    'protocol': 'v3', 'assets': [R.USDG, TOKEN], 'fees': [500],
                    'verified_pool': '0x' + '44' * 20,
                    'verified_block_number': '100',
                    'route_discovery': 'v3_factory_bounded_best_quote',
                }

            async def quote_with_reference(self, source, amount):
                self.source = source
                return (Quote('v3', R.V3_QUOTER, 100, '0x' + 'ab' * 32, 100.0,
                              R.USDG, TOKEN, amount, '220'),
                        Quote('v3', R.V3_QUOTER, 100, '0x' + 'ab' * 32, 100.0,
                              R.USDG, TOKEN, '1', '2'), '100')

        quoter = Quoter()
        engine = PaperEngine(
            store, quoter, QuotePolicy(max_adverse_deviation_bps=200,
                                       max_price_impact_bps=200,
                                       max_gas_cost_wei='40000000'),
            'paper-relay-discovery-v1', 'relay_buy_evidenced',
            frozenset({'relay_solver'}), frozenset({R.USDG}),
            execution_routes=())
        decision = await engine.propose_buy(
            signal, AmountRule('fixed', fixed_amount_raw='100'), now=101)
        self.assertTrue(decision.accepted)
        self.assertEqual(quoter.discovery, (signal.event_id, '100'))
        self.assertEqual((quoter.source.protocol, quoter.source.evidence['hops']),
                         ('v3', [{'token_in': R.USDG,
                                  'token_out': TOKEN, 'fee': 500}]))
        persisted = store.signal(signal.event_id)
        self.assertEqual(persisted.evidence['local_execution_route']['verified_pool'],
                         '0x' + '44' * 20)
        proposal = store.paper_proposal(decision.proposal_id)
        self.assertEqual(proposal['attribution']['local_execution_route']['fees'], [500])
        store.close()

    async def test_live_quoter_discovers_best_verified_direct_v3_standard_fee_pool(self):
        pool500 = '0x' + '44' * 20
        pool3000 = '0x' + '55' * 20
        pools = {500: pool500, 3000: pool3000}
        calls = []

        class Rpc:
            async def call(self, method, params=None):
                calls.append((method, params))
                if method == 'eth_getBlockByNumber':
                    return {'number': '0x64', 'hash': '0x' + 'ab' * 32}
                if method == 'eth_getCode':
                    return '0x01'
                target = params[0]['to']
                data = bytes.fromhex(params[0]['data'][2:])
                if target == R.V3_FACTORY:
                    fee = decode(['address', 'address', 'uint24'], data[4:])[2]
                    if fee == 100:
                        return '0x'
                    return '0x' + encode(
                        ['address'], [pools.get(fee, R.NATIVE)]).hex()
                if target in pools.values():
                    if data[:4] == selector('token0()'):
                        return '0x' + encode(['address'], [TOKEN]).hex()
                    if data[:4] == selector('token1()'):
                        return '0x' + encode(['address'], [R.USDG]).hex()
                    if data[:4] == selector('fee()'):
                        fee = next(key for key, value in pools.items()
                                   if value == target)
                        return '0x' + encode(['uint24'], [fee]).hex()
                if target == R.V3_QUOTER:
                    path, amount = decode(['bytes', 'uint256'], data[4:])
                    fee = int.from_bytes(path[20:23], 'big')
                    outputs = {500: 220, 3000: 210}
                    self.amount = amount
                    return '0x' + encode(['uint256'], [outputs[fee]]).hex()
                raise AssertionError((method, target, data[:4].hex()))

        rpc = Rpc()
        signal = Signal(
            TXHASH, A, 'third_party', 'BUY', 'incoming', R.RELAY_ROUTER, '0x',
            stage='relay_buy_evidenced', execution_status='success',
            execution_success=True, token_in=R.USDG, token_out=TOKEN,
            protocol='relay_solver')
        route = await LiveQuoter(rpc).discover_v3_route(signal, '100')
        self.assertEqual((route['fees'], route['verified_pool'],
                          route['route_discovery_amount_out_raw']),
                         ([500], pool500, '220'))
        self.assertEqual(route['evaluated_fee_tiers'], [100, 500, 3000, 10000])
        self.assertEqual(rpc.amount, 100)
        block_calls = [params for method, params in calls
                       if method in {'eth_call', 'eth_getCode'}]
        self.assertTrue(all(params[-1] == '0x64' for params in block_calls))

    async def test_live_quoter_pins_v2_v3_v4_calls_to_observed_block(self):
        calls = []

        class Rpc:
            async def call(self, method, params=None):
                calls.append((method, params))
                if method == 'eth_getBlockByNumber':
                    return {'number': '0x64', 'hash': '0x' + 'ab' * 32}
                target = params[0]['to']
                if target == R.V2_ROUTER:
                    return '0x' + encode(['uint256[]'], [[100, 210]]).hex()
                if target == R.V3_QUOTER:
                    return '0x' + encode(
                        ['uint256', 'uint160[]', 'uint32[]', 'uint256'],
                        [220, [1], [2], 30000]).hex()
                if target == R.V4_QUOTER:
                    return '0x' + encode(['uint256', 'uint256'], [230, 40000]).hex()
                raise AssertionError(target)

        quoter = LiveQuoter(Rpc())
        base = dict(stage='swap_evidenced', execution_status='success',
                    token_in=R.USDG, token_out=TOKEN,
                    evidence={'actual_input_debit_raw': '100',
                              'actual_output_credit_raw': '200'})
        v2 = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                    protocol='v2', **deepcopy(base))
        v2.evidence['route'] = [R.USDG, TOKEN]
        v3 = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V3_ROUTER, '0x',
                    protocol='v3', **deepcopy(base))
        v3.evidence['hops'] = [
            {'token_in': R.USDG, 'token_out': TOKEN, 'fee': 500}]
        v4 = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.UNIVERSAL_ROUTER, '0x',
                    protocol='v4', **deepcopy(base))
        v4.evidence.update({'pool_key': [TOKEN, R.USDG, 3000, 60, R.NATIVE],
                            'hook_data': '0x'})
        middle = '0x' + '44' * 20
        v4_multi = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.UNIVERSAL_ROUTER, '0x',
                          protocol='v4', **deepcopy(base))
        v4_multi.evidence['v4_hops'] = [
            {'token_in': R.USDG, 'token_out': middle,
             'pool_key': [middle, R.USDG, 500, 10, R.NATIVE], 'hook_data': '0x12'},
            {'token_in': middle, 'token_out': TOKEN,
             'pool_key': [TOKEN, middle, 3000, 60, R.NATIVE], 'hook_data': '0x'},
        ]
        with patch('smart_money.quotes.time.time', return_value=1234.5):
            quotes = [await quoter.quote_exact_input(signal, '100')
                      for signal in (v2, v3, v4, v4_multi)]
        self.assertEqual([quote.amount_out_raw for quote in quotes],
                         ['210', '220', '230', '230'])
        self.assertEqual(quotes[2].gas_estimate_raw, '40000')
        self.assertTrue(all(quote.block_number == 100 and quote.observed_at == 1234.5
                            for quote in quotes))
        quote_calls = [params for method, params in calls if method == 'eth_call']
        self.assertTrue(all(params[1] == '0x64' for params in quote_calls))
        multi_data = bytes.fromhex(quote_calls[-1][0]['data'][2:])
        signature = 'quoteExactInput((address,(address,uint24,int24,address,bytes)[],uint128))'
        self.assertEqual(multi_data[:4], selector(signature))
        decoded = decode(['(address,(address,uint24,int24,address,bytes)[],uint128)'],
                         multi_data[4:])[0]
        self.assertEqual((decoded[0], decoded[1][0][0], decoded[1][1][0], decoded[2]),
                         (R.USDG, middle, TOKEN, 100))

    def test_quote_policy_rejects_expiry_assets_and_adverse_price_move(self):
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                        stage='swap_evidenced', token_in=R.USDG, token_out=TOKEN,
                        evidence={'actual_input_debit_raw': '100',
                                  'actual_output_credit_raw': '200'})
        quote = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                      R.USDG, TOKEN, '100', '190')
        policy = QuotePolicy(max_age_seconds=2, max_adverse_deviation_bps=400)
        self.assertEqual(validate_quote(signal, quote, policy, now=101)[1],
                         'adverse_price_deviation_exceeded')
        self.assertEqual(validate_quote(signal, quote, QuotePolicy(
            max_age_seconds=2, max_adverse_deviation_bps=600), now=103)[1],
                         'quote_missing_or_expired')
        wrong = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                      R.WETH, TOKEN, '100', '200')
        self.assertEqual(validate_quote(signal, wrong, policy, now=100)[1],
                         'quote_asset_mismatch')

    def test_feed_intent_uses_encoded_exact_in_limit_not_future_receipt_price(self):
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                        stage='intent', fresh=True, exact_in=True,
                        token_in=R.USDG, token_out=TOKEN,
                        amount_in_raw='1000', amount_limit_raw='1900')
        good = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                     R.USDG, TOKEN, '100', '191')
        allowed, reason, evidence = validate_quote(signal, good, QuotePolicy(), now=101)
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        self.assertEqual(evidence['source_price_basis'], 'intent_exact_in_minimum')
        self.assertEqual(evidence['scaled_source_minimum_out_raw'], '190')
        bad = replace(good, amount_out_raw='189')
        self.assertEqual(validate_quote(signal, bad, QuotePolicy(), now=101)[1],
                         'intent_price_limit_not_met')

    async def test_shadow_engine_records_eligibility_without_budget_reservation(self):
        store = Store(':memory:')
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                        stage='intent', fresh=True, exact_in=True, protocol='v2',
                        token_in=R.USDG, token_out=TOKEN,
                        amount_in_raw='1000', amount_limit_raw='1900')

        class Quoter:
            async def quote_with_reference(self, source, amount):
                quote = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                              R.USDG, TOKEN, amount, '191')
                reference = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                                  R.USDG, TOKEN, '1', '2')
                return quote, reference, '100'

        engine = PaperEngine(store, Quoter(), QuotePolicy(
            max_gas_cost_wei='30000000'), 'paper-v1', 'feed_intent',
            frozenset({'v2'}), frozenset({R.USDG, TOKEN}), shadow_only=True)
        decision = await engine.propose_buy(
            signal, AmountRule('fixed', fixed_amount_raw='100'), now=101)
        self.assertTrue(decision.accepted)
        self.assertIsNone(decision.proposal_id)
        self.assertTrue(store.paper_decision(decision.decision_id)['payload']['shadow_only'])
        self.assertEqual(store.connection.execute(
            'SELECT COUNT(*) FROM paper_proposals').fetchone()[0], 0)
        store.close()

    def test_quote_assessment_enforces_impact_gas_and_slippage_floor(self):
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                        stage='swap_evidenced', token_in=R.USDG, token_out=TOKEN,
                        evidence={'actual_input_debit_raw': '100',
                                  'actual_output_credit_raw': '200'})
        quote = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                      R.USDG, TOKEN, '100', '190')
        reference = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                          R.USDG, TOKEN, '10', '20')
        policy = QuotePolicy(max_adverse_deviation_bps=600, max_price_impact_bps=600,
                             max_slippage_bps=300, max_gas_cost_wei='30000000')
        allowed, reason, evidence = assess_quote(
            signal, quote, reference, policy, '100', now=101)
        self.assertTrue(allowed)
        self.assertIsNone(reason)
        self.assertEqual(evidence['estimated_price_impact_bps'], '500')
        self.assertEqual(evidence['estimated_gas_cost_wei'], '20000000')
        self.assertEqual(evidence['minimum_amount_out_raw'], '184')
        too_costly = QuotePolicy(max_adverse_deviation_bps=600,
                                 max_price_impact_bps=600,
                                 max_gas_cost_wei='19999999')
        self.assertEqual(assess_quote(
            signal, quote, reference, too_costly, '100', now=101)[1],
            'gas_cost_limit_exceeded')

    async def test_paper_engine_quotes_assesses_and_atomically_reserves(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                        stage='swap_evidenced', execution_status='success',
                        token_in=R.USDG, token_out=TOKEN, protocol='v2',
                        evidence={'actual_input_debit_raw': '1000',
                                  'actual_output_credit_raw': '2000',
                                  'route': [R.USDG, TOKEN]})

        class Quoter:
            async def quote_with_reference(self, source, amount):
                self.amount = amount
                quote = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                              R.USDG, TOKEN, amount, '198')
                reference = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                                  R.USDG, TOKEN, '10', '20')
                return quote, reference, '100'

        quoter = Quoter()
        engine = PaperEngine(store, quoter, QuotePolicy(
            max_adverse_deviation_bps=200, max_price_impact_bps=200,
            max_gas_cost_wei='30000000'), 'paper-v1')
        decision = await engine.propose_buy(
            signal, AmountRule('proportional', ratio_ppm=100_000), now=101)
        self.assertTrue(decision.accepted)
        self.assertEqual(quoter.amount, '100')
        self.assertEqual(store.paper_budget(A, 'USDG')['reserved_raw'], '100')
        persisted = store.paper_decision(decision.decision_id)
        self.assertTrue(persisted['accepted'])
        self.assertEqual(persisted['payload']['proposal_id'], decision.proposal_id)
        duplicate = await engine.propose_buy(
            signal, AmountRule('proportional', ratio_ppm=100_000), now=101)
        self.assertTrue(duplicate.accepted)
        self.assertEqual(store.paper_budget(A, 'USDG')['reserved_raw'], '100')
        store.close()

    async def test_paper_executor_requotes_and_records_fill_gas_and_lot(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                        stage='swap_evidenced', execution_status='success',
                        token_in=R.USDG, token_out=TOKEN, protocol='v2',
                        evidence={'actual_input_debit_raw': '1000',
                                  'actual_output_credit_raw': '2000',
                                  'route': [R.USDG, TOKEN]})

        class Quoter:
            calls = 0

            async def quote_with_reference(self, source, amount):
                self.calls += 1
                quote = Quote('v2', R.V2_ROUTER, self.calls, '0x' + 'aa' * 32,
                              100.0, R.USDG, TOKEN, amount, '198')
                reference = Quote('v2', R.V2_ROUTER, self.calls, '0x' + 'aa' * 32,
                                  100.0, R.USDG, TOKEN, '1', '2')
                return quote, reference, '100'

        quoter = Quoter()
        policy = QuotePolicy(max_adverse_deviation_bps=200,
                             max_price_impact_bps=200,
                             max_gas_cost_wei='30000000')
        decision = await PaperEngine(
            store, quoter, policy, 'paper-v1', wallet_labels={A: 'wallet-alpha'},
            wallet_contexts={A: {'follower_wallet': B, 'relationship_id': '42'}},
            config_snapshot_hash='ab' * 32,
        ).propose_buy(signal, AmountRule('fixed', fixed_amount_raw='100'), now=101)
        execution = await PaperExecutor(store, quoter, policy).execute(
            signal, decision.proposal_id, now=101)
        self.assertEqual((quoter.calls, execution.status), (2, 'filled'))
        fill = store.connection.execute(
            'SELECT amount_out_raw,gas_cost_wei FROM paper_fills WHERE fill_id=?',
            (execution.fill_id,)).fetchone()
        self.assertEqual(fill, ('198', '20000000'))
        self.assertEqual(store.paper_budget(A, 'USDG')['invested_raw'], '100')
        trade = store.paper_trades()[0]
        self.assertEqual(trade['attribution']['smart_wallet_label'], 'wallet-alpha')
        self.assertEqual(trade['attribution']['follower_wallet'], B)
        self.assertEqual(trade['attribution']['relationship_id'], '42')
        self.assertEqual(trade['attribution']['config_snapshot_hash'], 'ab' * 32)
        self.assertEqual(trade['decision']['follower_wallet'], B)
        self.assertEqual(trade['source_signal_at_decision']['event_id'], signal.event_id)
        self.assertEqual(trade['source_signal_at_decision']['evidence'][
            'actual_input_debit_raw'], '1000')
        self.assertIsNotNone(trade['decision_created_at'])
        self.assertIsNotNone(trade['proposal_created_at'])
        self.assertIsNotNone(store.paper_position(
            PaperExecutor._id(decision.proposal_id, 'lot')))
        store.close()

    async def test_paper_position_mark_reverses_route_and_keeps_gas_separate(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        source = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                        stage='swap_evidenced', execution_status='success',
                        token_in=R.USDG, token_out=TOKEN, protocol='v2',
                        evidence={'actual_input_debit_raw': '250',
                                  'actual_output_credit_raw': '5000',
                                  'route': [R.USDG, TOKEN]})
        store.put(source)
        store.reserve_paper_proposal({
            'proposal_id': 'buy-p', 'source_event_id': source.event_id,
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.USDG, 'output_asset': TOKEN,
            'budget_bucket': 'USDG', 'amount_in_raw': '250',
            'attribution': {'smart_wallet': A},
        })
        store.fill_paper_buy('buy-p', {
            'order_id': 'buy-o', 'fill_id': 'buy-f', 'lot_id': 'lot1',
            'amount_out_raw': '5000', 'fee_asset': R.USDG, 'fee_amount_raw': '0',
            'gas_cost_wei': '20000000',
            'quote_observed_at': '2026-09-12T00:00:00Z',
            'filled_at': '2026-09-12T00:00:01Z',
        })
        reversed_signal = reverse_quote_signal(source, R.USDG)
        self.assertEqual(reversed_signal.evidence['route'], [TOKEN, R.USDG])

        class Quoter:
            async def quote_with_reference(self, signal, amount):
                self.signal = signal
                return (Quote('v2', R.V2_ROUTER, 123, '0x' + 'ab' * 32, 100.0,
                              TOKEN, R.USDG, amount, '300'),
                        Quote('v2', R.V2_ROUTER, 123, '0x' + 'ab' * 32, 100.0,
                              TOKEN, R.USDG, '50', '3'), '100')

        quoter = Quoter()
        mark = await PaperValuator(store, quoter, QuotePolicy(
            max_gas_cost_wei='30000000')).mark('lot1', source, now=101)
        self.assertEqual((mark.gross_value_raw, mark.unrealized_pnl_raw,
                          mark.gas_cost_wei), ('300', '50', '20000000'))
        self.assertEqual(quoter.signal.evidence['route'], [TOKEN, R.USDG])
        persisted = store.paper_position_marks('lot1')[0]
        self.assertEqual((persisted['principal_asset'], persisted['unrealized_pnl_raw']),
                         (R.USDG, '50'))
        trade = store.paper_trades()[0]
        self.assertEqual((trade['smart_wallet'], trade['source_event_id'],
                          trade['source_tx_hash'], trade['trigger_mode'],
                          trade['strategy_version'], trade['source_canonical_status']),
                         (A, source.event_id, TXHASH, 'swap_evidenced', 'paper-v1',
                          'unconfirmed'))
        self.assertEqual((trade['amount_in_raw'], trade['amount_out_raw'],
                          trade['gas_cost_wei'], trade['paper_only']),
                         ('250', '5000', '20000000', True))
        self.assertEqual(trade['position_lots'][0]['lot_id'], 'lot1')
        self.assertEqual(trade['position_lots'][0]['latest_mark']['mark_id'], mark.mark_id)
        self.assertFalse(store.record_paper_position_mark({
            'mark_id': mark.mark_id, 'lot_id': 'lot1', 'principal_asset': R.USDG,
            'token_amount_raw': '5000', 'gross_value_raw': '300',
            'principal_remaining_raw': '250', 'unrealized_pnl_raw': '50',
            'gas_cost_wei': '20000000', 'block_number': 123,
            'block_hash': '0x' + 'ab' * 32, 'quote_source': R.V2_ROUTER,
            'quote_observed_at': '2026-09-12T00:00:00+00:00', 'risk': {},
        }))
        store.close()

    async def test_relay_position_mark_reverses_its_local_execution_route(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('relay-mark', 'operator_started')
        store.configure_paper_budget(A, 'ETH_WETH', '1000')
        source = Signal(
            TXHASH, A, 'third_party', 'BUY', 'incoming', R.RELAY_ROUTER, '0x',
            stage='relay_buy_evidenced', execution_status='success',
            token_in=R.NATIVE, token_out=TOKEN, protocol='relay_solver',
            evidence={'actual_input_debit_raw': '400',
                      'actual_output_credit_raw': '2000'})
        store.reserve_paper_proposal({
            'proposal_id': 'relay-mark-p', 'source_event_id': source.event_id,
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'relay_buy_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.NATIVE, 'output_asset': TOKEN,
            'budget_bucket': 'ETH_WETH', 'amount_in_raw': '400',
            'attribution': {'smart_wallet': A},
        })
        store.fill_paper_buy('relay-mark-p', {
            'order_id': 'relay-mark-o', 'fill_id': 'relay-mark-f',
            'lot_id': 'relay-mark-lot', 'amount_out_raw': '2000',
            'fee_asset': R.NATIVE, 'fee_amount_raw': '0', 'gas_cost_wei': '20000000',
            'quote_observed_at': '2026-09-12T00:00:00Z',
            'filled_at': '2026-09-12T00:00:01Z',
        })

        class Quoter:
            async def quote_with_reference(self, signal, amount):
                self.signal = signal
                return (Quote('v3', R.V3_QUOTER, 10, '0x' + 'ab' * 32, 100.0,
                              TOKEN, R.NATIVE, amount, '440'),
                        Quote('v3', R.V3_QUOTER, 10, '0x' + 'ab' * 32, 100.0,
                              TOKEN, R.NATIVE, '100', '22'), '100')

        quoter = Quoter()
        routes = ({'protocol': 'v3', 'assets': [R.NATIVE, TOKEN], 'fees': [500]},)
        mark = await PaperValuator(
            store, quoter, QuotePolicy(max_gas_cost_wei='30000000'), routes,
        ).mark('relay-mark-lot', source, now=101)
        self.assertEqual((quoter.signal.protocol, quoter.signal.token_in,
                          quoter.signal.token_out, mark.unrealized_pnl_raw),
                         ('v3', TOKEN, R.NATIVE, '40'))
        store.close()

    async def test_paper_engine_persists_quote_rejection_without_reserving(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                        stage='swap_evidenced', execution_status='success',
                        token_in=R.USDG, token_out=TOKEN, protocol='v2',
                        evidence={'actual_input_debit_raw': '1000',
                                  'actual_output_credit_raw': '2000'})

        class StaleQuoter:
            async def quote_with_reference(self, source, amount):
                quote = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 90.0,
                              R.USDG, TOKEN, amount, '200')
                reference = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 90.0,
                                  R.USDG, TOKEN, '10', '20')
                return quote, reference, '100'

        decision = await PaperEngine(
            store, StaleQuoter(), QuotePolicy(), 'paper-v1').propose_buy(
                signal, AmountRule('fixed', fixed_amount_raw='100'), now=100)
        self.assertFalse(decision.accepted)
        self.assertEqual(decision.reason, 'quote_missing_or_expired')
        self.assertEqual(store.paper_budget(A, 'USDG')['reserved_raw'], '0')
        self.assertEqual(store.paper_decision(decision.decision_id)['reason'],
                         'quote_missing_or_expired')
        store.close()

    async def test_paper_engine_sell_reserves_only_attributed_position(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        buy = {
            'proposal_id': 'buy-p', 'source_event_id': 'buy-event',
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.USDG, 'output_asset': TOKEN,
            'budget_bucket': 'USDG', 'amount_in_raw': '250',
            'attribution': {
                'smart_wallet': A, 'source_event_id': 'buy-event',
                'source_amount_out_raw': '5000',
            },
        }
        store.reserve_paper_proposal(buy)
        store.fill_paper_buy('buy-p', {
            'order_id': 'buy-o', 'fill_id': 'buy-f', 'lot_id': 'lot1',
            'amount_out_raw': '5000', 'fee_asset': R.USDG, 'fee_amount_raw': '0',
            'gas_cost_wei': '20000000',
            'quote_observed_at': '2026-09-12T00:00:00Z',
            'filled_at': '2026-09-12T00:00:01Z',
        })
        signal = Signal('0x' + '55' * 32, A, 'direct', 'SELL', 'call',
                        R.V2_ROUTER, '0x', stage='swap_evidenced',
                        execution_status='success', token_in=TOKEN, token_out=R.USDG,
                        protocol='v2', evidence={'actual_input_debit_raw': '2000',
                        'actual_output_credit_raw': '120', 'route': [TOKEN, R.USDG]})

        class Quoter:
            async def quote_with_reference(self, source, amount):
                self.amount = amount
                quote = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                              TOKEN, R.USDG, amount, '59')
                reference = Quote('v2', R.V2_ROUTER, 1, '0x' + 'aa' * 32, 100.0,
                                  TOKEN, R.USDG, '20', '1')
                return quote, reference, '100'

        quoter = Quoter()
        decision = await PaperEngine(store, quoter, QuotePolicy(
            max_adverse_deviation_bps=200, max_price_impact_bps=200,
            max_gas_cost_wei='30000000'), 'paper-v1').propose_sell(
                signal, AmountRule('proportional', ratio_ppm=500_000), now=101)
        self.assertTrue(decision.accepted)
        self.assertEqual(quoter.amount, '1000')
        reservation = store.connection.execute(
            "SELECT token_amount_raw FROM paper_position_reservations WHERE proposal_id=?",
            (decision.proposal_id,)).fetchone()
        self.assertEqual(reservation[0], '1000')
        store.close()


class SafetyTests(unittest.TestCase):
    @staticmethod
    def _okx_swap_document(**changes):
        document = {
            'code': '0',
            'data': [{
                'routerResult': {
                    'chainIndex': str(R.CHAIN_ID),
                    'fromTokenAmount': '100', 'toTokenAmount': '200',
                    'fromToken': {'tokenContractAddress': R.USDG},
                    'toToken': {'tokenContractAddress': TOKEN},
                },
                'tx': {
                    'from': B, 'to': R.OKX_ROUTER, 'value': '0',
                    'data': '0x12345678', 'gas': '300000', 'gasPrice': '10',
                    'minReceiveAmount': '190',
                },
            }],
        }
        for path, value in changes.items():
            target = document
            parts = path.split('__')
            for part in parts[:-1]:
                target = target[int(part)] if part.isdecimal() else target[part]
            target[parts[-1]] = value
        return document

    def test_okx_swap_response_is_strict_and_credentials_are_not_evidence(self):
        client = OkxSwapClient('api-key', 'secret-key', 'passphrase')
        document = self._okx_swap_document()
        swap = client._parse(
            document, R.USDG, TOKEN, '100', B, 100.0, 'ab' * 32,
            frozenset({R.OKX_ROUTER}))
        self.assertEqual((swap.to, swap.amount_out_raw,
                          swap.minimum_amount_out_raw, swap.value_raw),
                         (R.OKX_ROUTER, '200', '190', '0'))
        serialized = json.dumps(swap.public_evidence(), sort_keys=True)
        self.assertNotIn('api-key', serialized)
        self.assertNotIn('secret-key', serialized)
        self.assertNotIn('passphrase', serialized)

        for change in (
                {'data__0__tx__from': A},
                {'data__0__tx__to': A},
                {'data__0__routerResult__chainIndex': '1'},
                {'data__0__routerResult__fromTokenAmount': '101'},
                {'data__0__tx__minReceiveAmount': '201'}):
            with self.subTest(change=change), self.assertRaises(OkxError):
                client._parse(
                    self._okx_swap_document(**change), R.USDG, TOKEN, '100', B,
                    100.0, 'ab' * 32, frozenset({R.OKX_ROUTER}))

    def test_okx_client_requires_credentials_and_signs_exact_query(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
                ValueError, 'credentials are incomplete'):
            OkxSwapClient()
        client = OkxSwapClient('key', 'secret', 'phrase', 'project')
        headers = client._headers(
            '2026-09-13T00:00:00.000Z',
            '/api/v6/dex/aggregator/swap?chainIndex=4663&amount=100')
        expected = base64.b64encode(hmac.new(
            b'secret',
            b'2026-09-13T00:00:00.000ZGET/api/v6/dex/aggregator/swap?chainIndex=4663&amount=100',
            hashlib.sha256).digest()).decode()
        self.assertEqual(headers['OK-ACCESS-SIGN'], expected)
        self.assertEqual(headers['OK-ACCESS-PROJECT'], 'project')

    @staticmethod
    def _signed_execution_store(tx_hash='0x' + '91' * 32, path=':memory:'):
        store = Store(path)
        store.start_paper_budget_cycle('cycle-execution', 'test')
        store.configure_paper_budget(A, 'USDG', '1000')
        store.reserve_paper_proposal({
            'proposal_id': 'proposal-track', 'source_event_id': 'event-track',
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.USDG, 'output_asset': TOKEN,
            'budget_bucket': 'USDG', 'amount_in_raw': '100',
            'attribution': {'smart_wallet': A, 'follower_wallet': B,
                            'relationship_id': '42', 'config_snapshot_hash': 'ab' * 32},
        })
        reservation_id = 'reservation-track'
        store.reserve_execution_nonce(
            reservation_id, B, '42', 'proposal-track', R.CHAIN_ID, 7)
        transaction = {
            'chainId': R.CHAIN_ID, 'nonce': 7, 'to': R.V2_ROUTER,
            'value': 0, 'data': '0x12345678', 'gas': 220000,
            'maxFeePerGas': 120, 'maxPriorityFeePerGas': 2, 'type': 2,
        }
        store.record_execution_plan({
            'plan_id': 'plan-track', 'proposal_id': 'proposal-track',
            'follower_wallet': B, 'relationship_id': '42',
            'config_snapshot_hash': 'ab' * 32,
            'nonce_reservation_id': reservation_id, 'transaction': transaction,
            'unsigned_plan': {'public': 'only'},
        }, {'read_only': True})
        store.mark_execution_plan_signed(
            'plan-track', reservation_id, tx_hash, {'read_only': True})
        return store, transaction

    def test_execution_audit_reports_healthy_signed_state_without_secret_material(self):
        store, _ = self._signed_execution_store()
        audit = store.execution_audit()
        self.assertEqual(audit, {
            'plans': 1, 'prepared': 0, 'signed': 1, 'cancelled': 0,
            'attempts': 1, 'issues': [], 'healthy': True,
            'attempt_statuses': {
                'signed': 1, 'observed_pending': 0, 'confirmed': 0,
                'reverted': 0, 'replaced': 0, 'orphaned': 0,
            },
            'coverage': {
                'has_plans': True, 'has_signed_attempts': True,
                'has_rpc_observed_attempts': False,
                'has_canonical_receipts': False,
                'has_successful_confirmation': False,
            },
            'end_to_end_evidenced': False,
            'read_only': True, 'copy_eligible': False, 'live_trading': False,
        })
        serialized = json.dumps(audit).lower()
        self.assertNotIn('private_key', serialized)
        self.assertNotIn('raw_transaction', serialized)
        store.close()

    def test_execution_audit_does_not_treat_empty_ledger_as_end_to_end_evidence(self):
        store = Store(':memory:')
        audit = store.execution_audit()
        self.assertTrue(audit['healthy'])
        self.assertFalse(audit['coverage']['has_plans'])
        self.assertFalse(audit['end_to_end_evidenced'])
        store.close()

    def test_execution_track_cli_requires_explicit_proposal_and_bounded_replacement_args(self):
        parsed = cli_parser().parse_args([
            'execution-track', '--db', 'ledger.sqlite3',
            '--proposal-id', 'proposal-1', '--tx-hash', '0x' + '11' * 32,
            '--replaces-tx-hash', '0x' + '22' * 32,
        ])
        self.assertEqual(parsed.command, 'execution-track')
        self.assertEqual(parsed.proposal_id, 'proposal-1')
        self.assertEqual(parsed.db, 'ledger.sqlite3')

    def test_execution_track_cli_plumbing_is_read_only_and_persistent(self):
        async def scenario(path):
            tx_hash = '0x' + '91' * 32
            store, transaction = self._signed_execution_store(tx_hash, path)
            store.close()

            class Rpc:
                async def call(self, method, params=None):
                    if method == 'eth_chainId':
                        return hex(R.CHAIN_ID)
                    if method == 'eth_getTransactionByHash':
                        return {
                            'hash': tx_hash, 'from': B, 'to': transaction['to'],
                            'nonce': 7, 'chainId': R.CHAIN_ID, 'type': 2,
                            'gas': transaction['gas'], 'value': 0,
                            'input': transaction['data'], 'maxFeePerGas': 120,
                            'maxPriorityFeePerGas': 2,
                        }
                    if method == 'eth_getTransactionReceipt':
                        return None
                    raise AssertionError(method)

            args = SimpleNamespace(
                db=str(path), proposal_id='proposal-track',
                tx_hash=None, replaces_tx_hash=None,
            )
            output = io.StringIO()
            with patch('smart_money.cli.ReadOnlyRpc', return_value=Rpc()), \
                    redirect_stdout(output):
                await execution_track(args)
            result = json.loads(output.getvalue())
            self.assertEqual(result['status'], 'observed_pending')
            self.assertFalse(result['broadcast_performed'])
            self.assertFalse(result['copy_eligible'])
            self.assertNotIn('raw_transaction', output.getvalue())
            self.assertNotIn('private_key', output.getvalue())
            reopened = Store(path)
            self.assertEqual(
                reopened.execution_attempts('plan-track')[0]['status'],
                'observed_pending')
            reopened.close()

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(scenario(Path(folder) / 'track.sqlite3'))

    def test_execution_track_rejects_wrong_chain_and_missing_signed_plan(self):
        async def scenario(path):
            Store(path).close()
            args = SimpleNamespace(
                db=str(path), proposal_id='missing',
                tx_hash=None, replaces_tx_hash=None,
            )

            class Rpc:
                chain_id = R.CHAIN_ID + 1

                async def call(self, method, params=None):
                    if method == 'eth_chainId':
                        return hex(self.chain_id)
                    raise AssertionError(method)

            rpc = Rpc()
            with patch('smart_money.cli.ReadOnlyRpc', return_value=rpc):
                with self.assertRaisesRegex(ValueError, 'wrong chain'):
                    await execution_track(args)
                rpc.chain_id = R.CHAIN_ID
                with self.assertRaisesRegex(ValueError, 'signed execution plan'):
                    await execution_track(args)

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(scenario(Path(folder) / 'missing.sqlite3'))

    def test_execution_audit_survives_integrity_corruption(self):
        store, _ = self._signed_execution_store()
        store.connection.execute(
            "UPDATE execution_plans SET preflight_payload=? WHERE proposal_id=?",
            (json.dumps({'read_only': False}), 'proposal-track'))
        store.connection.commit()
        audit = store.execution_audit()
        self.assertFalse(audit['healthy'])
        self.assertEqual(audit['plans'], 1)
        self.assertEqual(audit['issues'], [{
            'proposal_id': 'proposal-track',
            'reason': 'execution_plan_integrity_or_decode_error',
            'error_type': 'ValueError',
        }])
        store.close()

    def test_execution_audit_detects_nonce_identity_mismatch(self):
        store, _ = self._signed_execution_store()
        store.connection.execute(
            "UPDATE execution_nonce_reservations SET nonce=8 WHERE proposal_id=?",
            ('proposal-track',))
        store.connection.commit()
        audit = store.execution_audit()
        self.assertFalse(audit['healthy'])
        self.assertIn({'proposal_id': 'proposal-track',
                       'reason': 'plan_nonce_identity_mismatch'}, audit['issues'])
        store.close()

    def test_execution_audit_detects_corrupt_attempt_identity_fee_and_parent(self):
        tx_hash = '0x' + '91' * 32
        store, transaction = self._signed_execution_store(tx_hash)
        payload = dict(transaction, nonce=8, maxFeePerGas=119)
        store.connection.execute(
            """UPDATE execution_attempts SET nonce=8,replaces_tx_hash=?,public_payload=?
               WHERE tx_hash=?""",
            (tx_hash, json.dumps(payload, sort_keys=True), tx_hash))
        store.connection.commit()
        audit = store.execution_audit()
        reasons = {issue['reason'] for issue in audit['issues']}
        self.assertFalse(audit['healthy'])
        self.assertTrue({
            'execution_attempt_identity_mismatch',
            'execution_attempt_fee_chain_invalid',
            'signed_attempt_has_replacement_parent',
        } <= reasons)
        store.close()

    def test_read_only_execution_tracker_records_pending_then_canonical_receipt(self):
        async def scenario():
            tx_hash, block_hash = '0x' + '91' * 32, '0x' + '92' * 32
            store, transaction = self._signed_execution_store(tx_hash)

            class Rpc:
                receipt = None

                async def call(self, method, params=None):
                    if method == 'eth_getTransactionByHash':
                        return {'hash': tx_hash, 'from': B, 'to': transaction['to'],
                                'nonce': '0x7', 'chainId': hex(R.CHAIN_ID), 'type': '0x2',
                                'gas': hex(transaction['gas']), 'value': '0x0',
                                'input': transaction['data'], 'maxFeePerGas': '0x78',
                                'maxPriorityFeePerGas': '0x2'}
                    if method == 'eth_getTransactionReceipt':
                        return self.receipt
                    return {'number': '0x64', 'hash': block_hash}

            rpc = Rpc()
            tracker = ReadOnlyExecutionTracker(store, rpc)
            pending = await tracker.observe('proposal-track')
            self.assertEqual(pending.status, 'observed_pending')
            self.assertEqual(store.execution_nonce_reservation(
                'proposal-track')['status'], 'broadcast')
            rpc.receipt = {'transactionHash': tx_hash, 'blockNumber': '0x64',
                           'blockHash': block_hash, 'status': '0x1'}
            confirmed = await tracker.observe('proposal-track')
            self.assertEqual((confirmed.status, confirmed.block_number), ('confirmed', 100))
            attempt = store.execution_attempts('plan-track')[0]
            self.assertEqual((attempt['status'], attempt['block_hash']),
                             ('confirmed', block_hash))
            self.assertEqual(store.execution_nonce_reservation(
                'proposal-track')['status'], 'confirmed')
            self.assertNotIn('raw_transaction', json.dumps(attempt))
            audit = store.execution_audit()
            self.assertTrue(audit['healthy'])
            self.assertEqual(audit['attempt_statuses']['confirmed'], 1)
            self.assertTrue(audit['coverage']['has_rpc_observed_attempts'])
            self.assertTrue(audit['coverage']['has_canonical_receipts'])
            self.assertTrue(audit['end_to_end_evidenced'])
            store.close()

        asyncio.run(scenario())

    def test_read_only_execution_tracker_replacement_requires_same_intent_and_higher_fee(self):
        async def scenario():
            original, replacement = '0x' + '91' * 32, '0x' + '93' * 32
            store, transaction = self._signed_execution_store(original)

            class Rpc:
                current_hash = original
                changed_data = False
                higher_fee = False

                async def call(self, method, params=None):
                    if method == 'eth_getTransactionByHash':
                        return {'hash': self.current_hash, 'from': B,
                                'to': transaction['to'], 'nonce': '0x7',
                                'chainId': hex(R.CHAIN_ID), 'type': '0x2',
                                'gas': hex(transaction['gas']), 'value': '0x0',
                                'input': '0xdeadbeef' if self.changed_data else transaction['data'],
                                'maxFeePerGas': '0x79' if self.higher_fee else '0x78',
                                'maxPriorityFeePerGas': '0x2'}
                    return None

            rpc = Rpc()
            tracker = ReadOnlyExecutionTracker(store, rpc)
            await tracker.observe('proposal-track')
            rpc.current_hash = replacement
            with self.assertRaisesRegex(ValueError, 'fees were not increased'):
                await tracker.observe('proposal-track', replacement, original)
            rpc.higher_fee, rpc.changed_data = True, True
            with self.assertRaisesRegex(ValueError, 'does not match execution intent'):
                await tracker.observe('proposal-track', replacement, original)
            rpc.changed_data = False
            observed = await tracker.observe('proposal-track', replacement, original)
            self.assertEqual(observed.status, 'observed_pending')
            attempts = store.execution_attempts('plan-track')
            self.assertEqual([(row['tx_hash'], row['status']) for row in attempts],
                             [(original, 'replaced'), (replacement, 'observed_pending')])
            self.assertTrue(store.execution_audit()['healthy'])
            store.close()

        asyncio.run(scenario())

    def test_read_only_execution_tracker_records_revert_and_orphan(self):
        async def scenario(receipt_status, canonical_hash):
            tx_hash, receipt_hash = '0x' + '91' * 32, '0x' + '92' * 32
            store, transaction = self._signed_execution_store(tx_hash)

            class Rpc:
                async def call(self, method, params=None):
                    if method == 'eth_getTransactionByHash':
                        return {'hash': tx_hash, 'from': B, 'to': transaction['to'],
                                'nonce': 7, 'chainId': R.CHAIN_ID, 'type': 2,
                                'gas': transaction['gas'], 'value': 0,
                                'input': transaction['data'], 'maxFeePerGas': 120,
                                'maxPriorityFeePerGas': 2}
                    if method == 'eth_getTransactionReceipt':
                        return {'transactionHash': tx_hash, 'blockNumber': 100,
                                'blockHash': receipt_hash, 'status': receipt_status}
                    return {'number': '0x64', 'hash': canonical_hash}

            result = await ReadOnlyExecutionTracker(store, Rpc()).observe('proposal-track')
            stored = store.execution_attempts('plan-track')[0]
            store.close()
            return result.status, stored['status']

        self.assertEqual(asyncio.run(scenario(0, '0x' + '92' * 32)),
                         ('reverted', 'reverted'))
        self.assertEqual(asyncio.run(scenario(1, '0x' + '94' * 32)),
                         ('orphaned', 'orphaned'))

    def test_offline_execution_signer_rechecks_and_persists_only_public_hash(self):
        async def scenario(path):
            account = Account.create()
            follower = account.address.lower()
            store = Store(path)
            store.start_paper_budget_cycle('cycle', 'test')
            store.configure_paper_budget(A, 'USDG', '1000')
            signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                            stage='swap_evidenced', execution_status='success', exact_in=True,
                            token_in=R.USDG, token_out=TOKEN, protocol='v2',
                            evidence={'route': [R.USDG, TOKEN],
                                      'actual_input_debit_raw': '100',
                                      'actual_output_credit_raw': '200'})
            store.put(signal)
            store.reserve_paper_proposal({
                'proposal_id': 'proposal-sign', 'source_event_id': signal.event_id,
                'source_tx_hash': TXHASH, 'wallet': A,
                'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
                'input_asset': R.USDG, 'output_asset': TOKEN,
                'budget_bucket': 'USDG', 'amount_in_raw': '100',
                'attribution': {'smart_wallet': A, 'follower_wallet': follower,
                                'relationship_id': '42',
                                'config_snapshot_hash': 'ab' * 32},
            })

            class Quoter:
                output = '198'

                async def quote_with_reference(self, source, amount):
                    return (Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, TOKEN, amount, self.output),
                            Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, TOKEN, '1', '2'), '100')

            class Rpc:
                nonce = 7

                async def call(self, method, params=None):
                    return {'eth_getTransactionCount': hex(self.nonce),
                            'eth_getBalance': hex(100000000),
                            'eth_gasPrice': '0x64', 'eth_call': '0x64'}[method]

            class Signer:
                def __init__(self, expected):
                    self.expected = expected

                def sign_transaction(self, transaction):
                    self.assertion = transaction
                    return bytes(account.sign_transaction(transaction).raw_transaction)

            class Gate:
                def __init__(self):
                    self.calls = []

                def validate(self, *values):
                    self.calls.append(values)

            policy = QuotePolicy(max_adverse_deviation_bps=200,
                                 max_price_impact_bps=200,
                                 max_gas_cost_wei='30000000')
            route = frozenset({signal_route_key(signal)})
            prepared = await ExecutionPreparer(
                store, Quoter(), Rpc(), policy, frozenset({'v2'}),
                frozenset({R.USDG, TOKEN}), route, 'ab' * 32).prepare(
                    signal, 'proposal-sign', now=101)
            sign_quoter, sign_rpc = Quoter(), Rpc()
            gate = Gate()
            offline = OfflineExecutionSigner(
                store, sign_quoter, sign_rpc, policy, 'ab' * 32,
                signer_factory=Signer, relationship_gate=gate)
            persisted_payload = store.connection.execute(
                "SELECT plan_payload FROM execution_plans WHERE plan_id=?",
                (prepared.plan_id,)).fetchone()[0]
            tampered = json.loads(persisted_payload)
            tampered['transaction']['data'] = '0xdeadbeef'
            store.connection.execute(
                "UPDATE execution_plans SET plan_payload=? WHERE plan_id=?",
                (json.dumps(tampered, sort_keys=True), prepared.plan_id))
            store.connection.commit()
            with patch.dict(os.environ, OFFLINE_ENV):
                with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                    await offline.sign(signal, 'proposal-sign', now=101)
            store.connection.execute(
                "UPDATE execution_plans SET plan_payload=? WHERE plan_id=?",
                (persisted_payload, prepared.plan_id))
            store.connection.commit()
            store.connection.execute(
                "UPDATE paper_reservations SET status='released' WHERE proposal_id=?",
                ('proposal-sign',))
            store.connection.commit()
            with patch.dict(os.environ, OFFLINE_ENV):
                with self.assertRaisesRegex(ValueError, 'reservation is stale'):
                    await offline.sign(signal, 'proposal-sign', now=101)
            store.connection.execute(
                "UPDATE paper_reservations SET status='active' WHERE proposal_id=?",
                ('proposal-sign',))
            store.connection.commit()
            with patch.dict(os.environ, {}, clear=False):
                for key in OFFLINE_ENV:
                    os.environ.pop(key, None)
                with self.assertRaisesRegex(PermissionError, 'emergency stop'):
                    await offline.sign(signal, 'proposal-sign', now=101)
            with patch.dict(os.environ, OFFLINE_ENV):
                sign_quoter.output = '180'
                with self.assertRaises(ValueError):
                    await offline.sign(signal, 'proposal-sign', now=101)
                sign_quoter.output = '198'
                sign_rpc.nonce = 8
                with self.assertRaisesRegex(ValueError, 'behind'):
                    await offline.sign(signal, 'proposal-sign', now=101)
                sign_rpc.nonce = 7
                signed = await offline.sign(signal, 'proposal-sign', now=101)
            self.assertEqual(Account.recover_transaction(
                signed.raw_transaction).lower(), follower)
            self.assertNotIn(signed.raw_transaction.hex(), repr(signed))
            self.assertNotIn('raw_transaction', repr(signed))
            with self.assertRaises(TypeError):
                vars(signed)
            persisted = store.execution_plan('proposal-sign')
            self.assertEqual((persisted['status'], persisted['signed_tx_hash']),
                             ('signed', signed.signed_tx_hash))
            self.assertTrue(persisted['final_review']['read_only'])
            self.assertTrue(persisted['final_review']['relationship_revalidated'])
            self.assertTrue(persisted['final_review']['transaction_fields_verified'])
            self.assertEqual(persisted['final_review']['sender_recovered'], follower)
            self.assertEqual(store.execution_nonce_reservation(
                'proposal-sign')['status'], 'signed')
            serialized = json.dumps(persisted)
            self.assertNotIn(signed.raw_transaction.hex(), serialized)
            self.assertNotIn(account.key.hex(), serialized)
            self.assertEqual(prepared.nonce, 7)
            self.assertTrue(gate.calls)
            self.assertEqual(gate.calls[-1], ('42', follower, A, 'ab' * 32))
            reviewer = ReadOnlyPreBroadcastReviewer(
                store, sign_quoter, sign_rpc, policy, gate)
            with patch.dict(os.environ, OFFLINE_ENV):
                reviewed = await reviewer.review(
                    signal, 'proposal-sign', signed.raw_transaction, now=101)
                self.assertEqual(reviewed.signed_tx_hash, signed.signed_tx_hash)
                self.assertFalse(reviewed.evidence['broadcast_performed'])
                with self.assertRaisesRegex(ValueError, 'hash does not match'):
                    await reviewer.review(
                        signal, 'proposal-sign',
                        signed.raw_transaction[:-1] + bytes([signed.raw_transaction[-1] ^ 1]),
                        now=101)
                sign_rpc.nonce = 8
                with self.assertRaisesRegex(ValueError, 'exactly match'):
                    await reviewer.review(
                        signal, 'proposal-sign', signed.raw_transaction, now=101)
            sign_rpc.nonce = 7
            store.close()

            reopened = Store(path)
            recovery = OfflineExecutionSigner(
                reopened, sign_quoter, sign_rpc, policy, 'ab' * 32,
                signer_factory=Signer, relationship_gate=gate)
            with patch.dict(os.environ, OFFLINE_ENV):
                recovered = await recovery.recover_signed(
                    signal, 'proposal-sign', now=101)
            self.assertEqual(recovered.raw_transaction, signed.raw_transaction)
            self.assertEqual(recovered.signed_tx_hash, signed.signed_tx_hash)
            self.assertEqual(len(reopened.execution_attempts(prepared.plan_id)), 1)
            self.assertTrue(reopened.observe_execution_attempt(
                prepared.plan_id, signed.signed_tx_hash, prepared.transaction))
            with patch.dict(os.environ, OFFLINE_ENV):
                with self.assertRaisesRegex(ValueError, 'unavailable or stale'):
                    await recovery.recover_signed(signal, 'proposal-sign', now=101)
            reopened.close()

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(scenario(Path(folder) / 'offline-signing.sqlite3'))

    def test_execution_preparer_persists_plan_nonce_and_restart_idempotency(self):
        async def scenario(path):
            store = Store(path)
            store.start_paper_budget_cycle('cycle', 'test')
            store.configure_paper_budget(A, 'USDG', '1000')
            signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                            stage='swap_evidenced', execution_status='success', exact_in=True,
                            token_in=R.USDG, token_out=TOKEN, protocol='v2',
                            evidence={'route': [R.USDG, TOKEN],
                                      'actual_input_debit_raw': '100',
                                      'actual_output_credit_raw': '200'})
            store.put(signal)
            self.assertTrue(store.reserve_paper_proposal({
                'proposal_id': 'proposal-1', 'source_event_id': signal.event_id,
                'source_tx_hash': TXHASH, 'wallet': A,
                'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
                'input_asset': R.USDG, 'output_asset': TOKEN,
                'budget_bucket': 'USDG', 'amount_in_raw': '100',
                'attribution': {'smart_wallet': A, 'follower_wallet': B,
                                'relationship_id': '42',
                                'config_snapshot_hash': 'ab' * 32},
            })[0])

            class Quoter:
                calls = 0

                async def quote_with_reference(self, source, amount):
                    self.calls += 1
                    return (Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, TOKEN, amount, '198'),
                            Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, TOKEN, '1', '2'), '100')

            class Rpc:
                async def call(self, method, params=None):
                    return {'eth_getTransactionCount': '0x7',
                            'eth_getBalance': hex(100000000),
                            'eth_gasPrice': '0x64',
                            'eth_call': '0x64'}[method]

            quoter = Quoter()
            policy = QuotePolicy(max_adverse_deviation_bps=200,
                                 max_price_impact_bps=200,
                                 max_gas_cost_wei='30000000')
            preparer = ExecutionPreparer(
                store, quoter, Rpc(), policy, frozenset({'v2'}),
                frozenset({R.USDG, TOKEN}), frozenset({signal_route_key(signal)}),
                'ab' * 32)
            prepared = await preparer.prepare(signal, 'proposal-1', now=101)
            self.assertEqual((prepared.nonce, prepared.existing), (7, False))
            self.assertEqual(prepared.transaction['to'].lower(), R.V2_ROUTER)
            duplicate = await preparer.prepare(signal, 'proposal-1', now=102)
            self.assertEqual((duplicate.plan_id, duplicate.nonce, duplicate.existing),
                             (prepared.plan_id, 7, True))
            self.assertEqual(quoter.calls, 1)
            store.close()
            reopened = Store(path)
            persisted = reopened.execution_plan('proposal-1')
            self.assertEqual((persisted['transaction']['nonce'], persisted['status']),
                             (7, 'prepared'))
            self.assertNotIn('private', json.dumps(persisted).lower())
            reopened.connection.execute(
                "UPDATE execution_plans SET preflight_payload=? WHERE proposal_id=?",
                (json.dumps({'read_only': False}), 'proposal-1'))
            reopened.connection.commit()
            with self.assertRaisesRegex(ValueError, 'integrity mismatch'):
                reopened.execution_plan('proposal-1')
            reopened.close()

        with tempfile.TemporaryDirectory() as folder:
            asyncio.run(scenario(Path(folder) / 'execution.sqlite3'))

    def test_execution_builder_emits_v2_v3_and_bounded_native_v4_calldata(self):
        common = dict(
            follower_wallet=B, relationship_id='42', proposal_id='proposal-1',
            minimum_amount_out_raw='190', deadline=200, gas_limit=200000,
            max_fee_per_gas='100', max_priority_fee_per_gas='1',
        )
        v2 = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                    stage='swap_evidenced', execution_status='success', exact_in=True,
                    token_in=R.USDG, token_out=TOKEN, protocol='v2',
                    evidence={'route': [R.USDG, TOKEN]})
        v2_quote = Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32, 100.0,
                         R.USDG, TOKEN, '100', '200')
        route = frozenset({signal_route_key(v2)})
        plan = build_execution_plan(
            v2, quote=v2_quote, allowed_protocols=frozenset({'v2'}),
            allowed_assets=frozenset({R.USDG, TOKEN}), allowed_routes=route, **common)
        signature = 'swapExactTokensForTokens(uint256,uint256,address[],address,uint256)'
        self.assertEqual(bytes.fromhex(plan.data[2:10]), selector(signature))
        decoded = decode(['uint256', 'uint256', 'address[]', 'address', 'uint256'],
                         bytes.fromhex(plan.data[10:]))
        self.assertEqual((decoded[0], decoded[1], decoded[3], decoded[4]),
                         (100, 190, B, 200))
        for unsafe in (
                replace(v2, stage='needs_review'),
                replace(v2, execution_status='unknown'),
                replace(v2, canonical_status='orphaned'),
                replace(v2, behavior='UNKNOWN')):
            with self.assertRaisesRegex(ValueError, 'not eligible'):
                build_execution_plan(
                    unsafe, quote=v2_quote, allowed_protocols=frozenset({'v2'}),
                    allowed_assets=frozenset({R.USDG, TOKEN}),
                    allowed_routes=route, **common)

        v3 = replace(v2, contract=R.V3_ROUTER, protocol='v3',
                     evidence={'hops': [{'token_in': R.USDG,
                                         'token_out': TOKEN, 'fee': 500}]})
        v3_quote = replace(v2_quote, protocol='v3')
        v3_plan = build_execution_plan(
            v3, quote=v3_quote, allowed_protocols=frozenset({'v3'}),
            allowed_assets=frozenset({R.USDG, TOKEN}),
            allowed_routes=frozenset({signal_route_key(v3)}), **common)
        self.assertEqual(bytes.fromhex(v3_plan.data[2:10]),
                         selector('exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))'))
        params = decode(['(address,address,uint24,address,uint256,uint256,uint160)'],
                        bytes.fromhex(v3_plan.data[10:]))[0]
        self.assertEqual((params[2], params[3], params[4], params[5]), (500, B, 100, 190))
        token_v4 = replace(
            v2, contract=R.UNIVERSAL_ROUTER, protocol='v4',
            evidence={'pool_key': [R.USDG, TOKEN, 3000, 60, R.NATIVE],
                      'hook_data': '0x'})
        with self.assertRaisesRegex(ValueError, 'Permit2'):
            build_execution_plan(
                token_v4, quote=replace(v2_quote, protocol='v4'),
                allowed_protocols=frozenset({'v4'}),
                allowed_assets=frozenset({R.USDG, TOKEN}),
                allowed_routes=frozenset({signal_route_key(token_v4)}), **common)

        v4 = replace(v2, contract=R.UNIVERSAL_ROUTER, protocol='v4',
                     token_in=R.NATIVE,
                     evidence={'pool_key': [R.NATIVE, TOKEN, 3000, 60, R.NATIVE],
                               'hook_data': '0x'})
        v4_quote = Quote('v4', R.V4_QUOTER, 10, '0x' + 'ab' * 32, 100.0,
                         R.NATIVE, TOKEN, '100', '200')
        v4_plan = build_execution_plan(
            v4, quote=v4_quote, allowed_protocols=frozenset({'v4'}),
            allowed_assets=frozenset({R.NATIVE, TOKEN}),
            allowed_routes=frozenset({signal_route_key(v4)}), **common)
        self.assertEqual((v4_plan.to, v4_plan.value_raw), (R.UNIVERSAL_ROUTER, '100'))
        decoded_v4 = Decoder({B: {}}).decode(Transaction(
            TXHASH, B, R.UNIVERSAL_ROUTER, bytes.fromhex(v4_plan.data[2:]),
            value=100, fresh=True))[0]
        self.assertEqual((decoded_v4.protocol, decoded_v4.exact_in,
                          decoded_v4.token_in, decoded_v4.token_out),
                         ('v4', True, R.NATIVE, TOKEN))
        self.assertEqual([item['action'] for item in
                          decoded_v4.evidence['v4_settlement_actions']],
                         ['SETTLE_ALL', 'TAKE_ALL'])
        self.assertEqual(decoded_v4.evidence['v4_settlement_actions'][1]['recipient'], B)
        unknown_hook = replace(
            v4, evidence={'pool_key': [R.NATIVE, TOKEN, 3000, 60, '0x' + '12' * 20],
                          'hook_data': '0x'})
        with self.assertRaisesRegex(ValueError, 'hook is not approved'):
            build_execution_plan(
                unknown_hook, quote=v4_quote, allowed_protocols=frozenset({'v4'}),
                allowed_assets=frozenset({R.NATIVE, TOKEN}),
                allowed_routes=frozenset({signal_route_key(unknown_hook)}), **common)

    def test_nonce_reservations_are_persistent_idempotent_and_gap_free(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'nonce.sqlite3'
            store = Store(path)
            self.assertEqual(store.reserve_execution_nonce(
                'r1', B, 'rel-1', 'proposal-1', R.CHAIN_ID, 7), (7, 'reserved'))
            self.assertEqual(store.reserve_execution_nonce(
                'r1-other', B, 'rel-1', 'proposal-1', R.CHAIN_ID, 99),
                (7, 'reserved'))
            self.assertEqual(store.reserve_execution_nonce(
                'r2', B, 'rel-2', 'proposal-2', R.CHAIN_ID, 7), (8, 'reserved'))
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.execution_nonce_reservation('proposal-2')['nonce'], 8)
            self.assertTrue(reopened.update_execution_nonce_status('r1', 'reserved', 'signed'))
            self.assertFalse(reopened.update_execution_nonce_status('r1', 'reserved', 'released'))
            with self.assertRaisesRegex(ValueError, 'transition'):
                reopened.update_execution_nonce_status('r1', 'signed', 'confirmed')
            reopened.close()

    def test_readonly_execution_preflight_checks_nonce_balances_and_gas(self):
        plan = UnsignedExecutionPlan(
            follower_wallet=B, relationship_id='42', proposal_id='proposal-1',
            to=R.V2_ROUTER, data='0x12345678', value_raw='0', input_asset=R.USDG,
            amount_in_raw='100', minimum_amount_out_raw='190', gas_limit=200000,
            max_fee_per_gas='100', max_priority_fee_per_gas='1',
            quote_observed_at=100.0, quote_block_number=10,
            quote_block_hash='0x' + 'ab' * 32, deadline=200,
        )

        class Rpc:
            async def call(self, method, params=None):
                if method == 'eth_getTransactionCount':
                    return '0x7'
                if method == 'eth_getBalance':
                    return hex(100000000)
                if method == 'eth_gasPrice':
                    return '0x32'
                if method == 'eth_call':
                    self.calls = getattr(self, 'calls', []) + [params]
                    return '0x64'
                raise AssertionError(method)

        rpc = Rpc()
        result = asyncio.run(ReadOnlyExecutionPreflight(
            rpc, frozenset({R.V2_ROUTER}), '30000000').check(plan, now=101))
        self.assertEqual((result['pending_nonce'], result['token_balance_raw'],
                          result['maximum_gas_cost_wei'], result['read_only']),
                         (7, '100', '20000000', True))
        self.assertEqual(result['token_allowance_raw'], '100')
        self.assertEqual([call[1] for call in rpc.calls], ['pending', 'pending'])
        expired = replace(plan, quote_observed_at=90.0)
        with self.assertRaisesRegex(ValueError, 'expired'):
            asyncio.run(ReadOnlyExecutionPreflight(
                rpc, frozenset({R.V2_ROUTER}), '30000000').check(expired, now=101))

    def test_database_signer_is_offline_gated_and_never_exposes_key(self):
        account = Account.create()

        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params):
                self.sql, self.params = sql, params

            def fetchall(self):
                return [{'private_key_hex': '0x' + account.key.hex()}]

        class Connection:
            def cursor(self):
                return Cursor()

            def close(self):
                self.closed = True

        signer = OfflineDatabaseSigner(account.address)
        with patch.dict(os.environ, {}, clear=False):
            for key in OFFLINE_ENV:
                os.environ.pop(key, None)
            with self.assertRaisesRegex(PermissionError, 'emergency stop'):
                signer.sign_transaction({})
        transaction = {
            'chainId': R.CHAIN_ID, 'nonce': 0,
            'to': '0x' + '12' * 20, 'value': 0, 'data': '0x',
            'gas': 21000, 'maxFeePerGas': 2, 'maxPriorityFeePerGas': 1,
            'type': 2,
        }
        with patch.dict(os.environ, OFFLINE_ENV), \
                patch('smart_money.key_source._key_connection', return_value=Connection()):
            raw = signer.sign_transaction(transaction)
        self.assertEqual(Account.recover_transaction(raw).lower(), account.address.lower())
        self.assertNotIn(account.key.hex(), repr(signer))

    def test_key_status_reads_only_public_metadata_and_is_offline_gated(self):
        queries = []

        with patch.dict(os.environ, {}, clear=True), patch(
                'smart_money.key_source.pymysql.connect') as connect:
            with self.assertRaises(PermissionError):
                key_record_status(B)
            connect.assert_not_called()

        class Cursor:
            rows = [{'wallet_address': B, 'enabled': 1}]

            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql, params):
                queries.append((sql, params))
            def fetchall(self): return self.rows

        class Connection:
            def cursor(self): return Cursor()
            def close(self): self.closed = True

        with patch.dict(os.environ, OFFLINE_ENV), patch(
                'smart_money.key_source._key_connection', return_value=Connection()):
            status = key_record_status(B)
        self.assertEqual(status, {
            'wallet_address': B, 'found': True, 'enabled': True,
            'private_key_read': False, 'read_only': True,
        })
        self.assertEqual(queries[0][1], (B,))
        self.assertNotIn('private_key', queries[0][0].lower())

        Cursor.rows = []
        with patch.dict(os.environ, OFFLINE_ENV), patch(
                'smart_money.key_source._key_connection', return_value=Connection()):
            status = key_record_status(B)
        self.assertFalse(status['found'])
        self.assertFalse(status['enabled'])
        self.assertFalse(status['private_key_read'])

        Cursor.rows = [{'wallet_address': B, 'enabled': 1}]
        with patch('smart_money.key_source._key_connection', return_value=Connection()):
            status = live_key_record_status(B, '42', 'ab' * 32)
        self.assertTrue(status['found'])
        self.assertTrue(status['enabled'])
        self.assertFalse(status['private_key_read'])
        self.assertNotIn('private_key', queries[-1][0].lower())

    def test_live_database_signer_is_bound_to_relationship_snapshot(self):
        account = Account.create()
        follower = account.address.lower()

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *_): return False
            def execute(self, sql, params): self.sql, self.params = sql, params
            def fetchall(self):
                return [{'private_key_hex': '0x' + account.key.hex()}]

        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass

        transaction = {
            'chainId': R.CHAIN_ID, 'nonce': 0, 'to': to_checksum_address(A),
            'value': 0, 'data': '0x', 'gas': 21000, 'maxFeePerGas': 2,
            'maxPriorityFeePerGas': 1, 'type': 2,
        }
        signer = LiveDatabaseSigner(follower, '42', 'ab' * 32)
        with patch.dict(os.environ, {}, clear=True), patch(
                'smart_money.key_source._key_connection') as connection:
            with self.assertRaises(PermissionError):
                signer.sign_transaction(transaction)
            connection.assert_not_called()
        live = {'SMART_MONEY_EMERGENCY_STOP_FILE': OFFLINE_ENV[
            'SMART_MONEY_EMERGENCY_STOP_FILE']}
        policy = SimpleNamespace(
            run_mode='mainnet_live', follower_wallet=follower,
            relationship_id='42', snapshot_hash='ab' * 32,
        )
        with patch.dict(os.environ, live, clear=True), patch(
                'smart_money.mysql_config.load_enabled_mainnet_acceptance',
                return_value=accepted_mainnet_relationship(policy)), patch(
                'smart_money.key_source._key_connection',
                return_value=Connection()):
            raw = signer.sign_transaction(transaction)
        self.assertEqual(Account.recover_transaction(raw).lower(), follower)
        wrong = LiveDatabaseSigner(follower, '43', 'ab' * 32)
        with patch.dict(os.environ, live, clear=True), patch(
                'smart_money.mysql_config.load_enabled_mainnet_acceptance',
                return_value=accepted_mainnet_relationship(policy)), patch(
                'smart_money.key_source._key_connection') as connection:
            with self.assertRaisesRegex(PermissionError, 'does not match'):
                wrong.sign_transaction(transaction)
            connection.assert_not_called()

    def test_execution_process_controls_require_enabled_bound_mainnet_relationship(self):
        with patch.dict(os.environ, {}, clear=False):
            for key in OFFLINE_ENV:
                os.environ.pop(key, None)
            with self.assertRaisesRegex(PermissionError, 'emergency stop'):
                require_offline_signing_enabled()
        for missing in OFFLINE_CONTROL_KEYS:
            values = dict(OFFLINE_ENV)
            values.pop(missing)
            with patch.dict(os.environ, values, clear=True):
                with self.assertRaises(PermissionError):
                    require_offline_signing_enabled()
        with patch.dict(os.environ, OFFLINE_ENV, clear=True):
            require_offline_signing_enabled()
        with tempfile.TemporaryDirectory() as folder:
            stop_file = Path(folder) / 'EXECUTION_STOP'
            stop_file.write_text('stop\n')
            with patch.dict(os.environ, {
                    **OFFLINE_ENV,
                    'SMART_MONEY_EMERGENCY_STOP_FILE': str(stop_file),
            }, clear=True):
                with self.assertRaisesRegex(PermissionError, 'stop file'):
                    require_offline_signing_enabled()
        live = {'SMART_MONEY_EMERGENCY_STOP_FILE': OFFLINE_ENV[
            'SMART_MONEY_EMERGENCY_STOP_FILE']}
        policy = SimpleNamespace(
            run_mode='mainnet_live', follower_wallet=B,
            relationship_id='42', snapshot_hash='ab' * 32,
        )
        with patch.dict(os.environ, live, clear=True), patch(
                'smart_money.mysql_config.load_enabled_mainnet_acceptance',
                return_value=accepted_mainnet_relationship(policy)):
            evidence = require_mainnet_signing_enabled(B, '42', 'ab' * 32)
            self.assertEqual(evidence['follower_wallet'], B)
            self.assertEqual(
                evidence['acceptance_source'], 'enabled_mainnet_mysql_relationship')
            require_mainnet_broadcast_enabled(B, '42', 'ab' * 32)
            with self.assertRaisesRegex(PermissionError, 'does not match'):
                require_mainnet_broadcast_enabled(A, '42', 'ab' * 32)
        with patch.dict(os.environ, live, clear=True), patch(
                'smart_money.mysql_config.load_enabled_mainnet_acceptance',
                side_effect=ValueError('relationship is disabled or unavailable')):
            with self.assertRaisesRegex(PermissionError, 'unavailable'):
                require_mainnet_broadcast_enabled(B, '42', 'ab' * 32)
        with patch.dict(os.environ, live, clear=True), patch(
                'smart_money.mysql_config.load_enabled_mainnet_acceptance',
                return_value=accepted_mainnet_relationship(policy, stale=True)):
            with self.assertRaisesRegex(PermissionError, 'stale'):
                require_mainnet_broadcast_enabled(B, '42', 'ab' * 32)

    def test_mainnet_broadcaster_is_separate_hash_checked_and_default_closed(self):
        account = Account.create()
        follower = account.address.lower()
        transaction = {
            'chainId': R.CHAIN_ID, 'nonce': 0, 'to': to_checksum_address(A),
            'value': 0, 'data': '0x', 'gas': 21000, 'maxFeePerGas': 2,
            'maxPriorityFeePerGas': 1, 'type': 2,
        }
        raw = bytes(account.sign_transaction(transaction).raw_transaction)
        from eth_utils import keccak
        tx_hash = '0x' + keccak(raw).hex()
        review = SimpleNamespace(
            proposal_id='proposal-live', signed_tx_hash=tx_hash,
            evidence={'broadcast_performed': False})
        broadcaster = MainnetBroadcaster('https://rpc.example')
        with patch.dict(os.environ, {
                'SMART_MONEY_EMERGENCY_STOP_FILE': OFFLINE_ENV[
                    'SMART_MONEY_EMERGENCY_STOP_FILE'],
        }, clear=True), patch(
                'smart_money.mysql_config.load_enabled_mainnet_acceptance',
                side_effect=ValueError('relationship is disabled')), \
                self.assertRaises(PermissionError):
            asyncio.run(broadcaster.broadcast(
                review, raw, follower_wallet=follower, relationship_id='42',
                config_snapshot_hash='ab' * 32))

        live = {'SMART_MONEY_EMERGENCY_STOP_FILE': OFFLINE_ENV[
            'SMART_MONEY_EMERGENCY_STOP_FILE']}
        policy = SimpleNamespace(
            run_mode='mainnet_live', follower_wallet=follower,
            relationship_id='42', snapshot_hash='ab' * 32,
        )
        with patch.dict(os.environ, live, clear=True), patch(
                'smart_money.mysql_config.load_enabled_mainnet_acceptance',
                return_value=accepted_mainnet_relationship(policy)), patch(
                'smart_money.broadcast.asyncio.to_thread',
                new=AsyncMock(return_value=tx_hash)) as send:
            result = asyncio.run(broadcaster.broadcast(
                review, raw, follower_wallet=follower, relationship_id='42',
                config_snapshot_hash='ab' * 32))
        self.assertTrue(result.submitted)
        self.assertEqual(result.tx_hash, tx_hash)
        send.assert_awaited_once_with(broadcaster._request, '0x' + raw.hex())

        # Time spent in the final database gate must count against the original
        # full/reference quote timestamps; it cannot renew either quote's TTL.
        timed_review = SimpleNamespace(
            proposal_id=review.proposal_id, signed_tx_hash=tx_hash,
            evidence={'broadcast_performed': False, 'quote_max_age_seconds': 2.0,
                      'quote': {'observed_at': 100.0},
                      'reference_quote': {'observed_at': 99.0}})
        with patch('smart_money.broadcast.require_mainnet_broadcast_enabled'), patch(
                'smart_money.broadcast.time.time', return_value=102.0), patch(
                'smart_money.broadcast.asyncio.to_thread', new=AsyncMock()) as send:
            with self.assertRaisesRegex(ValueError, 'expired before broadcast'):
                asyncio.run(broadcaster.broadcast(
                    timed_review, raw, follower_wallet=follower, relationship_id='42',
                    config_snapshot_hash='ab' * 32))
            send.assert_not_called()

        expired_trial_review = SimpleNamespace(
            proposal_id=review.proposal_id, signed_tx_hash=tx_hash,
            evidence={'broadcast_performed': False, 'early_trial_id': 'trial',
                      'early_trial_expires_at': 101.0})
        with patch('smart_money.broadcast.require_mainnet_broadcast_enabled'), patch(
                'smart_money.broadcast.time.time', return_value=101.0), patch(
                'smart_money.broadcast.asyncio.to_thread', new=AsyncMock()) as send:
            with self.assertRaisesRegex(ValueError, 'trial expired before broadcast'):
                asyncio.run(broadcaster.broadcast(
                    expired_trial_review, raw, follower_wallet=follower, relationship_id='42',
                    config_snapshot_hash='ab' * 32))
            send.assert_not_called()

        with patch.dict(os.environ, live, clear=True), patch(
                'smart_money.mysql_config.load_enabled_mainnet_acceptance',
                return_value=accepted_mainnet_relationship(policy)), patch(
                'smart_money.broadcast.asyncio.to_thread',
                new=AsyncMock(return_value='0x' + 'ff' * 32)), \
                self.assertRaisesRegex(RuntimeError, 'different transaction hash'):
            asyncio.run(broadcaster.broadcast(
                review, raw, follower_wallet=follower, relationship_id='42',
                config_snapshot_hash='ab' * 32))

    def test_final_execution_review_rejects_secret_or_raw_transaction_fields(self):
        store = Store(':memory:')
        tx_hash = '0x' + '91' * 32
        for evidence in ({'private_key': 'never'}, {'raw_transaction': '0x1234'}):
            with self.assertRaisesRegex(ValueError, 'invalid final execution review'):
                store.mark_execution_plan_signed('missing', 'missing', tx_hash, evidence)
        store.close()

    def test_usdg_approval_is_200x_budget_bounded_and_relationship_gated(self):
        policy = SimpleNamespace(
            run_mode='mainnet_live', follower_wallet=B, relationship_id='42',
            wallet=A, snapshot_hash='ab' * 32,
            allowed_assets=frozenset({R.USDG}),
            budget_limits={'USDG': '10000000'},
            quote_policy=QuotePolicy(max_gas_cost_wei='1000000000000000'),
        )
        gate = MagicMock()
        broadcaster = SimpleNamespace(broadcast=AsyncMock(return_value=SimpleNamespace(
            tx_hash='0x' + '99' * 32)))

        class Rpc:
            async def call(self, method, params=None):
                if method == 'eth_call' and params[0].get('from'):
                    return '0x1'
                return {
                    'eth_call': '0x0', 'eth_getCode': '0x6000',
                    'eth_getTransactionCount': '0x3',
                    'eth_gasPrice': '0x64', 'eth_getBalance': hex(10 ** 18),
                }[method]

        class Signer:
            def __init__(self, wallet, relationship, snapshot):
                self.identity = wallet, relationship, snapshot

            def sign_transaction(self, transaction):
                self.transaction = transaction
                return b'signed-approval'

        result = asyncio.run(approve_relationship_usdg(
            policy, Rpc(), gate, broadcaster, signer_factory=Signer))
        self.assertTrue(result.submitted)
        self.assertEqual(result.amount_raw, '2000000000')
        gate.validate.assert_called_once_with('42', B, A, 'ab' * 32)
        review, raw = broadcaster.broadcast.await_args.args
        self.assertEqual(raw, b'signed-approval')
        self.assertEqual(review.evidence['amount_raw'], '2000000000')
        self.assertEqual(review.evidence['spender'], R.V3_ROUTER)

        already = AsyncMock(call=AsyncMock(return_value=hex(2000000000)))
        no_submit = asyncio.run(approve_relationship_usdg(
            policy, already, gate, broadcaster, signer_factory=Signer))
        self.assertFalse(no_submit.submitted)
        self.assertIsNone(no_submit.tx_hash)

        spent_but_sufficient = AsyncMock(
            call=AsyncMock(return_value=hex(1998000000)))
        no_top_up = asyncio.run(approve_relationship_usdg(
            policy, spent_but_sufficient, gate, broadcaster,
            signer_factory=Signer, minimum_required_raw='2000000'))
        self.assertFalse(no_top_up.submitted)
        self.assertEqual((no_top_up.amount_raw, no_top_up.previous_allowance_raw),
                         ('2000000000', '1998000000'))

    def test_dynamic_sell_token_approval_is_position_bounded_and_confirmed(self):
        policy = SimpleNamespace(
            run_mode='mainnet_live', follower_wallet=B, relationship_id='42',
            wallet=A, snapshot_hash='ab' * 32,
            quote_policy=QuotePolicy(max_gas_cost_wei='1000000000000000'),
        )
        tx_hash = '0x' + '99' * 32
        block_hash = '0x' + '88' * 32
        gate = MagicMock()
        broadcaster = SimpleNamespace(broadcast=AsyncMock(return_value=SimpleNamespace(
            tx_hash=tx_hash)))

        class Rpc:
            def __init__(self):
                self.allowance = 7

            async def call(self, method, params=None):
                if method == 'eth_getCode':
                    return '0x6000'
                if method == 'eth_call' and params[0].get('from'):
                    return '0x1'
                if method == 'eth_call':
                    return hex(self.allowance)
                if method == 'eth_getTransactionCount':
                    return '0x3'
                if method == 'eth_gasPrice':
                    return '0x64'
                if method == 'eth_getBalance':
                    return hex(10 ** 18)
                if method == 'eth_getBlockByNumber':
                    return {'hash': block_hash}
                raise AssertionError(method)

            async def receipt(self, tx, attempts=8, interval=.25):
                self.allowance = 5000
                return {'transactionHash': tx, 'status': '0x1',
                        'blockNumber': '0x12', 'blockHash': block_hash}

        class Signer:
            def __init__(self, wallet, relationship, snapshot):
                pass

            def sign_transaction(self, transaction):
                return b'signed-token-approval'

        rpc = Rpc()
        result = asyncio.run(approve_relationship_token(
            policy, rpc, gate, broadcaster, TOKEN, '5000', R.V3_ROUTER,
            signer_factory=Signer))
        self.assertTrue(result.submitted)
        self.assertEqual((result.asset, result.amount_raw,
                          result.previous_allowance_raw), (TOKEN, '5000', '7'))
        confirmation = asyncio.run(confirm_relationship_token_approval(
            rpc, result, B))
        self.assertEqual((confirmation['allowance_raw'], confirmation['block_number']),
                         ('5000', 18))
        gate.validate.assert_called_once_with('42', B, A, 'ab' * 32)

        gate.reset_mock()
        broadcaster.broadcast.reset_mock()
        rpc.allowance = 4000
        no_top_up = asyncio.run(approve_relationship_token(
            policy, rpc, gate, broadcaster, TOKEN, '5000', R.V3_ROUTER,
            signer_factory=Signer, minimum_required_raw='1000'))
        self.assertFalse(no_top_up.submitted)
        self.assertEqual((no_top_up.amount_raw, no_top_up.previous_allowance_raw),
                         ('5000', '4000'))
        broadcaster.broadcast.assert_not_awaited()

        with self.assertRaisesRegex(ValueError, 'minimum allowance'):
            asyncio.run(approve_relationship_token(
                policy, rpc, gate, broadcaster, TOKEN, '5000', R.V3_ROUTER,
                signer_factory=Signer, minimum_required_raw='5001'))

    def test_remote_mysql_requires_ca_before_credentials_are_used(self):
        with patch.dict(os.environ, {
                'SMART_MONEY_MYSQL_HOST': 'remote.example',
                'SMART_MONEY_MYSQL_PASSWORD': 'must-not-leak',
        }, clear=False), patch('smart_money.mysql_config.pymysql.connect') as connect:
            os.environ.pop('SMART_MONEY_MYSQL_SSL_CA', None)
            with self.assertRaisesRegex(ValueError, 'requires SMART_MONEY_MYSQL_SSL_CA'):
                mysql_connection()
            connect.assert_not_called()

    def test_relationship_import_rejects_new_zero_wallet_placeholders(self):
        with patch('smart_money.mysql_config.mysql_connection') as connect:
            with self.assertRaisesRegex(ValueError, 'zero follower wallet'):
                import_watchlist_relationships(
                    R.NATIVE, 'invalid', ROOT / 'data/fomo_watchlist.csv',
                    ROOT / 'config/paper.example.json')
            connect.assert_not_called()
        with patch('smart_money.mysql_config.load_watchlist',
                   return_value={R.NATIVE: {'handle': 'invalid'}}), patch(
                       'smart_money.mysql_config.mysql_connection') as connect:
            with self.assertRaisesRegex(ValueError, 'zero smart wallet'):
                import_watchlist_relationships(
                    B, 'follower', 'unused.csv',
                    ROOT / 'config/paper.example.json')
            connect.assert_not_called()

    def test_remote_mysql_never_falls_back_to_local_credentials(self):
        with patch.dict(os.environ, {
                'SMART_MONEY_MYSQL_HOST': 'remote.example',
                'SMART_MONEY_MYSQL_SSL_CA': '/trusted/ca.pem',
        }, clear=True), patch('smart_money.mysql_config.pymysql.connect') as connect:
            with self.assertRaisesRegex(ValueError, 'requires explicit connection settings'):
                mysql_connection()
            connect.assert_not_called()

        with patch.dict(os.environ, {
                **OFFLINE_ENV,
                'SMART_MONEY_KEY_MYSQL_HOST': 'keys.remote.example',
                'SMART_MONEY_KEY_MYSQL_SSL_CA': '/trusted/ca.pem',
        }, clear=True), patch('smart_money.key_source.pymysql.connect') as connect:
            signer = OfflineDatabaseSigner(B)
            with self.assertRaisesRegex(ValueError, 'requires explicit connection settings'):
                signer._load_account()
            connect.assert_not_called()

    def test_business_mysql_ledger_schema_is_complete_and_secret_free(self):
        sql = (ROOT / 'docker/mysql/init/003_runtime_ledger.sql').read_text()
        tables = {
            'signals', 'candidates', 'chain_cursors', 'canonical_blocks',
            'candidate_inclusions', 'solver_order_evidence',
            'paper_budget_cycles', 'paper_budgets', 'paper_proposals',
            'paper_reservations', 'paper_orders', 'paper_fills',
            'paper_positions', 'paper_position_reservations',
            'paper_realized_pnl', 'paper_position_marks', 'paper_decisions',
            'execution_nonce_reservations', 'execution_plans',
            'execution_attempts',
        }
        for table in tables:
            self.assertIn(f'CREATE TABLE IF NOT EXISTS {table} (', sql)
            self.assertIn(
                f'GRANT SELECT, INSERT, UPDATE, DELETE ON smart_money.{table}',
                sql)
        self.assertEqual(sql.count('CREATE TABLE IF NOT EXISTS '), len(tables))
        self.assertNotIn('wallet_keys', sql)
        self.assertNotIn('private_key', sql)
        self.assertIn('amount_in_raw VARCHAR(80)', sql)
        acceptance_sql = (
            ROOT / 'docker/mysql/init/006_database_live_acceptance.sql').read_text()
        self.assertIn('live_risk_accepted_at', acceptance_sql)
        self.assertIn('TIMESTAMP(6) NULL', acceptance_sql)
        self.assertNotIn('private_key', acceptance_sql)
        self.assertIn('realized_pnl_raw VARCHAR(81)', sql)
        self.assertIn('UNIQUE KEY uq_one_active_paper_budget_cycle', sql)
        arc_keys = (
            ROOT / 'docker/mysql/init/012_arc_chain_keys.sql').read_text()
        self.assertIn('ADD PRIMARY KEY (chain_id, name)', arc_keys)
        self.assertIn('ADD PRIMARY KEY (chain_id, block_number)', arc_keys)
        self.assertIn(
            'ADD UNIQUE KEY uq_canonical_block_hash (chain_id, block_hash)',
            arc_keys)
        self.assertIn('maintenance window', arc_keys)

    def test_mysql_store_translates_only_bounded_store_sql(self):
        self.assertEqual(
            MySqlConnectionCompat._sql(
                "INSERT OR IGNORE INTO candidates(tx_hash,payload) VALUES(?,?)"),
            "INSERT IGNORE INTO candidates(tx_hash,payload) VALUES(%s,%s)")
        translated = MySqlConnectionCompat._sql(
            "INSERT INTO chain_cursors(name,block_number,block_hash) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET "
            "block_number=excluded.block_number,block_hash=excluded.block_hash")
        self.assertIn("ON DUPLICATE KEY UPDATE", translated)
        self.assertIn("block_number=VALUES(block_number)", translated)
        self.assertNotIn("excluded.", translated)
        self.assertTrue(MySqlConnectionCompat._sql(
            "SELECT limit_raw FROM paper_budgets WHERE cycle_id=?", lock=True
        ).endswith(" FOR UPDATE"))
        json_join = MySqlConnectionCompat._sql(
            "SELECT json_extract(p.attribution_payload,'$.source_event_id')")
        self.assertIn("USING ascii", json_join)
        self.assertIn("COLLATE ascii_bin", json_join)
        parsed = cli_parser().parse_args(['paper-export', '--ledger-mysql'])
        self.assertTrue(parsed.ledger_mysql)

    def test_sqlite_ledger_migration_is_idempotent_and_conflict_safe(self):
        class FakeCursor:
            def __init__(self, connection):
                self.connection = connection
                self.result = []
                self.rowcount = 0

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def execute(self, sql, params=()):
                if sql.startswith('SHOW COLUMNS'):
                    table = sql.split('`')[1]
                    self.result = [{'Field': column}
                                   for column in self.connection.columns[table]]
                    return
                if sql.startswith('SELECT'):
                    table = sql.split('FROM `', 1)[1].split('`')[0]
                    key = tuple(params)
                    found = self.connection.rows[table].get(key)
                    self.result = [found] if found is not None else []
                    return
                if sql.startswith('INSERT'):
                    table = sql.split('INSERT INTO `', 1)[1].split('`')[0]
                    column_text = sql[sql.index('(') + 1:sql.index(')')]
                    columns = tuple(part.strip('`') for part in column_text.split(','))
                    row = dict(zip(columns, params))
                    key = tuple(row[column] for column in self.connection.primary_keys[table])
                    self.connection.rows[table][key] = row
                    self.rowcount = 1
                    return
                raise AssertionError(sql)

            def fetchall(self):
                return self.result

            def fetchone(self):
                return self.result[0] if self.result else None

        class FakeConnection:
            def __init__(self, columns, primary_keys):
                self.columns = columns
                self.primary_keys = primary_keys
                self.rows = {table: {} for table in columns}
                self.commits = self.rollbacks = 0

            def begin(self):
                pass

            def cursor(self):
                return FakeCursor(self)

            def commit(self):
                self.commits += 1

            def rollback(self):
                self.rollbacks += 1

            def close(self):
                pass

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'ledger.sqlite3'
            store = Store(path)
            signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER,
                            '0x12345678', stage='swap_evidenced')
            self.assertTrue(store.put(signal))
            store.start_paper_budget_cycle('migration-cycle', 'test')
            store.configure_paper_budget(A, 'USDG', '1000')
            trial = store.start_early_trial('migration-trial', B, [1])
            self.assertTrue(store.reserve_paper_proposal({
                'proposal_id': 'migration-proposal', 'source_event_id': signal.event_id,
                'source_tx_hash': TXHASH, 'wallet': A, 'trigger_mode': 'evidenced',
                'strategy_version': 'test', 'input_asset': R.USDG, 'output_asset': TOKEN,
                'budget_bucket': 'USDG', 'amount_in_raw': '100',
                'attribution': {'smart_wallet': A, 'follower_wallet': B,
                                'relationship_id': '1', 'copy_operation_order_id': TXHASH,
                                'source_behavior': 'BUY', 'early_trial_id': 'migration-trial'},
            })[0])
            store.close()
            source = sqlite3.connect(path)
            tables = [row[0] for row in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")]
            columns = {}
            primary_keys = {}
            for table in tables:
                info = source.execute(f'PRAGMA table_info(`{table}`)').fetchall()
                columns[table] = tuple(row[1] for row in info)
                primary_keys[table] = tuple(row[1] for row in sorted(
                    (row for row in info if row[5]), key=lambda row: row[5]))
            source.close()
            target = FakeConnection(columns, primary_keys)
            factory = lambda write=False: target
            digest = sqlite_sha256(path)
            first = migrate_sqlite_ledger(path, digest, factory)
            self.assertEqual(first['source_rows'], 7)
            self.assertEqual(first['inserted_rows'], 7)
            self.assertEqual(first['tables']['copy_operation_claims']['inserted_rows'], 1)
            self.assertEqual(target.rows['early_trials'][('migration-trial',)]['expires_at'],
                             trial['expires_at'])
            self.assertFalse(first['private_key_data_migrated'])
            second = migrate_sqlite_ledger(path, digest, factory)
            self.assertEqual(second['inserted_rows'], 0)
            self.assertEqual(second['existing_identical_rows'], 7)
            claim = next(iter(target.rows['copy_operation_claims'].values()))
            self.assertEqual(claim['status'], 'held')
            claim['status'] = 'released'
            with self.assertRaisesRegex(ValueError, 'target row conflicts'):
                migrate_sqlite_ledger(path, digest, factory)
            claim['status'] = 'held'
            target.rows['signals'][(signal.event_id,)]['payload'] = '{}'
            with self.assertRaisesRegex(ValueError, 'target row conflicts'):
                migrate_sqlite_ledger(path, digest, factory)
            self.assertEqual(target.rollbacks, 2)

            wal = Path(str(path) + '-wal')
            wal.write_bytes(b'active-writer')
            with self.assertRaisesRegex(ValueError, 'WAL is non-empty'):
                migrate_sqlite_ledger(path, digest, factory)

        parsed = cli_parser().parse_args([
            'ledger-migrate', '--sqlite', 'observer.sqlite3',
            '--confirm-source-sha256', 'ab' * 32,
        ])
        self.assertEqual(parsed.command, 'ledger-migrate')
        self.assertEqual(parsed.sqlite, 'observer.sqlite3')
        self.assertEqual(parsed.confirm_source_sha256, 'ab' * 32)

    def test_mysql_connection_errors_do_not_leak_remote_credentials(self):
        config_env = {
            'SMART_MONEY_MYSQL_HOST': 'remote.example',
            'SMART_MONEY_MYSQL_PORT': '3306',
            'SMART_MONEY_MYSQL_USER': 'runtime-user',
            'SMART_MONEY_MYSQL_PASSWORD': 'config-secret-value',
            'SMART_MONEY_MYSQL_DATABASE': 'smart_money',
            'SMART_MONEY_MYSQL_SSL_CA': '/trusted/ca.pem',
        }
        with patch.dict(os.environ, config_env, clear=True), patch(
                'smart_money.mysql_config.pymysql.connect',
                side_effect=RuntimeError('server repeated config-secret-value')):
            with self.assertRaises(ValueError) as caught:
                mysql_connection()
        self.assertNotIn('config-secret-value', str(caught.exception))

        key_env = {
            **OFFLINE_ENV,
            'SMART_MONEY_KEY_MYSQL_HOST': 'keys.remote.example',
            'SMART_MONEY_KEY_MYSQL_PORT': '3306',
            'SMART_MONEY_KEY_MYSQL_USER': 'key-runtime-user',
            'SMART_MONEY_KEY_MYSQL_PASSWORD': 'key-secret-value',
            'SMART_MONEY_KEY_MYSQL_DATABASE': 'smart_money_keys',
            'SMART_MONEY_KEY_MYSQL_SSL_CA': '/trusted/key-ca.pem',
        }
        with patch.dict(os.environ, key_env, clear=True), patch(
                'smart_money.key_source.pymysql.connect',
                side_effect=RuntimeError('server repeated key-secret-value')):
            with self.assertRaises(ValueError) as caught:
                OfflineDatabaseSigner(B)._load_account()
        self.assertNotIn('key-secret-value', str(caught.exception))

    def test_mysql_relationship_gate_fails_closed_on_disable_or_snapshot_change(self):
        base = load_paper_config(ROOT / 'config/paper.example.json').relationships[0]
        policy = replace(base, follower_wallet=B, relationship_id='42',
                         snapshot_hash='ab' * 32)
        gate = MySqlRelationshipGate()
        with patch('smart_money.mysql_config.load_enabled_relationship_policy',
                   return_value=policy):
            self.assertIs(gate.validate('42', B, policy.wallet, 'ab' * 32), policy)
            with self.assertRaisesRegex(
                    ValueError, 'no longer matches execution snapshot'):
                gate.validate('42', B, policy.wallet, 'cd' * 32)
        with patch('smart_money.mysql_config.load_enabled_relationship_policy',
                   side_effect=ValueError('relationship is disabled or unavailable')):
            with self.assertRaisesRegex(ValueError, 'disabled or unavailable'):
                gate.validate('42', B, policy.wallet, 'ab' * 32)

    def test_mysql_relationship_rows_reuse_strict_paper_validation(self):
        template = json.loads((ROOT / 'config/paper.example.json').read_text())
        policy = template['wallets'][0]
        row = {
            'id': 7,
            'follower_wallet': '0x' + '11' * 20, 'follower_label': 'paper-wallet',
            'smart_wallet': '0x' + '22' * 20, 'smart_wallet_label': 'smart-a',
            'run_mode': 'paper', 'strategy_version': template['strategy_version'],
            'trigger_mode': template['trigger_mode'],
            'shadow_trigger_modes': template['shadow_trigger_modes'],
            'quote_policy': template['quote_policy'],
            'allowed_protocols': template['allowed_protocols'],
            'allowed_assets': template['allowed_assets'],
            'allowed_routes': template['allowed_routes'],
            'usdg_rule_mode': policy['buy_rules']['USDG']['mode'],
            'usdg_fixed_amount_raw': policy['buy_rules']['USDG']['fixed_amount_raw'],
            'usdg_ratio_ppm': None,
            'usdg_budget_limit_raw': policy['budget_limits']['USDG'],
            'eth_rule_mode': policy['buy_rules']['ETH_WETH']['mode'],
            'eth_fixed_amount_raw': None,
            'eth_ratio_ppm': policy['buy_rules']['ETH_WETH']['ratio_ppm'],
            'eth_budget_limit_raw': policy['budget_limits']['ETH_WETH'],
            'sell_rule_mode': policy['sell_rule']['mode'],
            'sell_fixed_amount_raw': None,
            'sell_ratio_ppm': policy['sell_rule']['ratio_ppm'],
        }
        document = rows_to_document([row])
        self.assertEqual(document['wallets'][0]['wallet'], '0x' + '22' * 20)
        self.assertEqual(document['wallets'][0]['follower_wallet'], '0x' + '11' * 20)
        self.assertEqual(document['wallets'][0]['relationship_id'], '7')
        self.assertEqual(document['wallets'][0]['run_mode'], 'paper')
        self.assertEqual(document['wallets'][0]['buy_rules']['USDG'],
                         {'mode': 'fixed', 'fixed_amount_raw': '1000000'})
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'mysql-converted.json'
            path.write_text(json.dumps(document))
            converted = load_paper_config(path)
            self.assertEqual(len(converted.snapshot_hash), 64)
            self.assertEqual(converted.wallets['0x' + '22' * 20].follower_wallet,
                             '0x' + '11' * 20)
            self.assertEqual(converted.wallets['0x' + '22' * 20].run_mode, 'paper')
        live_row = dict(row, run_mode='mainnet_live')
        live_document = rows_to_document([live_row])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'mysql-live.json'
            path.write_text(json.dumps(live_document))
            live = load_paper_config(path)
        self.assertEqual(live.relationships[0].run_mode, 'mainnet_live')
        accepted_at = datetime(2026, 9, 13, 1, 2, 3)
        accepted_row = dict(
            live_row, live_risk_accepted_at=accepted_at, updated_at=accepted_at)
        with patch('smart_money.mysql_config._load_enabled_relationship_row',
                   return_value=accepted_row):
            acceptance = load_enabled_mainnet_acceptance('7')
        self.assertEqual(acceptance['policy'].relationship_id, '7')
        self.assertEqual(acceptance['accepted_at'], accepted_at)
        second = dict(row, id=8, follower_wallet='0x' + '33' * 20)
        multi = rows_to_document([row, second])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'multi-follower.json'
            path.write_text(json.dumps(multi))
            converted = load_paper_config(path)
        self.assertEqual(len(converted.relationships), 2)
        self.assertEqual(len(converted.policies_for('0x' + '22' * 20, R.CHAIN_ID)), 2)
        self.assertNotEqual(converted.relationships[0].ledger_scope,
                            converted.relationships[1].ledger_scope)

        automatic = cli_parser().parse_args(['run'])
        self.assertEqual(automatic.command, 'run')
        self.assertTrue(automatic.paper_mysql)
        self.assertTrue(automatic.ledger_mysql)
        self.assertTrue(automatic.relay_auto_associate)
        self.assertEqual(automatic.paper_cycle_action, 'auto')
        self.assertEqual(automatic.seconds, 0)
        status = cli_parser().parse_args([
            'relationship-status', '--relationship-id', '7'])
        self.assertEqual(status.relationship_id, '7')

    def test_database_run_adds_relationship_wallet_and_reuses_budget_cycle(self):
        policy = SimpleNamespace(
            wallet=B, label='database-smart-wallet', ledger_scope='relationship-scope',
            budget_limits={'USDG': '1000'},
        )
        config = SimpleNamespace(relationships=(policy,))
        watchlist = monitoring_watchlist(ROOT / 'data/fomo_watchlist.csv', config)
        self.assertEqual(watchlist[B]['source'], 'copy_relationships')
        self.assertEqual(watchlist[B]['handle'], 'database-smart-wallet')

        store = Store(':memory:')
        cycle_id, created = prepare_runtime_budget_cycle(store, config, 'auto')
        self.assertTrue(created)
        self.assertEqual(store.active_paper_budget_cycle(), cycle_id)
        self.assertEqual(store.paper_budget('relationship-scope', 'USDG')['limit_raw'], '1000')

        store.connection.execute("""UPDATE paper_budgets SET invested_raw='400'
            WHERE cycle_id=? AND wallet=? AND bucket='USDG'""", (
                cycle_id, 'relationship-scope'))
        store.connection.commit()
        changed = SimpleNamespace(
            relationships=(SimpleNamespace(
                wallet=B, label='database-smart-wallet',
                ledger_scope='relationship-scope', budget_limits={'USDG': '1500'}),
            SimpleNamespace(
                wallet=TOKEN, label='new-smart-wallet',
                ledger_scope='new-relationship-scope', budget_limits={'USDG': '2000'})),
        )
        reused_id, reused_created = prepare_runtime_budget_cycle(store, changed, 'auto')
        self.assertFalse(reused_created)
        self.assertEqual(reused_id, cycle_id)
        budget = store.paper_budget('relationship-scope', 'USDG')
        self.assertEqual((budget['limit_raw'], budget['invested_raw']), ('1500', '400'))
        self.assertEqual(
            store.paper_budget('new-relationship-scope', 'USDG')['limit_raw'], '2000')

        lowered = SimpleNamespace(relationships=(SimpleNamespace(
            wallet=B, label='database-smart-wallet', ledger_scope='relationship-scope',
            budget_limits={'USDG': '300'}),))
        with self.assertRaisesRegex(ValueError, 'below occupied budget'):
            prepare_runtime_budget_cycle(store, lowered, 'auto')
        store.close()

    def test_database_run_validates_every_live_relationship_and_locks_per_follower(self):
        first = SimpleNamespace(
            follower_wallet=B, relationship_id='7', snapshot_hash='ab' * 32)
        same_follower = SimpleNamespace(
            follower_wallet=B, relationship_id='8', snapshot_hash='cd' * 32)
        other_follower = SimpleNamespace(
            follower_wallet=TOKEN, relationship_id='9', snapshot_hash='ef' * 32)
        policies = (first, same_follower, other_follower)
        with patch('smart_money.cli.require_mainnet_broadcast_enabled') as gate, patch(
                'smart_money.cli.live_key_record_status', return_value={
                    'found': True, 'enabled': True, 'private_key_read': False,
                }) as key_status:
            self.assertEqual(validate_live_relationships(policies), policies)
        self.assertEqual(gate.call_count, 3)
        self.assertEqual(key_status.call_count, 3)
        gate.assert_any_call(B, '7', 'ab' * 32)
        gate.assert_any_call(B, '8', 'cd' * 32)
        gate.assert_any_call(TOKEN, '9', 'ef' * 32)

        with patch('smart_money.cli.require_mainnet_broadcast_enabled'), patch(
                'smart_money.cli.live_key_record_status', side_effect=(
                    {'found': True, 'enabled': True},
                    {'found': False, 'enabled': False},
                )), self.assertRaisesRegex(ValueError, 'relationship 8'):
            validate_live_relationships((first, same_follower))

        async def exercise_locks():
            locks = live_wallet_execution_locks(policies)
            self.assertEqual(set(locks), {B, TOKEN})
            active = Counter()
            maxima = Counter()

            async def work(wallet, group):
                async with locks[wallet]:
                    active[group] += 1
                    maxima[group] = max(maxima[group], active[group])
                    await asyncio.sleep(0.01)
                    active[group] -= 1

            await asyncio.gather(work(B, 'same'), work(B, 'same'))
            await asyncio.gather(work(B, 'different'), work(TOKEN, 'different'))
            return maxima

        maxima = asyncio.run(exercise_locks())
        self.assertEqual(maxima['same'], 1)
        self.assertEqual(maxima['different'], 2)

    def test_same_smart_wallet_relationships_have_isolated_budget_and_proposals(self):
        async def scenario():
            template = json.loads((ROOT / 'config/paper.example.json').read_text())
            base = template['wallets'][0]
            base['wallet'] = A
            base['follower_wallet'], base['relationship_id'] = B, 'relationship-a'
            second = deepcopy(base)
            second['follower_wallet'], second['relationship_id'] = TOKEN, 'relationship-b'
            template['wallets'] = [base, second]
            with tempfile.TemporaryDirectory() as folder:
                path = Path(folder) / 'relationships.json'
                path.write_text(json.dumps(template))
                config = load_paper_config(path)
            store = Store(':memory:')
            store.start_paper_budget_cycle('multi-cycle', 'test')
            for policy in config.relationships:
                store.configure_paper_budget(policy.ledger_scope, 'USDG', '100000000')
            signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                            stage='swap_evidenced', execution_status='success', exact_in=True,
                            token_in=R.USDG, token_out=R.WETH, protocol='v2',
                            evidence={'route': [R.USDG, R.WETH],
                                      'actual_input_debit_raw': '1000000',
                                      'actual_output_credit_raw': '2000000'})

            class Quoter:
                async def quote_with_reference(self, source, amount):
                    return (Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, R.WETH, amount, '2000000'),
                            Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, R.WETH, '1', '2'), '1')

            decisions = []
            for policy in config.relationships:
                engine = PaperEngine(
                    store, Quoter(), config.quote_policy, config.strategy_version,
                    allowed_protocols=config.allowed_protocols,
                    allowed_assets=config.allowed_assets,
                    allowed_routes=config.allowed_routes,
                    wallet_contexts={A: {'follower_wallet': policy.follower_wallet,
                                         'relationship_id': policy.relationship_id,
                                         'ledger_scope': policy.ledger_scope}},
                    config_snapshot_hash=config.snapshot_hash)
                decisions.append(await engine.propose_buy(
                    signal, policy.buy_rules['USDG'], now=100))
            self.assertTrue(all(item.accepted for item in decisions))
            self.assertNotEqual(decisions[0].proposal_id, decisions[1].proposal_id)
            for policy in config.relationships:
                budget = store.paper_budget(policy.ledger_scope, 'USDG')
                self.assertEqual((budget['reserved_raw'], budget['invested_raw']),
                                 ('1000000', '0'))
            proposals = [store.paper_proposal(item.proposal_id) for item in decisions]
            self.assertEqual({row['source_event_id'] for row in proposals}, {signal.event_id})
            self.assertEqual({row['attribution']['follower_wallet'] for row in proposals},
                             {B, TOKEN})
            store.close()

        asyncio.run(scenario())

    def test_mysql_relationships_can_use_independent_strategy_and_risk_policy(self):
        template = json.loads((ROOT / 'config/paper.example.json').read_text())
        policy = template['wallets'][0]

        def row(row_id, follower, strategy, trigger, max_slippage):
            quote_policy = dict(template['quote_policy'], max_slippage_bps=max_slippage)
            shadows = [mode for mode in ('feed_intent', 'receipt_success', 'swap_evidenced')
                       if mode != trigger]
            return {
                'id': row_id, 'follower_wallet': follower, 'follower_label': 'follower',
                'smart_wallet': A, 'smart_wallet_label': 'smart', 'run_mode': 'paper',
                'strategy_version': strategy, 'trigger_mode': trigger,
                'shadow_trigger_modes': shadows, 'quote_policy': quote_policy,
                'allowed_protocols': template['allowed_protocols'],
                'allowed_assets': template['allowed_assets'],
                'allowed_routes': template['allowed_routes'],
                'usdg_rule_mode': 'fixed', 'usdg_fixed_amount_raw': '1000000',
                'usdg_ratio_ppm': None,
                'usdg_budget_limit_raw': policy['budget_limits']['USDG'],
                'eth_rule_mode': 'proportional', 'eth_fixed_amount_raw': None,
                'eth_ratio_ppm': 100000,
                'eth_budget_limit_raw': policy['budget_limits']['ETH_WETH'],
                'sell_rule_mode': 'proportional', 'sell_fixed_amount_raw': None,
                'sell_ratio_ppm': 100000,
            }

        rows = [row(7, B, 'strategy-a', 'swap_evidenced', 100),
                row(8, TOKEN, 'strategy-b', 'receipt_success', 500)]

        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql): self.sql = sql
            def fetchall(self): return rows

        class Connection:
            def cursor(self): return Cursor()
            def close(self): pass

        with patch('smart_money.mysql_config.mysql_connection', return_value=Connection()):
            config = load_mysql_paper_config()
        self.assertEqual([item.strategy_version for item in config.relationships],
                         ['strategy-a', 'strategy-b'])
        self.assertEqual([item.trigger_mode for item in config.relationships],
                         ['swap_evidenced', 'receipt_success'])
        self.assertEqual([item.quote_policy.max_slippage_bps
                          for item in config.relationships], [100, 500])
        self.assertNotEqual(config.relationships[0].snapshot_hash,
                            config.relationships[1].snapshot_hash)

    def test_paper_config_is_strict_secret_free_and_defaults_to_evidenced(self):
        config = load_paper_config(ROOT / 'config/paper.example.json')
        self.assertEqual(config.trigger_mode, 'swap_evidenced')
        self.assertEqual(config.shadow_trigger_modes, ('feed_intent', 'receipt_success'))
        self.assertEqual(config.allowed_protocols, frozenset({'v2', 'v3', 'v4'}))
        self.assertEqual(len(config.allowed_routes), 2)
        policy = config.wallets['0x' + '00' * 19 + '01']
        self.assertEqual(policy.label, 'replace-with-smart-wallet-label')
        self.assertEqual(policy.budget_limits['USDG'], '100000000')
        self.assertEqual(policy.buy_rules['USDG'].fixed_amount_raw, '1000000')

        document = json.loads((ROOT / 'config/paper.example.json').read_text())
        document['allowed_routes'] = []
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'dynamic-targets.json'
            path.write_text(json.dumps(document))
            dynamic = load_paper_config(path)
        self.assertEqual(dynamic.allowed_routes, frozenset())

        document['wallets'][0]['run_mode'] = 'mainnet_live'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'unbound-live.json'
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'relationship identity'):
                load_paper_config(path)

        document = json.loads((ROOT / 'config/paper.example.json').read_text())
        document['private_key'] = 'must-never-be-accepted'
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bad.json'
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'paper config fields'):
                load_paper_config(path)

    def test_paper_config_rejects_zero_relationship_wallets_but_allows_native_asset(self):
        document = json.loads((ROOT / 'config/paper.example.json').read_text())
        self.assertIn(R.NATIVE, document['allowed_assets'])
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'zero-wallet.json'
            for field, reason in (
                    ('wallet', 'zero smart wallet'),
                    ('follower_wallet', 'zero follower wallet')):
                changed = deepcopy(document)
                changed['wallets'][0][field] = R.NATIVE
                path.write_text(json.dumps(changed))
                with self.assertRaisesRegex(ValueError, reason):
                    load_paper_config(path)

    def test_paper_config_rejects_trigger_overlap_and_bucket_rule_gaps(self):
        document = json.loads((ROOT / 'config/paper.example.json').read_text())
        document['shadow_trigger_modes'].append('swap_evidenced')
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'bad-trigger.json'
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'shadow trigger'):
                load_paper_config(path)
            document['shadow_trigger_modes'] = []
            del document['wallets'][0]['buy_rules']['USDG']
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'same supported buckets'):
                load_paper_config(path)
            document['wallets'][0]['buy_rules']['USDG'] = {
                'mode': 'fixed', 'fixed_amount_raw': 100,
            }
            document['wallets'][0]['budget_limits']['USDG'] = '1000'
            path.write_text(json.dumps(document))
            with self.assertRaisesRegex(ValueError, 'fixed amount'):
                load_paper_config(path)

    def test_paper_amount_policy_uses_verified_input_and_asset_bucket(self):
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x12345678',
                        stage='swap_evidenced', token_in=R.USDG, token_out=TOKEN,
                        execution_status='success',
                        evidence={'actual_input_debit_raw': '1001'})
        self.assertEqual(budget_bucket(R.USDG, R.CHAIN_ID), 'USDG')
        self.assertEqual(budget_bucket(R.NATIVE, R.CHAIN_ID), 'ETH_WETH')
        self.assertEqual(budget_bucket(R.WETH, R.CHAIN_ID), 'ETH_WETH')
        self.assertEqual(budget_bucket(TOKEN, R.CHAIN_ID), None)
        amount, bucket = planned_input_amount(
            signal, AmountRule('proportional', ratio_ppm=100_000))
        self.assertEqual((amount, bucket), ('100', 'USDG'))
        signal.evidence.clear()
        self.assertEqual(planned_input_amount(
            signal, AmountRule('fixed', fixed_amount_raw='50')),
            ('50', 'USDG'))
        self.assertEqual(planned_input_amount(
            signal, AmountRule('proportional', ratio_ppm=100_000)),
            (None, 'verified_actual_input_missing'))
        sell = Signal(TXHASH, A, 'direct', 'SELL', 'call', R.V2_ROUTER, '0',
                      stage='swap_evidenced', token_in=TOKEN, token_out=R.USDG,
                      evidence={'actual_input_debit_raw': '301'})
        self.assertEqual(planned_input_amount(
            sell, AmountRule('proportional', ratio_ppm=500_000)), ('150', 'USDG'))

    def test_paper_trigger_modes_fail_closed(self):
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x12345678',
                        stage='swap_evidenced', execution_status='success', fresh=True)
        self.assertEqual(trigger_allowed(signal, 'swap_evidenced'), (True, None))
        signal.canonical_status = 'orphaned'
        self.assertEqual(trigger_allowed(signal, 'feed_intent'),
                         (False, 'source_signal_orphaned'))
        signal.canonical_status = 'unconfirmed'
        signal.stage = 'needs_review'
        self.assertEqual(trigger_allowed(signal, 'receipt_success'),
                         (False, 'source_signal_not_eligible'))

    def test_relay_paper_trigger_modes_require_the_exact_evidence_stage(self):
        sell = Signal(TXHASH, A, 'bundled_account', 'SELL', 'userop/0',
                      R.ZERO_X_ALLOWANCE_HOLDER, '0x2213bc0b',
                      stage='relay_sell_evidenced', execution_status='success',
                      protocol='0x', token_in=TOKEN, token_out=R.USDG)
        self.assertEqual(trigger_allowed(sell, 'relay_sell_evidenced'), (True, None))
        self.assertEqual(trigger_allowed(sell, 'relay_buy_evidenced'),
                         (False, 'relay_buy_evidenced_required'))
        buy = replace(sell, behavior='BUY', stage='relay_buy_evidenced',
                      protocol='relay_solver', token_in=R.NATIVE, token_out=TOKEN)
        self.assertEqual(trigger_allowed(buy, 'relay_buy_evidenced'), (True, None))
        self.assertEqual(trigger_allowed(buy, 'swap_evidenced'),
                         (False, 'swap_evidence_required'))
        self.assertEqual(trigger_allowed(buy, 'evidenced'), (True, None))
        self.assertEqual(trigger_allowed(sell, 'evidenced'), (True, None))
        buy.stage = 'needs_review'
        self.assertEqual(trigger_allowed(buy, 'relay_buy_evidenced'),
                         (False, 'source_signal_not_eligible'))

    def test_relay_source_routes_are_pair_scoped_and_direction_symmetric(self):
        signal = Signal(TXHASH, A, 'third_party', 'BUY', 'incoming', R.RELAY_ROUTER,
                        '0xcd6e13f7', protocol='relay_solver',
                        token_in=R.NATIVE, token_out=TOKEN)
        reverse = replace(signal, behavior='SELL', token_in=TOKEN, token_out=R.NATIVE)
        self.assertEqual(signal_route_key(signal), signal_route_key(reverse))
        self.assertIsNone(scope_reason(
            signal, frozenset({'relay_solver'}), frozenset({R.NATIVE, TOKEN}),
            frozenset({signal_route_key(signal)})))

    def test_relay_signal_uses_receipt_verified_dynamic_v3_execution_route(self):
        signal = Signal(
            TXHASH, A, 'third_party', 'BUY', 'incoming', R.RELAY_ROUTER,
            '0xcd6e13f7', stage='relay_buy_evidenced', execution_status='success',
            protocol='relay_solver', token_in=R.USDG, token_out=TOKEN,
            evidence={
                'actual_input_debit_raw': '100',
                'actual_output_credit_raw': '90',
                'local_execution_route': {
                    'protocol': 'v3', 'assets': [R.USDG, TOKEN],
                    'fees': [3000], 'verified_pool': B,
                },
            })
        execution = execution_quote_signal(signal, ())
        self.assertEqual(execution.protocol, 'v3')
        self.assertEqual(execution.evidence['hops'], [{
            'token_in': R.USDG, 'token_out': TOKEN, 'fee': 3000,
        }])
        self.assertEqual(execution.evidence['actual_input_debit_raw'], '100')

    def test_paper_asset_allowlist_covers_every_route_intermediate(self):
        middle = '0x' + '44' * 20
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V3_ROUTER, '0x',
                        protocol='v3', token_in=R.USDG, token_out=TOKEN,
                        evidence={'hops': [
                            {'token_in': R.USDG, 'token_out': middle, 'fee': 500},
                            {'token_in': middle, 'token_out': TOKEN, 'fee': 3000},
                        ]})
        self.assertEqual(scope_reason(
            signal, frozenset({'v3'}), frozenset({R.USDG, TOKEN})),
            'asset_not_allowed')
        self.assertIsNone(scope_reason(
            signal, frozenset({'v3'}), frozenset({R.USDG, middle, TOKEN})))

    def test_evidenced_meme_target_is_dynamic_but_intermediates_remain_bounded(self):
        middle = '0x' + '44' * 20
        buy = Signal(
            TXHASH, A, 'direct', 'BUY', 'call', R.V3_ROUTER, '0x',
            stage='swap_evidenced', execution_status='success', protocol='v3',
            token_in=R.USDG, token_out=TOKEN,
            evidence={'hops': [
                {'token_in': R.USDG, 'token_out': TOKEN, 'fee': 3000},
            ]})
        trusted = frozenset({R.NATIVE, R.WETH, R.USDG})
        self.assertIsNone(scope_reason(
            buy, frozenset({'v3'}), trusted, frozenset()))
        unconfirmed = replace(buy, stage='intent', execution_status='unknown')
        self.assertEqual(scope_reason(
            unconfirmed, frozenset({'v3'}), trusted, frozenset()),
            'asset_not_allowed')
        multihop = replace(buy, evidence={'hops': [
            {'token_in': R.USDG, 'token_out': middle, 'fee': 500},
            {'token_in': middle, 'token_out': TOKEN, 'fee': 3000},
        ]})
        self.assertEqual(scope_reason(
            multihop, frozenset({'v3'}), trusted, frozenset()),
            'asset_not_allowed')
        trusted_with_middle = trusted | {middle}
        self.assertIsNone(scope_reason(
            multihop, frozenset({'v3'}), trusted_with_middle, frozenset()))

        sell = replace(
            buy, behavior='SELL', token_in=TOKEN, token_out=R.USDG,
            evidence={'hops': [
                {'token_in': TOKEN, 'token_out': R.USDG, 'fee': 3000},
            ]})
        self.assertIsNone(scope_reason(
            sell, frozenset({'v3'}), trusted, frozenset()))

    def test_paper_route_allowlist_is_fee_aware_and_direction_symmetric(self):
        config = load_paper_config(ROOT / 'config/paper.example.json')
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V3_ROUTER, '0x',
                        protocol='v3', token_in=R.WETH, token_out=R.USDG,
                        evidence={'hops': [
                            {'token_in': R.WETH, 'token_out': R.USDG, 'fee': 500},
                        ]})
        self.assertIn(signal_route_key(signal), config.allowed_routes)
        self.assertIsNone(scope_reason(signal, config.allowed_protocols,
                                       config.allowed_assets, config.allowed_routes))
        reverse = replace(signal, token_in=R.USDG, token_out=R.WETH,
                          evidence={'hops': [
                              {'token_in': R.USDG, 'token_out': R.WETH, 'fee': 500},
                          ]})
        self.assertEqual(signal_route_key(reverse), signal_route_key(signal))
        wrong_fee = replace(signal, evidence={'hops': [
            {'token_in': R.WETH, 'token_out': R.USDG, 'fee': 3000},
        ]})
        self.assertEqual(scope_reason(wrong_fee, config.allowed_protocols,
                                      config.allowed_assets, config.allowed_routes),
                         'route_not_allowed')
        document = json.loads((ROOT / 'config/paper.example.json').read_text())
        document['allowed_routes'].append({
            'protocol': 'v4', 'assets': [R.NATIVE, R.USDG], 'fees': [500],
            'tick_spacings': [10], 'hooks': [R.NATIVE], 'hook_data': ['0x'],
        })
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'routes.json'
            path.write_text(json.dumps(document))
            extended = load_paper_config(path)
        v4 = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.UNIVERSAL_ROUTER, '0x',
                    protocol='v4', token_in=R.NATIVE, token_out=R.USDG,
                    evidence={'pool_key': [R.NATIVE, R.USDG, 500, 10, R.NATIVE],
                              'hook_data': '0x'})
        self.assertIsNone(scope_reason(v4, extended.allowed_protocols,
                                       extended.allowed_assets, extended.allowed_routes))
        v4.evidence['hook_data'] = '0x12'
        self.assertEqual(scope_reason(v4, extended.allowed_protocols,
                                      extended.allowed_assets, extended.allowed_routes),
                         'route_not_allowed')

    def test_paper_budget_reservation_is_atomic_idempotent_and_persistent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'paper.sqlite3'
            store = Store(path)
            store.start_paper_budget_cycle('manual-1', 'operator_started')
            very_large = str(10 ** 30)
            store.configure_paper_budget(A, 'USDG', very_large)
            proposal = {
                'proposal_id': 'p1', 'source_event_id': 'event-1',
                'source_tx_hash': TXHASH, 'wallet': A,
                'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
                'input_asset': R.USDG, 'output_asset': TOKEN,
                'budget_bucket': 'USDG', 'amount_in_raw': str(6 * 10 ** 29),
                'attribution': {'smart_wallet': A, 'source_tx_hash': TXHASH},
            }
            self.assertEqual(store.reserve_paper_proposal(proposal), (True, 'reserved'))
            self.assertEqual(store.reserve_paper_proposal(proposal),
                             (True, 'proposal_already_exists'))
            self.assertEqual(store.paper_budget(A, 'USDG')['available_raw'],
                             str(4 * 10 ** 29))
            too_large = dict(proposal, proposal_id='p2', source_event_id='event-2',
                             amount_in_raw=str(5 * 10 ** 29))
            self.assertEqual(store.reserve_paper_proposal(too_large),
                             (False, 'budget_limit_exceeded'))
            with self.assertRaisesRegex(ValueError, 'active reservations'):
                store.start_paper_budget_cycle('manual-2', 'unsafe_reset')
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.active_paper_budget_cycle(), 'manual-1')
            self.assertEqual(reopened.paper_budget(A, 'USDG')['reserved_raw'],
                             str(6 * 10 ** 29))
            reopened.close()

    def test_reserved_paper_proposal_and_source_signal_survive_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'paper-recovery.sqlite3'
            store = Store(path)
            store.start_paper_budget_cycle('manual-1', 'operator_started')
            store.configure_paper_budget(A, 'USDG', '1000')
            signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                            stage='swap_evidenced', execution_status='success',
                            token_in=R.USDG, token_out=TOKEN, protocol='v2',
                            evidence={'actual_input_debit_raw': '100',
                                      'actual_output_credit_raw': '200'})
            store.put(signal)
            proposal = {
                'proposal_id': 'recover-p', 'source_event_id': signal.event_id,
                'source_tx_hash': TXHASH, 'wallet': A,
                'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
                'input_asset': R.USDG, 'output_asset': TOKEN,
                'budget_bucket': 'USDG', 'amount_in_raw': '100',
                'attribution': {'smart_wallet': A},
            }
            store.reserve_paper_proposal(proposal)
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.reserved_paper_proposal_ids(
                'paper-v1', 'swap_evidenced'), ['recover-p'])
            recovered = reopened.signal(signal.event_id)
            self.assertEqual((recovered.event_id, recovered.stage),
                             (signal.event_id, 'swap_evidenced'))
            reopened.close()

    def test_paper_ledger_rejects_caller_supplied_asset_bucket_mismatch(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        buy = {
            'proposal_id': 'wrong-buy-bucket', 'source_event_id': 'event-1',
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.WETH, 'output_asset': TOKEN,
            'budget_bucket': 'USDG', 'amount_in_raw': '100',
            'attribution': {'smart_wallet': A},
        }
        self.assertEqual(store.reserve_paper_proposal(buy),
                         (False, 'input_asset_budget_bucket_mismatch'))
        sell = dict(buy, proposal_id='wrong-sell-bucket', source_event_id='event-2',
                    input_asset=TOKEN, output_asset=R.WETH)
        self.assertEqual(store.reserve_paper_sell(sell),
                         (False, 'sell_output_budget_bucket_mismatch'))
        self.assertEqual(store.paper_budget(A, 'USDG')['reserved_raw'], '0')
        store.close()

    def test_paper_cancel_releases_budget_and_allows_manual_new_cycle(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        proposal = {
            'proposal_id': 'p1', 'source_event_id': 'event-1',
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.USDG, 'output_asset': TOKEN,
            'budget_bucket': 'USDG', 'amount_in_raw': '600',
            'attribution': {'smart_wallet': A},
        }
        self.assertEqual(store.reserve_paper_proposal(proposal), (True, 'reserved'))
        self.assertTrue(store.cancel_paper_proposal('p1', 'quote_expired'))
        self.assertFalse(store.cancel_paper_proposal('p1', 'duplicate'))
        self.assertEqual(store.paper_budget(A, 'USDG')['available_raw'], '1000')
        store.start_paper_budget_cycle('manual-2', 'operator_reset')
        self.assertEqual(store.active_paper_budget_cycle(), 'manual-2')
        self.assertIsNone(store.paper_budget(A, 'USDG'))
        store.close()

    def test_paper_buy_fill_consumes_reservation_and_creates_attributed_lot(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'ETH_WETH', '1000')
        proposal = {
            'proposal_id': 'p1', 'source_event_id': 'event-1',
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.NATIVE, 'output_asset': TOKEN,
            'budget_bucket': 'ETH_WETH', 'amount_in_raw': '250',
            'attribution': {'smart_wallet': A, 'source_signal_stage': 'swap_evidenced'},
        }
        store.reserve_paper_proposal(proposal)
        fill = {
            'order_id': 'o1', 'fill_id': 'f1', 'lot_id': 'lot1',
            'amount_out_raw': '5000', 'fee_asset': R.NATIVE,
            'fee_amount_raw': '2', 'gas_cost_wei': '20000000',
            'quote_observed_at': '2026-09-12T00:00:00Z',
            'filled_at': '2026-09-12T00:00:01Z',
        }
        self.assertTrue(store.fill_paper_buy('p1', fill))
        self.assertTrue(store.fill_paper_buy('p1', fill))
        budget = store.paper_budget(A, 'ETH_WETH')
        self.assertEqual((budget['reserved_raw'], budget['invested_raw'],
                          budget['available_raw']), ('0', '250', '750'))
        lot = store.paper_position('lot1')
        self.assertEqual((lot['principal_remaining_raw'], lot['token_remaining_raw']),
                         ('250', '5000'))
        self.assertEqual(lot['attribution']['smart_wallet'], A)
        store.close()

    def test_paper_sell_uses_only_attributed_lot_and_restores_original_principal(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '1000')
        buy = {
            'proposal_id': 'buy-p', 'source_event_id': 'buy-event',
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.USDG, 'output_asset': TOKEN,
            'budget_bucket': 'USDG', 'amount_in_raw': '250',
            'attribution': {'smart_wallet': A},
        }
        store.reserve_paper_proposal(buy)
        store.fill_paper_buy('buy-p', {
            'order_id': 'buy-o', 'fill_id': 'buy-f', 'lot_id': 'lot1',
            'amount_out_raw': '5000', 'fee_asset': R.USDG, 'fee_amount_raw': '0',
            'gas_cost_wei': '20000000',
            'quote_observed_at': '2026-09-12T00:00:00Z',
            'filled_at': '2026-09-12T00:00:01Z',
        })
        sell = {
            'proposal_id': 'sell-p', 'source_event_id': 'sell-event',
            'source_tx_hash': '0x' + '55' * 32, 'wallet': A,
            'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': TOKEN, 'output_asset': R.USDG,
            'budget_bucket': 'USDG', 'amount_in_raw': '2500',
            'attribution': {'smart_wallet': A, 'source_sell_tx': '0x' + '55' * 32},
        }
        self.assertEqual(store.reserve_paper_sell(sell), (True, 'reserved'))
        other_wallet = dict(sell, proposal_id='wrong-wallet', source_event_id='other',
                            wallet=B, amount_in_raw='1')
        self.assertEqual(store.reserve_paper_sell(other_wallet),
                         (False, 'attributed_position_insufficient'))
        self.assertTrue(store.fill_paper_sell('sell-p', {
            'order_id': 'sell-o', 'fill_id': 'sell-f', 'amount_out_raw': '180',
            'fee_asset': R.USDG, 'fee_amount_raw': '5', 'gas_cost_wei': '7',
            'quote_observed_at': '2026-09-12T00:01:00Z',
            'filled_at': '2026-09-12T00:01:01Z',
        }))
        self.assertTrue(store.fill_paper_sell('sell-p', {
            'order_id': 'sell-o', 'fill_id': 'sell-f', 'amount_out_raw': '180',
            'fee_asset': R.USDG, 'fee_amount_raw': '5', 'gas_cost_wei': '7',
            'quote_observed_at': '2026-09-12T00:01:00Z',
            'filled_at': '2026-09-12T00:01:01Z',
        }))
        lot = store.paper_position('lot1')
        self.assertEqual((lot['token_remaining_raw'], lot['principal_remaining_raw']),
                         ('2500', '125'))
        budget = store.paper_budget(A, 'USDG')
        self.assertEqual((budget['invested_raw'], budget['available_raw']), ('125', '875'))
        pnl = store.paper_realized_pnl('sell-f')[0]
        self.assertEqual((pnl['principal_released_raw'], pnl['realized_pnl_raw'],
                          pnl['gas_cost_wei']), ('125', '50', '7'))
        cancel = dict(sell, proposal_id='sell-cancel', source_event_id='sell-cancel-event',
                      amount_in_raw='100')
        self.assertEqual(store.reserve_paper_sell(cancel), (True, 'reserved'))
        self.assertTrue(store.cancel_paper_proposal('sell-cancel', 'quote_expired'))
        retry = dict(sell, proposal_id='sell-retry', source_event_id='sell-retry-event',
                     amount_in_raw='2500')
        self.assertEqual(store.reserve_paper_sell(retry), (True, 'reserved'))
        store.close()

    def test_proportional_sell_maps_source_fraction_to_local_attributed_quantity(self):
        store = Store(':memory:')
        store.start_paper_budget_cycle('manual-1', 'operator_started')
        store.configure_paper_budget(A, 'USDG', '10000000')
        store.reserve_paper_proposal({
            'proposal_id': 'buy-map', 'source_event_id': 'source-buy-map',
            'source_tx_hash': TXHASH, 'wallet': A,
            'trigger_mode': 'relay_buy_evidenced', 'strategy_version': 'paper-v1',
            'input_asset': R.USDG, 'output_asset': TOKEN,
            'budget_bucket': 'USDG', 'amount_in_raw': '2000000',
            'attribution': {
                'smart_wallet': A, 'source_event_id': 'source-buy-map',
                'source_amount_out_raw': '69405773920665976786',
            },
        })
        store.fill_paper_buy('buy-map', {
            'order_id': 'buy-map-o', 'fill_id': 'buy-map-f', 'lot_id': 'buy-map-lot',
            'amount_out_raw': '50303912913447597330', 'fee_asset': R.USDG,
            'fee_amount_raw': '0', 'gas_cost_wei': '1',
            'quote_observed_at': '2026-09-13T00:00:00Z',
            'filled_at': '2026-09-13T00:00:01Z',
        })
        half, reason = store.paper_proportional_sell_amount(
            A, TOKEN, '34702886960332988393', 1_000_000)
        self.assertEqual((half, reason), ('25151956456723798665', 'selected'))
        full, reason = store.paper_proportional_sell_amount(
            A, TOKEN, '69405773920665976786', 1_000_000)
        self.assertEqual((full, reason), ('50303912913447597330', 'selected'))
        self.assertEqual(store.paper_open_position_amount(A, TOKEN),
                         '50303912913447597330')
        store.close()

    def test_receipt_coverage_does_not_double_count_intent_updates(self):
        stats = Counter(receipt_signals=4, receipt_unknown=1,
                        receipt_needs_review=2, receipt_swap_evidenced=1,
                        receipt_relay_sell_evidenced=1,
                        receipt_relay_buy_evidenced=0,
                        intent_signals=99)
        self.assertEqual(coverage_summary(stats), {
            "receipt_signals": 4, "unknown": 1, "needs_review": 2,
            "swap_evidenced": 1, "relay_sell_evidenced": 1,
            "relay_buy_evidenced": 0, "unknown_fraction": 0.25,
        })

    def test_signal_upgrades_to_safe_head_but_not_finality(self):
        store = Store(":memory:")
        block_hash = "0x" + "cc" * 32
        signal = Signal(TXHASH, A, "self", "TRANSFER", "x", TOKEN, "0x",
                        stage="execution_observed", evidence={"block_number": 10,
                        "block_hash": block_hash, "canonicality": "not_rechecked_for_reorgs"})
        store.put(signal)
        store.put_candidate(tx(b""))
        store.record_chain_block(10, block_hash, "0x" + "bb" * 32)
        store.complete_candidate(TXHASH, 10, block_hash)
        row = next(store.rows())
        self.assertEqual(row["canonical_status"], "safe_head_confirmed")
        self.assertEqual(row["evidence"]["canonicality"],
                         "safe_head_hash_rechecked_not_l1_finality")
        store.close()

    def test_solver_delivery_requires_exact_order_and_wallet_evidence(self):
        store = Store(":memory:")
        order_id = "0x" + "77" * 32
        deposit = Signal(TXHASH, A, "self", "INTENT_DEPOSIT", "deposit", R.DEPOSITORY,
                         "0xe8017952", stage="execution_observed", token_in=R.USDG,
                         amount_in_raw="100", evidence={"order_id": order_id,
                         "solver_order_status": "source_deposit_evidenced"})
        self.assertTrue(store.put(deposit))
        self.assertEqual(len(store.solver_order(order_id)), 1)
        self.assertEqual(store.record_solver_delivery("0x" + "88" * 32, A, "0x" + "99" * 32, {}),
                         "source_order_not_uniquely_evidenced")
        self.assertEqual(store.record_solver_delivery(order_id, B, "0x" + "99" * 32, {}),
                         "delivery_wallet_mismatch")
        self.assertEqual(store.record_solver_delivery(
            order_id, A, "0x" + "99" * 32, {"proof": "explicit_order_event"}),
            "order_delivery_linked")
        self.assertEqual([row["kind"] for row in store.solver_order(order_id)],
                         ["destination_delivery", "source_deposit"])
        store.close()

    def test_relay_associate_is_idempotent_and_reorg_orphans_buy(self):
        with tempfile.TemporaryDirectory() as folder:
            db = Path(folder) / "observer.sqlite3"
            document = Path(folder) / "relay.json"
            document.write_text(json.dumps(relay_buy_document()))
            parent_hash, block_hash = "0x" + "cc" * 32, "0x" + "dd" * 32
            candidate = passive_candidate()
            candidate.evidence.update({"block_number": 10, "block_hash": block_hash})
            store = Store(db)
            store.record_chain_block(9, parent_hash, "0x" + "bb" * 32)
            store.record_chain_block(10, block_hash, parent_hash)
            store.put_candidate(tx(b""))
            store.put(candidate)
            store.complete_candidate(TXHASH, 10, block_hash)
            store.close()
            args = SimpleNamespace(db=str(db), ledger_mysql=False,
                                   event_id=candidate.event_id, document=str(document))
            with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                relay_associate(args)
                relay_associate(args)
            reopened = Store(db)
            associated = reopened.signal(candidate.event_id)
            self.assertEqual((associated.behavior, associated.stage),
                             ("BUY", "relay_buy_evidenced"))
            self.assertEqual(sum(1 for _ in reopened.rows()), 1)
            reopened.rewind_chain(9, parent_hash)
            orphaned = reopened.signal(candidate.event_id)
            self.assertEqual(orphaned.canonical_status, "orphaned")
            self.assertFalse(orphaned.copy_eligible)
            reopened.close()

    def test_reorg_removes_solver_execution_evidence(self):
        store = Store(":memory:")
        order_id = "0x" + "77" * 32
        parent_hash, orphan_hash = "0x" + "bb" * 32, "0x" + "cc" * 32
        store.record_chain_block(9, parent_hash, "0x" + "aa" * 32)
        store.record_chain_block(10, orphan_hash, parent_hash)
        source = tx(b"")
        store.put_candidate(source)
        deposit = Signal(TXHASH, A, "self", "INTENT_DEPOSIT", "deposit", R.DEPOSITORY,
                         "0xe8017952", stage="execution_observed", token_in=R.USDG,
                         amount_in_raw="100", evidence={"order_id": order_id,
                         "block_hash": orphan_hash, "block_number": 10,
                         "solver_order_status": "source_deposit_evidenced"})
        store.put(deposit)
        store.complete_candidate(TXHASH, 10, orphan_hash)
        self.assertEqual(len(store.solver_order(order_id)), 1)
        store.rewind_chain(9, parent_hash)
        self.assertEqual(store.solver_order(order_id), [])
        store.close()

    def test_endpoint_env_loader_ignores_wallet_secrets(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, {}, clear=True):
            path = Path(folder) / '.env'
            path.write_text('ROBINHOOD_RPC_URL="https://paid.example/key"\nPRIVATE_KEY=never-load\n')
            self.assertEqual(load_endpoint_env(path), {'ROBINHOOD_RPC_URL'})
            self.assertEqual(os.environ['ROBINHOOD_RPC_URL'], 'https://paid.example/key')
            self.assertNotIn('PRIVATE_KEY', os.environ)

    def test_rpc_broadcast_forbidden(self):
        rpc = ReadOnlyRpc('https://example.com')
        with self.assertRaises(PermissionError):
            asyncio.run(rpc.call('eth_sendRawTransaction', ['0x00']))

    def test_rpc_custom_debug_tracer_forbidden(self):
        rpc = ReadOnlyRpc('http://localhost:8545')
        with self.assertRaises(PermissionError):
            asyncio.run(rpc.call('debug_traceTransaction', [TXHASH, {'tracer': 'callTracer'}]))

    def test_remote_plain_http_rejected(self):
        with self.assertRaises(ValueError):
            ReadOnlyRpc('http://example.com')

    def test_rpc_errors_do_not_leak_endpoint(self):
        rpc = ReadOnlyRpc('https://example.com/secret-token')
        with patch.object(rpc.transport, 'request', side_effect=OSError('secret-token')):
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

    def test_durable_candidates_survive_queue_pressure(self):
        store = Store(':memory:')
        first = tx(b'first')
        second = replace(tx(b'second'), hash='0x' + 'bb' * 32)
        self.assertTrue(store.put_candidate(first))
        self.assertTrue(store.put_candidate(second))
        self.assertFalse(store.put_candidate(first))
        queue = asyncio.Queue(maxsize=1)
        stats = Counter()
        self.assertEqual(dispatch_pending(store, queue, stats), 1)
        self.assertEqual(store.candidate_counts()['pending'], 1)
        queued, queued_at = queue.get_nowait()
        self.assertEqual(queued.hash, first.hash)
        self.assertIsInstance(queued_at, float)
        queue.task_done()
        store.complete_candidate(first.hash)
        self.assertEqual(dispatch_pending(store, queue, stats), 1)
        self.assertEqual(queue.get_nowait()[0].hash, second.hash)
        self.assertEqual(stats['candidates_dispatched'], 2)
        store.close()

    def test_inflight_candidate_recovers_after_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'observer.sqlite3'
            store = Store(path)
            source = replace(tx(b'payload'), value=123, sequence=9, timestamp=10)
            store.put_candidate(source)
            claimed = store.claim_candidates(1, now=100)
            self.assertEqual(claimed, [source])
            store.close()
            reopened = Store(path)
            self.assertEqual(reopened.candidate_counts()['queued'], 1)
            self.assertEqual(reopened.recover_inflight(), 1)
            self.assertEqual(reopened.claim_candidates(1, now=100), [source])
            reopened.complete_candidate(source.hash)
            self.assertEqual(reopened.claim_candidates(1, now=100), [])
            reopened.close()

    def test_candidate_retry_uses_bounded_backoff(self):
        store = Store(':memory:')
        source = tx(b'payload')
        store.put_candidate(source)
        store.claim_candidates(1, now=100)
        attempts, delay = store.retry_candidate(source.hash, 'receipt_unavailable', now=100)
        self.assertEqual((attempts, delay), (1, 2.0))
        self.assertEqual(store.claim_candidates(1, now=101), [])
        self.assertEqual(store.claim_candidates(1, now=102), [source])
        now = 102
        for expected_attempt in range(2, MAX_CANDIDATE_ATTEMPTS + 1):
            attempts, delay = store.retry_candidate(source.hash, 'receipt_unavailable', now=now)
            self.assertEqual(attempts, expected_attempt)
            if expected_attempt == MAX_CANDIDATE_ATTEMPTS:
                self.assertIsNone(delay)
                break
            now += delay
            self.assertEqual(store.claim_candidates(1, now=now), [source])
        self.assertEqual(store.candidate_counts()['failed'], 1)
        store.close()

    def test_latency_summary_is_bounded_and_uses_percentiles(self):
        samples = LatencySamples(limit=3)
        for value in (0.001, 0.002, 0.003, 9.0):
            samples.observe('queue_wait_ms', value)
        summary = samples.summary()['queue_wait_ms']
        self.assertEqual(summary, {'count': 3, 'p50': 2.0, 'p95': 3.0, 'p99': 3.0})

    def test_chain_cursor_is_independent_and_cannot_silently_rewind(self):
        store = Store(':memory:')
        self.assertIsNone(store.chain_cursor())
        store.set_chain_cursor(100, '0x' + 'aa' * 32)
        self.assertEqual(store.chain_cursor(), (100, '0x' + 'aa' * 32))
        store.set_chain_cursor(101, '0x' + 'BB' * 32)
        self.assertEqual(store.chain_cursor(), (101, '0x' + 'bb' * 32))
        with self.assertRaisesRegex(ValueError, 'explicit reorg handling'):
            store.set_chain_cursor(99, '0x' + 'cc' * 32)
        store.close()

    @staticmethod
    def _range_rpc(latest, sender, hit_height=101, log_side='from', hit_hash=TXHASH):
        """Fake RPC for the address-filtered range scanner (headers, logs, transactions)."""
        class Rpc:
            def __init__(self):
                self.latest = latest
                self.calls = []

            async def call(self, method, params=None):
                self.calls.append((method, params))
                if method == 'eth_blockNumber':
                    return hex(self.latest)
                if method == 'eth_getLogs':
                    query = params[0]
                    start, end = int(query['fromBlock'], 16), int(query['toBlock'], 16)
                    topics = query['topics']
                    matched = (log_side == 'from' and topics[1] and addr_topic(A) in topics[1]) or (
                        log_side == 'to' and topics[2] and addr_topic(A) in topics[2])
                    if matched and start <= hit_height <= end:
                        return [{'transactionHash': hit_hash, 'removed': False,
                                 'blockNumber': hex(hit_height),
                                 'topics': [TRANSFER, addr_topic(sender), addr_topic(A)]}]
                    return []
                if method == 'eth_getTransactionByHash':
                    return {'hash': hit_hash, 'from': sender, 'to': TOKEN, 'input': '0x',
                            'value': '0x0', 'chainId': hex(R.CHAIN_ID), 'nonce': '0x1',
                            'type': '0x2', 'blockNumber': hex(hit_height),
                            'blockHash': '0x' + format(hit_height, '064x')}
                height = int(params[0], 16)
                self.calls.append(('full_block', params[1]))
                return {'number': hex(height), 'hash': '0x' + format(height, '064x'),
                        'parentHash': '0x' + format(height - 1, '064x'), 'timestamp': '0x64'}
        return Rpc()

    def test_block_scanner_initializes_then_backfills_rpc_visible_candidate(self):
        store = Store(':memory:')
        rpc = self._range_rpc(latest=102, sender=A)
        progress = []
        scanner = BlockScanner(rpc, store, {A: {}}, confirmations=2, max_blocks=2000,
                               progress=lambda candidates, passive: progress.append((candidates, passive)))
        self.assertTrue(asyncio.run(scanner.scan_once()).initialized)
        self.assertEqual(store.chain_cursor()[0], 100)
        rpc.latest = 103
        result = asyncio.run(scanner.scan_once())
        self.assertEqual((result.blocks, result.candidates, result.passive_candidates), (1, 1, 0))
        self.assertEqual(progress, [(1, 0)])
        self.assertEqual(store.chain_cursor()[0], 101)
        # No full blocks are ever requested; only headers, logs and matched transactions.
        self.assertNotIn(('full_block', True), rpc.calls)
        candidate = store.claim_candidates(1)[0]
        self.assertEqual((candidate.observation_source, candidate.fresh), ('backfill', False))
        store.close()

    def test_block_scanner_range_covers_many_blocks_with_few_calls(self):
        store = Store(':memory:')
        rpc = self._range_rpc(latest=100, sender=A, hit_height=1500)
        scanner = BlockScanner(rpc, store, {A: {}}, confirmations=0, max_blocks=2000)
        asyncio.run(scanner.scan_once())
        rpc.latest = 5000
        result = asyncio.run(scanner.scan_once())
        self.assertEqual((result.blocks, result.candidates), (2000, 1))
        self.assertEqual(store.chain_cursor()[0], 2100)
        self.assertEqual(store.chain_block_hash(1500), '0x' + format(1500, '064x'))
        self.assertIsNone(store.chain_block_hash(1499))
        methods = Counter(method for method, _ in rpc.calls if method != 'full_block')
        self.assertEqual(methods['eth_getLogs'], 2)
        self.assertLessEqual(methods['eth_getBlockByNumber'], 4)
        store.close()

    def test_block_scanner_finds_passive_transfer_recipient(self):
        store = Store(':memory:')
        rpc = self._range_rpc(latest=102, sender=B, log_side='to')
        progress = []
        scanner = BlockScanner(rpc, store, {A: {}}, confirmations=2, max_blocks=2000,
                               progress=lambda candidates, passive: progress.append((candidates, passive)))
        asyncio.run(scanner.scan_once())
        rpc.latest = 103
        result = asyncio.run(scanner.scan_once())
        self.assertEqual((result.candidates, result.passive_candidates), (1, 1))
        self.assertEqual(progress, [(1, 1)])
        candidate = store.claim_candidates(1)[0]
        self.assertEqual((candidate.sender, candidate.observation_source, candidate.fresh),
                         (B, 'backfill', False))
        store.close()

    def test_block_scanner_halts_on_parent_hash_mismatch(self):
        class Rpc:
            async def call(self, method, params=None):
                if method == 'eth_blockNumber':
                    return '0x65'
                if method == 'eth_getLogs':
                    return []
                height = int(params[0], 16)
                return {'number': hex(height),
                        'hash': '0x' + ('aa' if height == 100 else 'bb') * 32,
                        'parentHash': '0x' + ('00' if height == 100 else 'cc') * 32,
                        'timestamp': '0x64', 'transactions': []}

        store = Store(':memory:')
        store.set_chain_cursor(100, '0x' + 'aa' * 32)
        with self.assertRaises(ReorgDetected):
            asyncio.run(BlockScanner(Rpc(), store, {A: {}}, confirmations=0).scan_once())
        self.assertEqual(store.chain_cursor(), (100, '0x' + 'aa' * 32))
        store.close()

    def test_block_scanner_splits_ranges_the_rpc_refuses(self):
        from smart_money.rpc import RpcError

        class Rpc:
            def __init__(self):
                self.ranges = []

            async def call(self, method, params=None):
                if method == 'eth_blockNumber':
                    return hex(108)
                if method == 'eth_getLogs':
                    start, end = int(params[0]['fromBlock'], 16), int(params[0]['toBlock'], 16)
                    self.ranges.append((start, end))
                    if end - start + 1 > 4:
                        raise RpcError('query returned more than 10000 results')
                    return []
                height = int(params[0], 16)
                return {'number': hex(height), 'hash': '0x' + format(height, '064x'),
                        'parentHash': '0x' + format(height - 1, '064x'), 'timestamp': '0x64'}

        store = Store(':memory:')
        store.set_chain_cursor(100, '0x' + format(100, '064x'))
        rpc = Rpc()
        result = asyncio.run(BlockScanner(rpc, store, {A: {}}, confirmations=0, max_blocks=8).scan_once())
        self.assertEqual(result.blocks, 8)
        self.assertEqual(store.chain_cursor()[0], 108)
        self.assertIn((101, 108), rpc.ranges)
        self.assertIn((101, 104), rpc.ranges)
        self.assertIn((105, 108), rpc.ranges)
        store.close()

    def test_reorg_preserves_intent_and_orphans_execution_evidence(self):
        old100, old101 = '0x' + 'aa' * 32, '0x' + 'bb' * 32

        class Rpc:
            async def call(self, method, params=None):
                height = int(params[0], 16)
                current_hash = old100 if height == 100 else '0x' + 'dd' * 32
                return {'number': hex(height), 'hash': current_hash,
                        'parentHash': '0x' + '00' * 32, 'transactions': []}

        store = Store(':memory:')
        store.record_chain_block(100, old100, '0x' + '99' * 32)
        store.record_chain_block(101, old101, old100)
        source = tx(b'payload')
        store.put_candidate(source)
        signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', TOKEN, '0x12345678',
                        stage='execution_observed', execution_status='success',
                        evidence={'block_hash': old101, 'block_number': 101})
        store.put(signal)
        store.complete_candidate(source.hash, 101, old101)
        resolution = asyncio.run(BlockScanner(Rpc(), store, {A: {}}, confirmations=0)
                                 .reconcile_reorg())
        self.assertEqual((resolution.common_ancestor, resolution.orphaned_signals,
                          resolution.candidates_requeued), (100, 1, 1))
        row = list(store.rows())[0]
        self.assertEqual(row['intent_status'], 'observed')
        self.assertEqual(row['execution_status'], 'success')
        self.assertEqual(row['canonical_status'], 'orphaned')
        self.assertEqual(store.candidate_counts()['pending'], 1)
        store.close()

    def test_explicit_deep_reorg_recovery_can_search_beyond_automatic_limit(self):
        hashes = {height: '0x' + format(height, '064x') for height in range(30, 101)}

        class Rpc:
            async def call(self, method, params=None):
                height = int(params[0], 16)
                canonical_hash = hashes[height] if height == 30 else '0x' + 'ff' * 32
                return {'number': hex(height), 'hash': canonical_hash,
                        'parentHash': '0x' + '00' * 32, 'transactions': []}

        store = Store(':memory:')
        for height in range(30, 101):
            store.record_chain_block(height, hashes[height],
                                     hashes.get(height - 1, '0x' + '00' * 32))
        scanner = BlockScanner(Rpc(), store, {A: {}}, confirmations=0)
        with self.assertRaisesRegex(ReorgDetected, 'within 64 blocks'):
            asyncio.run(scanner.reconcile_reorg(max_depth=64))
        self.assertEqual(store.chain_cursor(), (100, hashes[100]))
        resolution = asyncio.run(scanner.reconcile_reorg(max_depth=100))
        self.assertEqual(resolution.common_ancestor, 30)
        self.assertEqual(store.chain_cursor(), (30, hashes[30]))
        self.assertIsNone(store.chain_block_hash(31))
        store.close()

    def test_duplicate_watchlist_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'watch.csv'
            path.write_text('real_evm\n' + A + '\n' + A + '\n')
            with self.assertRaises(ValueError):
                R.load_watchlist(path)


class AggregatorExecutionTests(unittest.TestCase):
    """Kyber execution provider: allowlisted router, decoded recipient, simulation gate."""

    FIXTURE = ROOT / 'data/kyber_route_build_sample_2026-09-13.json'
    FOLLOWER = '0x3004ab92565deeea0a2eaa27e40e297bb457e1a6'
    TOKEN_OUT = '0x462dff4be800c77a61e69dc2ea6010e4237f674d'

    def _documents(self):
        sample = json.loads(self.FIXTURE.read_text())
        return {'routes': sample['routes'], 'route/build': sample['build']}

    def _client(self, documents):
        from smart_money.kyber import KyberAggregatorClient
        client = KyberAggregatorClient()
        client._request = lambda path, query=None, body=None: deepcopy(documents[path])
        return client

    def _swap(self, documents=None, follower=None):
        from smart_money.kyber import KyberAggregatorClient  # noqa: F401
        documents = documents or self._documents()
        client = self._client(documents)

        async def scenario():
            route = await client.route(R.USDG, self.TOKEN_OUT, '100000')
            return await client.build(route, follower or self.FOLLOWER, 300, 4102444800)
        return asyncio.run(scenario())

    def _signal(self, protocol='kyber', **overrides):
        values = dict(
            stage='relay_buy_evidenced', execution_status='success', exact_in=True,
            token_in=R.USDG, token_out=self.TOKEN_OUT, protocol=protocol,
            evidence={'actual_input_debit_raw': '2000000',
                      'actual_output_credit_raw': '3000000000000000000000'})
        values.update(overrides)
        return Signal(TXHASH, A, 'third_party', 'BUY', 'incoming',
                      R.RELAY_PROXY, '0x0a2b8f36', **values)

    def test_kyber_client_verifies_router_pair_amount_and_recipient(self):
        from smart_money.kyber import KyberApiError, decode_kyber_swap
        swap = self._swap()
        self.assertEqual(swap.to, R.KYBER_META_AGGREGATION_ROUTER_V2)
        self.assertEqual((swap.input_asset, swap.output_asset, swap.amount_in_raw,
                          swap.recipient, swap.value_raw),
                         (R.USDG, self.TOKEN_OUT, '100000', self.FOLLOWER, '0'))
        self.assertLessEqual(int(swap.minimum_amount_out_raw), int(swap.amount_out_raw))
        decoded = decode_kyber_swap(swap.data)
        self.assertEqual((decoded['src_token'], decoded['dst_token'], decoded['dst_receiver'],
                          decoded['amount_raw'], decoded['minimum_amount_out_raw']),
                         (R.USDG, self.TOKEN_OUT, self.FOLLOWER, '100000',
                          swap.minimum_amount_out_raw))
        self.assertEqual(swap.public_evidence()['provider'], 'kyber')
        self.assertNotIn('data', swap.public_evidence())
        bad_router = self._documents()
        bad_router['routes']['data']['routerAddress'] = R.V3_ROUTER
        with self.assertRaisesRegex(KyberApiError, 'allowlist'):
            self._swap(bad_router)
        bad_amount = self._documents()
        bad_amount['routes']['data']['routeSummary']['amountIn'] = '100001'
        with self.assertRaisesRegex(KyberApiError, 'input does not match'):
            self._swap(bad_amount)
        with self.assertRaisesRegex(KyberApiError, 'calldata does not match'):
            self._swap(follower=B)
        bad_value = self._documents()
        bad_value['route/build']['data']['transactionValue'] = '1'
        with self.assertRaisesRegex(KyberApiError, 'native value'):
            self._swap(bad_value)
        bad_selector = self._documents()
        bad_selector['route/build']['data']['data'] = (
            '0xdeadbeef' + bad_selector['route/build']['data']['data'][10:])
        with self.assertRaisesRegex(KyberApiError, 'not a Kyber router swap'):
            self._swap(bad_selector)
        bad_out = self._documents()
        bad_out['route/build']['data']['amountOut'] = '1'
        with self.assertRaisesRegex(KyberApiError, 'calldata does not match'):
            self._swap(bad_out)

    def test_execution_quote_signal_selects_only_matching_aggregator_definition(self):
        from smart_money.paper import aggregator_route_definition
        signal = self._signal(protocol='relay_solver')
        signal.evidence['local_execution_route'] = aggregator_route_definition(
            R.USDG, self.TOKEN_OUT, 'kyber', R.CHAIN_ID)
        selected = execution_quote_signal(signal, None)
        self.assertEqual((selected.protocol, selected.contract, selected.exact_in),
                         ('kyber', R.KYBER_META_AGGREGATION_ROUTER_V2, True))
        self.assertEqual(selected.evidence['aggregator_provider'], 'kyber')
        self.assertIsNone(scope_reason(
            selected, frozenset({'kyber', 'relay_solver'}), frozenset({R.USDG})))
        tampered = deepcopy(signal)
        tampered.evidence['local_execution_route']['router'] = R.V3_ROUTER
        with self.assertRaisesRegex(ValueError, 'does not select one'):
            execution_quote_signal(tampered, None)
        with self.assertRaisesRegex(ValueError, 'unsupported aggregator'):
            aggregator_route_definition(R.USDG, self.TOKEN_OUT, 'okx', R.CHAIN_ID)
        with self.assertRaisesRegex(ValueError, 'ERC-20'):
            aggregator_route_definition(R.NATIVE, self.TOKEN_OUT, 'kyber', R.CHAIN_ID)
        reversed_signal = reverse_quote_signal(selected, R.USDG)
        self.assertEqual((reversed_signal.behavior, reversed_signal.token_in,
                          reversed_signal.token_out), ('SELL', self.TOKEN_OUT, R.USDG))

    def test_paper_engine_falls_back_to_kyber_only_when_enabled(self):
        token = self.TOKEN_OUT

        class Quoter:
            def __init__(self, enabled):
                self.aggregators = {'kyber': object()} if enabled else {}

            async def discover_v3_route(self, signal, amount):
                raise ValueError('no verified quotable direct V3 pool')

            async def quote_with_reference(self, signal, amount):
                per_unit = 1_500_000_000_000_000
                return (Quote(signal.protocol, R.KYBER_META_AGGREGATION_ROUTER_V2, 10,
                              '0x' + 'ab' * 32, 100.0, R.USDG, token, amount,
                              str(int(amount) * per_unit), '356167'),
                        Quote(signal.protocol, R.KYBER_META_AGGREGATION_ROUTER_V2, 10,
                              '0x' + 'ab' * 32, 100.0, R.USDG, token, '1000',
                              str(1000 * per_unit), '356167'), '100000000')

        def scenario(providers, enabled):
            store = Store(':memory:')
            store.start_paper_budget_cycle('cycle', 'test')
            store.configure_paper_budget(A, 'USDG', '1000000')
            signal = self._signal(protocol='relay_solver')
            store.put(signal)
            engine = PaperEngine(
                store, Quoter(enabled), QuotePolicy(), 'agg-v1', 'evidenced',
                frozenset({'kyber', 'relay_solver', 'v3'}), frozenset({R.USDG}),
                frozenset(), execution_providers=providers)
            decision = asyncio.run(engine.propose_buy(
                signal, AmountRule('fixed', fixed_amount_raw='100000'), now=100.5))
            payload = store.connection.execute(
                'SELECT payload FROM paper_decisions WHERE decision_id=?',
                (decision.decision_id,)).fetchone()[0]
            proposal = (store.paper_proposal(decision.proposal_id)
                        if decision.proposal_id else None)
            store.close()
            return decision, json.loads(payload), proposal

        decision, payload, proposal = scenario(('local', 'kyber'), True)
        self.assertTrue(decision.accepted, decision.reason)
        self.assertEqual(payload['execution_provider'], 'kyber')
        self.assertEqual(payload['execution_signal']['protocol'], 'kyber')
        self.assertEqual(proposal['attribution']['local_execution_route']['provider'], 'kyber')
        self.assertEqual(proposal['amount_in_raw'], '100000')
        decision, payload, _ = scenario(('local',), True)
        self.assertEqual((decision.accepted, decision.reason), (False, 'quote_unavailable'))
        self.assertIn('no verified quotable direct V3 pool', payload['quote_error'])
        decision, payload, _ = scenario(('local', 'kyber'), False)
        self.assertEqual((decision.accepted, decision.reason), (False, 'quote_unavailable'))
        decision, payload, _ = scenario(('kyber',), True)
        self.assertTrue(decision.accepted, decision.reason)
        self.assertEqual(payload['execution_provider'], 'kyber')

    def test_aggregator_plan_requires_router_recipient_and_minimum_floor(self):
        from smart_money.execution_prep import build_aggregator_execution_plan
        swap = self._swap()
        signal = self._signal()
        quote = Quote('kyber', R.KYBER_META_AGGREGATION_ROUTER_V2, 10, '0x' + 'ab' * 32,
                      100.0, R.USDG, self.TOKEN_OUT, '100000', swap.amount_out_raw, '356167')
        floor = str(int(swap.minimum_amount_out_raw) - 1)
        plan = build_aggregator_execution_plan(
            signal, self.FOLLOWER, '2', 'proposal-agg', quote, floor, swap, 600000,
            '200', '0', frozenset({'kyber'}), frozenset({R.USDG}), frozenset())
        self.assertEqual((plan.to, plan.execution_provider, plan.value_raw,
                          plan.minimum_amount_out_raw, plan.deadline),
                         (R.KYBER_META_AGGREGATION_ROUTER_V2, 'kyber', '0',
                          swap.minimum_amount_out_raw, 4102444800))
        plan.validate(frozenset({R.KYBER_META_AGGREGATION_ROUTER_V2}), now=100.5)
        rounding_floor = str(int(swap.minimum_amount_out_raw) + 1)
        self.assertEqual(build_aggregator_execution_plan(
            signal, self.FOLLOWER, '2', 'proposal-agg', quote, rounding_floor, swap,
            600000, '200', '0', frozenset({'kyber'}), frozenset({R.USDG}),
            frozenset()).minimum_amount_out_raw, swap.minimum_amount_out_raw)
        with self.assertRaisesRegex(ValueError, 'below the plan slippage floor'):
            build_aggregator_execution_plan(
                signal, self.FOLLOWER, '2', 'proposal-agg', quote,
                str(int(swap.minimum_amount_out_raw) + 2), swap, 600000, '200', '0',
                frozenset({'kyber'}), frozenset({R.USDG}), frozenset())
        with self.assertRaisesRegex(ValueError, 'does not match the execution plan'):
            build_aggregator_execution_plan(
                signal, B, '2', 'proposal-agg', quote, floor, swap, 600000, '200', '0',
                frozenset({'kyber'}), frozenset({R.USDG}), frozenset())
        with self.assertRaisesRegex(ValueError, 'not allowlisted'):
            build_aggregator_execution_plan(
                signal, self.FOLLOWER, '2', 'proposal-agg', quote, floor,
                replace(swap, to=R.V3_ROUTER), 600000, '200', '0',
                frozenset({'kyber'}), frozenset({R.USDG}), frozenset())
        with self.assertRaisesRegex(ValueError, 'protocol_not_allowed'):
            build_aggregator_execution_plan(
                signal, self.FOLLOWER, '2', 'proposal-agg', quote, floor, swap, 600000,
                '200', '0', frozenset({'v3'}), frozenset({R.USDG}), frozenset())
        local_plan = replace(plan, execution_provider='local')
        with self.assertRaisesRegex(ValueError, 'targets an aggregator router'):
            local_plan.validate(frozenset({R.KYBER_META_AGGREGATION_ROUTER_V2}), now=100.5)

    def test_aggregator_simulation_gate_fails_closed(self):
        from smart_money.execution_prep import (
            build_aggregator_execution_plan, simulate_aggregator_execution,
        )
        from smart_money.rpc import RpcError
        swap = self._swap()
        signal = self._signal()
        quote = Quote('kyber', R.KYBER_META_AGGREGATION_ROUTER_V2, 10, '0x' + 'ab' * 32,
                      100.0, R.USDG, self.TOKEN_OUT, '100000', swap.amount_out_raw, '356167')
        plan = build_aggregator_execution_plan(
            signal, self.FOLLOWER, '2', 'proposal-agg', quote, swap.minimum_amount_out_raw,
            swap, 600000, '200', '0', frozenset({'kyber'}), frozenset({R.USDG}), frozenset())
        minimum = int(plan.minimum_amount_out_raw)

        class Rpc:
            def __init__(self, result=None, error=None):
                self.result, self.error, self.calls = result, error, []

            async def call(self, method, params=None):
                self.calls.append((method, params))
                if self.error:
                    raise self.error
                return self.result

        rpc = Rpc('0x' + encode(['uint256', 'uint256'], [minimum, 300000]).hex())
        evidence = asyncio.run(simulate_aggregator_execution(rpc, plan))
        self.assertEqual(evidence['simulated_return_amount_raw'], str(minimum))
        self.assertEqual(rpc.calls[0][0], 'eth_call')
        self.assertEqual(rpc.calls[0][1][0]['from'], self.FOLLOWER)
        self.assertEqual(rpc.calls[0][1][0]['to'], R.KYBER_META_AGGREGATION_ROUTER_V2)
        with self.assertRaisesRegex(ValueError, 'below the minimum'):
            asyncio.run(simulate_aggregator_execution(
                Rpc('0x' + encode(['uint256', 'uint256'], [minimum - 1, 1]).hex()), plan))
        with self.assertRaisesRegex(ValueError, 'reverted'):
            asyncio.run(simulate_aggregator_execution(
                Rpc(error=RpcError('execution reverted')), plan))
        with self.assertRaisesRegex(ValueError, 'no output'):
            asyncio.run(simulate_aggregator_execution(Rpc('0x'), plan))
        with self.assertRaisesRegex(ValueError, 'only defined for aggregator'):
            asyncio.run(simulate_aggregator_execution(
                rpc, replace(plan, execution_provider='local', to=R.V3_ROUTER)))

    def test_execution_providers_config_validation_and_snapshot(self):
        def load(providers):
            document = {
                'version': 1, 'strategy_version': 'agg-v1', 'trigger_mode': 'evidenced',
                'quote_policy': {}, 'allowed_protocols': ['v3', 'kyber', 'relay_solver'],
                'allowed_assets': [R.USDG], 'allowed_routes': [],
                'wallets': [{'wallet': A, 'budget_limits': {'USDG': '1000'},
                             'buy_rules': {'USDG': {'mode': 'fixed',
                                                    'fixed_amount_raw': '10'}},
                             'sell_rule': {'mode': 'proportional', 'ratio_ppm': 1000000}}],
            }
            if providers is not None:
                document['wallets'][0]['execution_providers'] = providers
            with tempfile.NamedTemporaryFile('w', suffix='.json', delete=False) as stream:
                json.dump(document, stream)
            try:
                return load_paper_config(stream.name)
            finally:
                os.unlink(stream.name)

        default = load(None)
        self.assertEqual(default.relationships[0].execution_providers, ('local',))
        enabled = load(['local', 'kyber'])
        self.assertEqual(enabled.relationships[0].execution_providers, ('local', 'kyber'))
        self.assertNotEqual(default.snapshot_hash, enabled.snapshot_hash)
        self.assertEqual(load(['kyber']).relationships[0].execution_providers, ('kyber',))
        self.assertNotEqual(load(['kyber']).snapshot_hash, enabled.snapshot_hash)
        for invalid in (['local', 'okx'], ['local', 'local'], []):
            with self.assertRaisesRegex(ValueError, 'execution providers'):
                load(invalid)

    def test_mysql_rows_default_to_local_execution_when_column_missing(self):
        row = {
            'id': 7, 'follower_wallet': B, 'smart_wallet': A, 'smart_wallet_label': 'x',
            'run_mode': 'mainnet_live', 'strategy_version': 'agg-v1',
            'trigger_mode': 'evidenced', 'shadow_trigger_modes': '[]',
            'quote_policy': '{}', 'allowed_protocols': '["v3","kyber"]',
            'allowed_assets': json.dumps([R.USDG]), 'allowed_routes': '[]',
            'usdg_rule_mode': 'fixed', 'usdg_fixed_amount_raw': '100000',
            'usdg_ratio_ppm': None, 'usdg_budget_limit_raw': '10000000',
            'eth_rule_mode': 'fixed', 'eth_fixed_amount_raw': '1', 'eth_ratio_ppm': None,
            'eth_budget_limit_raw': '1', 'sell_rule_mode': 'proportional',
            'sell_fixed_amount_raw': None, 'sell_ratio_ppm': 1000000,
        }
        document = rows_to_document([dict(row)])
        self.assertEqual(document['wallets'][0]['execution_providers'], ['local'])
        document = rows_to_document([{**row, 'execution_providers': '["local","kyber"]'}])
        self.assertEqual(document['wallets'][0]['execution_providers'], ['local', 'kyber'])

    def test_approval_spender_allowlist_includes_kyber_router(self):
        policy = SimpleNamespace(
            run_mode='mainnet_live', follower_wallet=B, relationship_id='1', wallet=A,
            snapshot_hash='ab' * 32, quote_policy=QuotePolicy(),
            allowed_assets=frozenset({R.USDG}), budget_limits={'USDG': '10000000'})

        class Rpc:
            async def call(self, method, params=None):
                return {'eth_getCode': '0x6001', 'eth_call': hex(10 ** 30)}[method]

        class Gate:
            def validate(self, *values):
                pass

        result = asyncio.run(approve_relationship_usdg(
            policy, Rpc(), Gate(), None, minimum_required_raw='100000',
            spender=R.KYBER_META_AGGREGATION_ROUTER_V2))
        self.assertEqual((result.submitted, result.spender, result.amount_raw),
                         (False, R.KYBER_META_AGGREGATION_ROUTER_V2, str(10000000 * 200)))
        with self.assertRaisesRegex(ValueError, 'not eligible for approval'):
            asyncio.run(approve_relationship_token(
                policy, Rpc(), Gate(), None, R.USDG, '1', spender=A))

    def test_prepared_plan_can_be_cancelled_and_nonce_released(self):
        async def scenario():
            store = Store(':memory:')
            store.start_paper_budget_cycle('cycle', 'test')
            store.configure_paper_budget(A, 'USDG', '1000')
            signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                            stage='swap_evidenced', execution_status='success', exact_in=True,
                            token_in=R.USDG, token_out=TOKEN, protocol='v2',
                            evidence={'route': [R.USDG, TOKEN],
                                      'actual_input_debit_raw': '100',
                                      'actual_output_credit_raw': '200'})
            store.put(signal)
            store.reserve_paper_proposal({
                'proposal_id': 'proposal-cancel', 'source_event_id': signal.event_id,
                'source_tx_hash': TXHASH, 'wallet': A,
                'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
                'input_asset': R.USDG, 'output_asset': TOKEN,
                'budget_bucket': 'USDG', 'amount_in_raw': '100',
                'attribution': {'smart_wallet': A, 'follower_wallet': B,
                                'relationship_id': '42',
                                'config_snapshot_hash': 'ab' * 32},
            })

            class Quoter:
                async def quote_with_reference(self, source, amount):
                    return (Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, TOKEN, amount, '198'),
                            Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, TOKEN, '1', '2'), '100')

            class Rpc:
                async def call(self, method, params=None):
                    return {'eth_getTransactionCount': hex(7),
                            'eth_getBalance': hex(100000000),
                            'eth_gasPrice': '0x64', 'eth_call': '0x64'}[method]

            policy = QuotePolicy(max_adverse_deviation_bps=200,
                                 max_price_impact_bps=200,
                                 max_gas_cost_wei='30000000')
            prepared = await ExecutionPreparer(
                store, Quoter(), Rpc(), policy, frozenset({'v2'}),
                frozenset({R.USDG, TOKEN}), frozenset({signal_route_key(signal)}),
                'ab' * 32).prepare(signal, 'proposal-cancel', now=101)
            self.assertEqual(store.execution_nonce_reservation(
                'proposal-cancel')['status'], 'reserved')
            self.assertTrue(store.cancel_prepared_execution_plan(
                'proposal-cancel', 'live_sign_rejected: adverse_price_deviation_exceeded'))
            self.assertFalse(store.cancel_prepared_execution_plan('proposal-cancel', 'again'))
            plan = store.execution_plan('proposal-cancel')
            self.assertEqual((plan['status'], plan['final_review']['reason'],
                              plan['final_review']['broadcast_performed'],
                              plan['final_review']['released_nonce']),
                             ('cancelled', 'live_sign_rejected: adverse_price_deviation_exceeded',
                              False, 7))
            released = store.execution_nonce_reservation('proposal-cancel')
            self.assertEqual(released['status'], 'released')
            self.assertGreaterEqual(released['nonce'], 1 << 62)
            self.assertTrue(store.cancel_paper_proposal('proposal-cancel', 'live_sign_rejected'))
            self.assertEqual(store.paper_budget(A, 'USDG')['reserved_raw'], '0')
            audit = store.execution_audit()
            self.assertTrue(audit['healthy'], audit['issues'])
            # A later plan for the same follower reuses the released nonce.
            nonce, status = store.reserve_execution_nonce(
                'reservation-next', B, '42', 'proposal-next', R.CHAIN_ID, 7)
            self.assertEqual((nonce, status), (7, 'reserved'))
            self.assertEqual(prepared.nonce, 7)
            store.close()

        asyncio.run(scenario())

    def test_signed_but_unbroadcast_plan_can_be_cancelled(self):
        async def scenario():
            account = Account.create()
            follower = account.address.lower()
            store = Store(':memory:')
            store.start_paper_budget_cycle('cycle', 'test')
            store.configure_paper_budget(A, 'USDG', '1000')
            signal = Signal(TXHASH, A, 'direct', 'BUY', 'call', R.V2_ROUTER, '0x',
                            stage='swap_evidenced', execution_status='success', exact_in=True,
                            token_in=R.USDG, token_out=TOKEN, protocol='v2',
                            evidence={'route': [R.USDG, TOKEN],
                                      'actual_input_debit_raw': '100',
                                      'actual_output_credit_raw': '200'})
            store.put(signal)
            store.reserve_paper_proposal({
                'proposal_id': 'proposal-signed-cancel', 'source_event_id': signal.event_id,
                'source_tx_hash': TXHASH, 'wallet': A,
                'trigger_mode': 'swap_evidenced', 'strategy_version': 'paper-v1',
                'input_asset': R.USDG, 'output_asset': TOKEN,
                'budget_bucket': 'USDG', 'amount_in_raw': '100',
                'attribution': {'smart_wallet': A, 'follower_wallet': follower,
                                'relationship_id': '42',
                                'config_snapshot_hash': 'ab' * 32},
            })

            class Quoter:
                async def quote_with_reference(self, source, amount):
                    return (Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, TOKEN, amount, '198'),
                            Quote('v2', R.V2_ROUTER, 10, '0x' + 'ab' * 32,
                                  100.0, R.USDG, TOKEN, '1', '2'), '100')

            class Rpc:
                async def call(self, method, params=None):
                    return {'eth_getTransactionCount': hex(7),
                            'eth_getBalance': hex(100000000),
                            'eth_gasPrice': '0x64', 'eth_call': '0x64'}[method]

            class Signer:
                def __init__(self, expected):
                    pass

                def sign_transaction(self, transaction):
                    return bytes(account.sign_transaction(transaction).raw_transaction)

            class Gate:
                def validate(self, *values):
                    pass

            policy = QuotePolicy(max_adverse_deviation_bps=200,
                                 max_price_impact_bps=200,
                                 max_gas_cost_wei='30000000')
            await ExecutionPreparer(
                store, Quoter(), Rpc(), policy, frozenset({'v2'}),
                frozenset({R.USDG, TOKEN}), frozenset({signal_route_key(signal)}),
                'ab' * 32).prepare(signal, 'proposal-signed-cancel', now=101)
            with patch.dict(os.environ, OFFLINE_ENV):
                signed = await OfflineExecutionSigner(
                    store, Quoter(), Rpc(), policy, 'ab' * 32,
                    signer_factory=Signer, relationship_gate=Gate()).sign(
                        signal, 'proposal-signed-cancel', now=101)
            self.assertEqual(len(store.execution_attempts(
                store.execution_plan('proposal-signed-cancel')['plan_id'])), 1)
            self.assertFalse(store.cancel_prepared_execution_plan(
                'proposal-signed-cancel', 'not prepared any more'))
            self.assertTrue(store.cancel_unbroadcast_signed_execution_plan(
                'proposal-signed-cancel', 'live_review_rejected: pending nonce mismatch'))
            plan = store.execution_plan('proposal-signed-cancel')
            self.assertEqual(plan['status'], 'cancelled')
            self.assertEqual(plan['final_review']['signed_tx_hash_never_broadcast'],
                             signed.signed_tx_hash)
            self.assertEqual(plan['final_review']['released_nonce'], 7)
            self.assertTrue(plan['final_review']['signing_review']['read_only'])
            self.assertEqual(store.execution_attempts(plan['plan_id']), [])
            self.assertEqual(store.execution_nonce_reservation(
                'proposal-signed-cancel')['status'], 'released')
            self.assertTrue(store.cancel_paper_proposal(
                'proposal-signed-cancel', 'live_review_rejected'))
            audit = store.execution_audit()
            self.assertTrue(audit['healthy'], audit['issues'])
            self.assertEqual(audit['attempts'], 0)
            nonce, status = store.reserve_execution_nonce(
                'reservation-after-signed', follower, '42', 'proposal-next', R.CHAIN_ID, 7)
            self.assertEqual((nonce, status), (7, 'reserved'))
            store.close()

        asyncio.run(scenario())

    def test_mysql_compat_reconnects_only_outside_transactions(self):
        import pymysql
        from smart_money.mysql_store import MySqlConnectionCompat

        class FakeCursor:
            def __init__(self, connection):
                self.connection = connection
                self.rowcount = 0

            def execute(self, sql, params=None):
                self.connection.statements.append(sql)
                if self.connection.dead:
                    raise pymysql.err.InterfaceError(0, '')

            def fetchone(self):
                return None

            def fetchall(self):
                return []

        class FakeConnection:
            def __init__(self):
                self.dead = False
                self.statements = []
                self.closed = False

            def cursor(self):
                return FakeCursor(self)

            def begin(self):
                if self.dead:
                    raise pymysql.err.OperationalError(2013, 'Lost connection')
                self.statements.append('BEGIN')

            def commit(self):
                self.statements.append('COMMIT')

            def rollback(self):
                if self.dead:
                    raise pymysql.err.InterfaceError(0, '')
                self.statements.append('ROLLBACK')

            def close(self):
                self.closed = True

        connections = [FakeConnection(), FakeConnection()]

        def factory(**kwargs):
            return connections.pop(0)

        first = FakeConnection()
        compat = MySqlConnectionCompat(first, factory)
        compat.execute("SELECT 1")
        first.dead = True
        compat.execute("SELECT 2")
        self.assertEqual(compat.reconnections, 1)
        self.assertTrue(first.closed)
        current = compat._connection
        self.assertEqual(current.statements, ["SELECT 2"])
        compat.execute("BEGIN IMMEDIATE")
        current.dead = True
        with self.assertRaises(pymysql.err.InterfaceError):
            compat.execute("UPDATE t SET x=1")
        self.assertFalse(compat._in_transaction)
        self.assertEqual(compat.reconnections, 1)
        compat.rollback()  # dead connection: rollback is a no-op, no exception
        compat.execute("SELECT 3")  # reconnects lazily on the next statement
        self.assertEqual(compat.reconnections, 2)
        self.assertEqual(compat._connection.statements, ["SELECT 3"])
        broken = FakeConnection()
        compat_no_factory = MySqlConnectionCompat(broken)
        broken.dead = True
        with self.assertRaises(AttributeError):
            compat_no_factory.execute("SELECT 1")  # ping() missing on the fake: surfaces

    def test_store_recovers_kyber_sell_route_from_attributed_lot(self):
        from smart_money.paper import aggregator_route_definition
        store = Store(':memory:')
        store.start_paper_budget_cycle('cycle', 'test')
        store.configure_paper_budget(A, 'USDG', '1000000')
        signal = self._signal(protocol='relay_solver')
        signal.evidence['local_execution_route'] = aggregator_route_definition(
            R.USDG, self.TOKEN_OUT, 'kyber', R.CHAIN_ID)
        store.put(signal)
        reserved, reason = store.reserve_paper_proposal({
            'proposal_id': 'proposal-kyber-buy', 'source_event_id': signal.event_id,
            'source_tx_hash': TXHASH, 'wallet': A, 'trigger_mode': 'evidenced',
            'strategy_version': 'agg-v1', 'input_asset': R.USDG,
            'output_asset': self.TOKEN_OUT, 'budget_bucket': 'USDG',
            'amount_in_raw': '100000',
            'attribution': {'smart_wallet': A, 'follower_wallet': B,
                            'relationship_id': '2', 'config_snapshot_hash': 'ab' * 32},
        })
        self.assertTrue(reserved, reason)
        self.assertTrue(store.fill_paper_buy('proposal-kyber-buy', {
            'order_id': 'order-1', 'fill_id': 'fill-1', 'lot_id': 'lot-1',
            'amount_out_raw': '150000000000000000000', 'fee_asset': self.TOKEN_OUT,
            'fee_amount_raw': '0', 'gas_cost_wei': '1',
            'quote_observed_at': '2026-09-13T00:00:00+00:00',
            'filled_at': '2026-09-13T00:00:01+00:00',
        }))
        route, status = store.paper_sell_execution_route(
            A, self.TOKEN_OUT, R.USDG, '150000000000000000000')
        self.assertEqual((status, route['protocol'], route['provider'], route['router']),
                         ('selected', 'kyber', 'kyber', R.KYBER_META_AGGREGATION_ROUTER_V2))
        store.close()


class RelaySellConfirmationTests(unittest.TestCase):
    """Relay-order confirmation of a sell whose venue emitted no recognised Swap."""

    TX = "0x4a9fefb4d4ff4602715dcf2099636c5938473c24395ef0e3fe2c1d5f57a22d3f"
    WALLET = "0x1cfbe3af88266ccca29372661f45261c7d19be09"
    SOLD = "0xba1ad98f097c924c3b5894ae05ab363be3bc0c22"
    ORDER = "0x8ea23cf693c029149424b3a175661a5fc45470edcc5000697d80f40e8ebef9fc"
    DEBIT = "10446315194677918327013232"
    DEPOSIT = "85841118"

    def document(self):
        return json.loads(
            (ROOT / "data/relay_sell_evidence_2026-09-13.json").read_text())

    def candidate(self, **overrides):
        evidence = {
            "source_orchestrator": "relay", "relay_deposit_order_id": self.ORDER,
            "relay_deposit_path": "0", "relay_deposit_amount_raw": self.DEPOSIT,
            "swap_event_count_in_scope": 0,
            "wallet_erc20_deltas_raw": {self.SOLD: "-" + self.DEBIT, R.USDG: "0"},
        }
        evidence.update(overrides.pop("evidence", {}))
        fields = dict(
            stage="needs_review", execution_status="success", execution_success=True,
            token_in=self.SOLD, token_out=R.USDG, amount_in_raw=self.DEBIT,
            recipient=R.RELAY_ROUTER, protocol="kyber",
            reasons=["relay_sell_evidence_not_uniquely_closed"], evidence=evidence)
        fields.update(overrides)
        wallet = fields.pop("wallet", self.WALLET)
        return Signal(self.TX, wallet, "userop", "SELL", "0", B, "0xe21fd0e9", **fields)

    def test_pons_swap_topic_counts_as_swap_event(self):
        self.assertEqual(SWAPS[PONS_V2_SWAP], "pons_v2")
        self.assertEqual(
            PONS_V2_SWAP,
            "0x8113d738abdcb6b38357e9d53a54a7157861a09031b453651f0fe7fe151f59df")
        self.assertEqual(len(SWAPS), 4)

    def test_relay_order_confirms_sell_among_bundled_requests(self):
        document = self.document()
        self.assertEqual(len(document["requests"]), 2)
        confirmed = relay_confirmed_sell(document, self.candidate())
        self.assertEqual(confirmed.stage, "relay_sell_evidenced")
        self.assertEqual(confirmed.behavior, "SELL")
        self.assertEqual(confirmed.amount_in_raw, self.DEBIT)
        self.assertEqual(confirmed.amount_out_raw, self.DEPOSIT)
        self.assertEqual(confirmed.evidence["actual_input_debit_raw"], self.DEBIT)
        self.assertEqual(confirmed.evidence["actual_output_deposit_raw"], self.DEPOSIT)
        self.assertEqual(confirmed.evidence["actual_output_credit_raw"], self.DEPOSIT)
        self.assertEqual(confirmed.evidence["relay_order_id"], self.ORDER)
        self.assertEqual(confirmed.evidence["relay_request_id"],
                         "0x1789305280a526a5b4701bb239125232cd83126c7fede6012bf6b4b6293ab5b2")
        self.assertEqual(confirmed.evidence["relay_destination_chain_id"], "792703809")
        self.assertEqual(confirmed.evidence["relay_destination_amount_raw"], "85204699")
        self.assertEqual(confirmed.evidence["relay_destination_recipient"],
                         "4zFEFU8gtZ2uQVn3pkWLaD829a7gdxTsexGY89KePW3j")
        self.assertNotIn("relay_sell_evidence_not_uniquely_closed", confirmed.reasons)
        self.assertIn("relay_order_confirms_sell_without_recognized_swap_event",
                      confirmed.reasons)
        self.assertFalse(confirmed.copy_eligible)
        # The original signal is untouched; the caller decides what to emit.
        self.assertEqual(self.candidate().stage, "needs_review")

    def test_relay_order_confirmation_rejects_every_identity_mismatch(self):
        document = self.document()
        cases = {
            "not owned": dict(wallet="0xbb0d687957a43cf9046341ce697d7f45764368c3"),
            "wallet debit does not match": dict(amount_in_raw="1"),
            "not uniquely present": dict(evidence={
                "relay_deposit_order_id": "0x" + "ab" * 32}),
            "local deposit event": dict(evidence={"relay_deposit_amount_raw": "85841117"}),
            "sold currency": dict(token_in=TOKEN, evidence={
                "wallet_erc20_deltas_raw": {TOKEN: "-" + self.DEBIT}}),
            "moved more than": dict(evidence={"wallet_erc20_deltas_raw": {
                self.SOLD: "-" + self.DEBIT, TOKEN: "5"}}),
            "not an unclosed": dict(reasons=["wallet_exchange_flows_not_closed"]),
            "must debit one token into the USDG": dict(token_out=TOKEN),
        }
        for message, overrides in cases.items():
            with self.subTest(message):
                with self.assertRaisesRegex(ValueError, message):
                    relay_confirmed_sell(document, self.candidate(**overrides))
        forged = self.document()
        forged["requests"][0]["data"]["inTxs"][0]["hash"] = "0x" + "cd" * 32
        with self.assertRaisesRegex(ValueError, "input transaction"):
            relay_confirmed_sell(forged, self.candidate())
        forged = self.document()
        forged["requests"][0]["protocol"]["deposit"]["origin"]["amount"] = "85841119"
        with self.assertRaisesRegex(ValueError, "origin deposit|local deposit event"):
            relay_confirmed_sell(forged, self.candidate())
        forged = self.document()
        forged["requests"][0]["data"]["metadata"]["currencyIn"]["amount"] = "1"
        with self.assertRaisesRegex(ValueError, "sold currency"):
            relay_confirmed_sell(forged, self.candidate())
        forged = self.document()
        forged["requests"][0]["status"] = "pending"
        with self.assertRaisesRegex(ValueError, "not uniquely present"):
            relay_confirmed_sell(forged, self.candidate())
        with self.assertRaisesRegex(ValueError, "empty"):
            relay_confirmed_sell({"requests": []}, self.candidate())

    def test_relay_sell_closure_records_deposit_amount_for_later_confirmation(self):
        signals = self.example_signals()
        trade = next(item for item in signals if item.behavior == "SELL")
        deposit = next(item for item in signals if item.behavior == "INTENT_DEPOSIT")
        self.assertEqual(trade.evidence["relay_deposit_amount_raw"], deposit.amount_in_raw)

    def example_signals(self):
        watch = R.load_watchlist(ROOT / "data/fomo_watchlist.csv")
        decoder = Decoder(watch, R.snapshot_delegations(ROOT / "data/account_codes.json"))
        rows = json.loads((ROOT / "data/transaction_examples.json").read_text())
        row = next(r for r in rows["examples"]
                   if r["transaction"]["hash"].startswith("0x23419e"))
        source = Transaction.from_rpc(row["transaction"])
        return enrich(source, decoder.decode(source), row["receipt"], watch)

    def test_relay_client_many_lookup_is_bounded(self):
        client = RelayPublicClient()
        with self.assertRaisesRegex(ValueError, "limit"):
            client._lookup_many(self.TX, 0)
        with self.assertRaisesRegex(ValueError, "invalid Relay"):
            client._lookup_many("0x1234", 5)


if __name__ == '__main__':
    unittest.main()
