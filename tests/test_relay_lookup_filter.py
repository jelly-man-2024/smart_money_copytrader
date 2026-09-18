"""Local synthetic receipts and mocked monitor; no network, keys or broadcasts."""
import asyncio
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from eth_abi import encode

from smart_money import cli, registry as R
from smart_money.models import Transaction
from smart_money.receipts import (
    APPROVAL, DEPOSIT_RECORDED, SWAPS, TRANSFER, direct_token_transfer_evidence, enrich,
)
from smart_money.relay_api import RelayApiError, RelayNotReady
from smart_money.store import Store

SENDER, WALLET, TOKEN, OPERATOR = ("0x" + byte * 20 for byte in ("11", "22", "33", "44"))
TXHASH, BLOCKHASH = "0x" + "aa" * 32, "0x" + "bb" * 32


def log(event=TRANSFER, sender=SENDER, recipient=WALLET, amount=123):
    return {"address": TOKEN, "topics": [event, "0x" + encode(["address"], [sender]).hex(),
                                        "0x" + encode(["address"], [recipient]).hex()],
            "data": "0x" + encode(["uint256"], [amount]).hex(), "logIndex": "0x0"}


def sample(transfer_from=False):
    data = (bytes.fromhex("23b872dd") + encode(["address", "address", "uint256"], [SENDER, WALLET, 123])
            if transfer_from else bytes.fromhex("a9059cbb") + encode(["address", "uint256"], [WALLET, 123]))
    tx = Transaction(TXHASH, OPERATOR if transfer_from else SENDER, TOKEN, data,
                     timestamp=int(time.time()), received_at=time.time(), fresh=True,
                     observation_source="feed")
    receipt = {"transactionHash": TXHASH, "status": "0x1", "blockNumber": "0xa",
               "blockHash": BLOCKHASH, "logs": [log()]}
    return tx, receipt


class TransferFilterTests(unittest.TestCase):
    def test_transfer_exact_evidence_without_mutating_input(self):
        tx, receipt = sample()
        original = deepcopy(receipt)
        result = direct_token_transfer_evidence(tx, receipt, WALLET)
        self.assertEqual(result, {"rule": "direct-token-transfer-v1", "selector": "0xa9059cbb",
            "token": TOKEN, "sender": SENDER, "recipient": WALLET, "amount_raw": "123",
            "provenance": "transaction_calldata_and_receipt"})
        self.assertEqual(receipt, original)
        signal = enrich(tx, [], receipt, {WALLET: {}})[0]
        self.assertEqual((signal.behavior, signal.stage), ("INCOMING_TRANSFER", "needs_review"))
        self.assertFalse(signal.copy_eligible)

    def test_transfer_from_uses_owner_not_operator_and_allows_approval(self):
        tx, receipt = sample(True)
        for approval in (False, True):
            with self.subTest(approval=approval):
                if approval:
                    receipt["logs"].append(log(APPROVAL, recipient=OPERATOR, amount=0))
                self.assertEqual(direct_token_transfer_evidence(tx, receipt, WALLET)["sender"], SENDER)

    def test_mismatched_log_fields_and_wallet_keep_lookup(self):
        tx, receipt = sample()
        for field, value in (("address", OPERATOR), ("data", "0x" + encode(["uint256"], [124]).hex()),
                             ("topics", log(sender=OPERATOR)["topics"]),
                             ("topics", log(recipient=OPERATOR)["topics"]), ("removed", True)):
            with self.subTest(field=field, value=value):
                bad = deepcopy(receipt)
                bad["logs"][0][field] = value
                self.assertIsNone(direct_token_transfer_evidence(tx, bad, WALLET))
        self.assertIsNone(direct_token_transfer_evidence(tx, receipt, OPERATOR))

    def test_swap_relay_unknown_and_multiple_transfer_logs_keep_lookup(self):
        tx, receipt = sample()
        for event in (*SWAPS, DEPOSIT_RECORDED, "0x" + "cc" * 32, TRANSFER):
            with self.subTest(event=event):
                bad = deepcopy(receipt)
                bad["logs"].append(log(event))
                self.assertIsNone(direct_token_transfer_evidence(tx, bad, WALLET))
        # A taxed transfer with an additional fee movement is also outside scope.
        receipt["logs"].append(log(recipient=OPERATOR, amount=1))
        self.assertIsNone(direct_token_transfer_evidence(tx, receipt, WALLET))

    def test_wrapper_value_trailing_truncated_unknown_calldata_keep_lookup(self):
        tx, receipt = sample()
        for bad in (replace(tx, to=R.RELAY_PROXY), replace(tx, to=None), replace(tx, value=1),
                    replace(tx, data=tx.data+b"\0"), replace(tx, data=tx.data[:-1]),
                    replace(tx, data=b"\0"*68), replace(tx, data=tx.data[:4]+b"\x01"+tx.data[5:])):
            with self.subTest(tx=bad):
                self.assertIsNone(direct_token_transfer_evidence(bad, receipt, WALLET))

    def test_failed_wrong_hash_missing_or_malformed_receipt_keeps_lookup(self):
        tx, receipt = sample()
        for field, value in (("status", "0x0"), ("status", None), ("transactionHash", BLOCKHASH),
                             ("logs", []), ("logs", None), ("logs", [None]), ("logs", [{}])):
            with self.subTest(field=field, value=value):
                bad = {**receipt, field: value}
                self.assertIsNone(direct_token_transfer_evidence(tx, bad, WALLET))

    def test_noncanonical_log_encoding_keeps_lookup(self):
        tx, receipt = sample()
        variants = []
        for field, value in (("data", "0x1"), ("data", "0x"+"gg"*32),
                             ("address", "0x12"), ("topics", None),
                             ("topics", [TRANSFER]), ("data", "0x"+" "*64)):
            variants.append({**log(), field: value})
        for index in (1, 2):
            malformed = log()
            malformed["topics"][index] = "0x01" + malformed["topics"][index][4:]
            variants.append(malformed)
        variants.append(log(APPROVAL))  # Approval alone is not a transfer.
        for bad in variants:
            with self.subTest(log=bad):
                self.assertIsNone(direct_token_transfer_evidence(tx, {**receipt, "logs": [bad]}, WALLET))

    def test_zero_mint_and_self_transfer_keep_lookup(self):
        tx, receipt = sample()
        for sender, recipient, amount in ((SENDER, WALLET, 0), (R.NATIVE, WALLET, 123),
                                          (WALLET, WALLET, 123), (SENDER, R.NATIVE, 123)):
            bad_tx = replace(tx, sender=sender,
                data=bytes.fromhex("a9059cbb")+encode(["address", "uint256"], [recipient, amount]))
            bad_receipt = {**receipt, "logs": [log(sender=sender, recipient=recipient, amount=amount)]}
            self.assertIsNone(direct_token_transfer_evidence(bad_tx, bad_receipt, recipient))

    def test_case_normalization_and_maximum_uint256(self):
        tx, receipt = sample()
        amount = 2**256-1
        tx = replace(tx, data=bytes.fromhex("a9059cbb")+encode(["address", "uint256"], [WALLET, amount]))
        receipt["logs"] = [log(amount=amount)]
        receipt["logs"][0]["topics"][0] = "0x" + TRANSFER[2:].upper()
        self.assertEqual(direct_token_transfer_evidence(tx, receipt, WALLET)["amount_raw"], str(amount))


class MonitorFilterTests(unittest.IsolatedAsyncioTestCase):
    async def run_monitor(self, *, unknown=False, transfer_from=False, mixed=False, enabled=True,
                          relay_error=None):
        tx, receipt = sample(transfer_from)
        if transfer_from:
            receipt["logs"].append(log(APPROVAL, recipient=OPERATOR))
        if unknown:
            receipt["logs"].append(log("0x"+"cc"*32))
        relay = MagicMock(lookup_by_destination_hash=AsyncMock(
            side_effect=relay_error or RelayNotReady("not indexed")))
        class Rpc:
            async def call(self, method, params=None):
                if method == "eth_chainId":
                    return hex(R.CHAIN_ID)
                if method == "debug_traceTransaction":
                    return {}
                raise AssertionError("unexpected RPC: " + method)
            async def receipt(self, tx_hash):
                return deepcopy(receipt)
        class Socket:
            async def __aenter__(self): return self
            async def __aexit__(self, *args): pass
            def __aiter__(self): return self.frames()
            async def frames(self):
                yield "synthetic-frame"
                await asyncio.Event().wait()
        def enriched(*args):
            signals = enrich(*args)
            if mixed:
                signals.append(replace(signals[0], wallet=OPERATOR, evidence={}))
            # A stale/injected flag must never grant a skip on unmatched data.
            if unknown:
                signals[0].evidence["relay_lookup_skipped"] = True
            return signals
        with tempfile.TemporaryDirectory() as folder:
            argv = ["monitor", "--seconds", "0.08", "--db", str(Path(folder)/"test.sqlite3")]
            if enabled:
                argv.append("--relay-auto-associate")
            args = cli.parser().parse_args(argv)
            health = MagicMock(gap=False, max_age_seconds=3)
            health.healthy.return_value = True
            scanner = MagicMock(scan_once=AsyncMock(return_value=SimpleNamespace(initialized=False)))
            with patch.object(cli, "load_endpoint_env"), patch.object(cli, "ReadOnlyRpc", return_value=Rpc()), \
                 patch.object(cli, "monitoring_watchlist", return_value={WALLET: {}}), \
                 patch.object(cli, "FeedHealth", return_value=health), \
                 patch.object(cli, "BlockScanner", return_value=scanner), \
                 patch.object(cli, "RelayPublicClient", return_value=relay), \
                 patch.object(cli, "envelopes", return_value=[(b"raw", {"fresh": True})]), \
                 patch.object(cli, "decode_raw", return_value=tx), \
                 patch.object(cli, "enrich", side_effect=enriched), \
                 patch.object(cli.websockets, "connect", return_value=Socket()), \
                 patch.object(cli, "report") as report, redirect_stdout(io.StringIO()):
                await asyncio.wait_for(cli.monitor(args), 3)
            db = Store(args.db)
            try:
                rows = list(db.rows())
                state = db.connection.execute("SELECT status,last_error FROM candidates").fetchone()
                inclusions = db.connection.execute("SELECT COUNT(*) FROM candidate_inclusions").fetchone()[0]
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM execution_attempts").fetchone()[0], 0)
                self.assertEqual(db.connection.execute("SELECT COUNT(*) FROM paper_proposals").fetchone()[0], 0)
            finally:
                db.close()
            finished = next(c.kwargs for c in report.call_args_list if c.args[0] == "monitor_finished")
            skips = [c for c in report.call_args_list if c.args[0] == "relay_lookup_skipped"]
            self.events = [c.args[0] for c in report.call_args_list]
            return relay, rows, state, inclusions, finished["counters"], skips

    async def test_skips_query_persists_signal_and_completes_candidate(self):
        for transfer_from in (False, True):
            with self.subTest(transfer_from=transfer_from):
                relay, rows, state, inclusions, counts, skips = await self.run_monitor(transfer_from=transfer_from)
                relay.lookup_by_destination_hash.assert_not_awaited()
                self.assertEqual(state[0], "complete")
                self.assertEqual(inclusions, 1)
                self.assertEqual(counts["relay_lookup_skipped"], 1)
                self.assertEqual(counts["candidate_retries"], 0)
                self.assertEqual(counts["worker_errors"], 0)
                self.assertEqual(len(skips), 1)
                self.assertEqual(len(rows), 1)
                signal = rows[0]
                self.assertEqual((signal["behavior"], signal["stage"]), ("INCOMING_TRANSFER", "needs_review"))
                self.assertFalse(signal["copy_eligible"])
                self.assertTrue(signal["evidence"]["relay_lookup_skipped"])
                self.assertEqual(signal["evidence"]["relay_lookup_skip_reason"], "direct_token_transfer")
                self.assertEqual(signal["evidence"]["relay_lookup_skip_evidence"]["amount_raw"], "123")

    async def test_unknown_retains_query_and_retry_despite_injected_flag(self):
        relay, rows, state, inclusions, counts, skips = await self.run_monitor(unknown=True)
        relay.lookup_by_destination_hash.assert_awaited_once_with(TXHASH)
        self.assertEqual(state, ("retry", "relay_request_not_ready"))
        self.assertEqual(inclusions, 0)
        self.assertEqual(counts["candidate_retries"], 1)
        self.assertEqual(counts["relay_lookup_skipped"], 0)
        self.assertEqual(skips, [])
        self.assertEqual(len(rows), 1)
        self.assertNotIn("relay_lookup_skipped", rows[0]["evidence"])

    async def test_failed_relay_lookup_defers_instead_of_rejecting(self):
        # A rate-limited/failed lookup or a half-written order is not evidence
        # that the delivery was not a purchase. Until 2026-09-18 it was terminal:
        # the candidate completed, the delivery stayed needs_review and the early
        # lot it funded stayed pending forever. It now takes the bounded retry.
        for error in (RelayApiError("Relay HTTP status 429"),
                      RelayApiError("Relay lookup failed: OSError"),
                      ValueError("relay order output does not uniquely authorize the wallet credit")):
            with self.subTest(error=error):
                relay, rows, state, inclusions, counts, skips = await self.run_monitor(
                    unknown=True, relay_error=error)
                relay.lookup_by_destination_hash.assert_awaited_once_with(TXHASH)
                self.assertEqual(state, ("retry", "relay_lookup_failed"))
                self.assertEqual(inclusions, 0)
                self.assertEqual(counts["candidate_retries"], 1)
                self.assertEqual(counts["relay_lookup_errors"], 1)
                self.assertEqual(counts["relay_lookup_pending"], 0)
                self.assertIn("relay_buy_auto_association_deferred", self.events)
                self.assertNotIn("relay_buy_auto_association_rejected", self.events)
                self.assertNotIn("relay_lookup_retry_exhausted", self.events)
                self.assertEqual(len(rows), 1)
                self.assertEqual((rows[0]["behavior"], rows[0]["stage"]),
                                 ("INCOMING_TRANSFER", "needs_review"))

    async def test_other_pending_signal_still_defers_completion(self):
        relay, rows, state, inclusions, counts, skips = await self.run_monitor(mixed=True)
        relay.lookup_by_destination_hash.assert_awaited_once_with(TXHASH)
        self.assertEqual(state[0], "retry")
        self.assertEqual(inclusions, 0)
        self.assertEqual(len(rows), 2)
        self.assertEqual(counts["relay_lookup_skipped"], 1)
        self.assertEqual(counts["candidate_retries"], 1)

    async def test_association_disabled_does_not_claim_a_skipped_query(self):
        relay, rows, state, inclusions, counts, skips = await self.run_monitor(enabled=False)
        relay.lookup_by_destination_hash.assert_not_awaited()
        self.assertEqual(state[0], "complete")
        self.assertNotIn("relay_lookup_skipped", rows[0]["evidence"])
        self.assertEqual(counts["relay_lookup_skipped"], 0)
        self.assertEqual(skips, [])
