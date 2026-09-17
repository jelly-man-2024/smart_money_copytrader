"""A cross-chain Relay fill on Arc is a buy, not a receipt of tokens."""
import unittest
from unittest.mock import AsyncMock

from eth_abi import encode

from smart_money import registry as R
from smart_money.arc_observer import ArcObserver
from smart_money.models import Signal, Transaction
from smart_money.receipts import TRANSFER
from smart_money.relay_api import RelayApiError, RelayNotReady

WALLET = "0x" + "11" * 20
TOKEN = "0x" + "44" * 20
TX_HASH = "0x" + "aa" * 32
SOURCE_HASH = "0x" + "bb" * 32
SOLANA_CHAIN = 792703809
SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_PAYER = "9t6RnQ83wgPwka77bc6B5tHCMc1UWXQUpaNHeiyp5Y4v"


def credit_only_signal():
    """What enrich produces for a wallet that only received tokens."""
    return Signal(
        TX_HASH, WALLET, "third_party", "EXTERNAL_DELIVERY_CANDIDATE", "incoming",
        TOKEN, "0x12345678", stage="needs_review", execution_status="success",
        execution_success=True, chain_id=R.ARC.chain_id,
        evidence={"wallet_erc20_deltas_raw": {TOKEN: "90"}, "swap_event_count": 1})


def arc_relay_document():
    """Relay's order for a Solana-funded delivery on Arc, as the API returns it."""
    return {"requests": [{
        "id": "0x" + "02" * 32, "status": "success", "user": SOLANA_PAYER,
        "recipient": WALLET, "data": {
            "inTxs": [{"hash": SOURCE_HASH, "chainId": SOLANA_CHAIN, "status": "success"}],
            "outTxs": [{
                "hash": TX_HASH, "chainId": R.ARC.chain_id, "status": "success",
                "stateChanges": [{"address": WALLET, "change": {
                    "kind": "token", "balanceDiff": "90",
                    "data": {"tokenKind": "ft", "tokenAddress": TOKEN}}}],
            }],
        }, "protocol": {
            "orderId": "0x" + "01" * 32,
            "deposit": {"origin": {
                "amount": "100", "chainId": SOLANA_CHAIN, "currency": SOLANA_USDC,
                "depositor": SOLANA_PAYER, "transactionId": SOURCE_HASH,
            }},
            "settlement": {"destination": {"fills": [{
                "chainId": R.ARC.chain_id, "transactionId": TX_HASH}]}},
            "orderData": {"output": {"payments": [{
                "currency": TOKEN, "recipient": WALLET, "minimumAmount": "80"}]}},
        },
    }]}


def transaction(to=None, data="0x", sender="0x" + "99" * 20):
    return Transaction.from_rpc({
        "hash": TX_HASH, "from": sender, "to": to or R.ARC.universal_router,
        "input": data, "value": "0x0", "chainId": hex(R.ARC.chain_id),
        "nonce": "0x1", "type": "0x2"})


def transfer_log(token, sender, recipient, amount):
    def topic(value):
        return "0x" + value[2:].rjust(64, "0")
    return {"address": token, "logIndex": "0x0", "removed": False,
            "topics": [TRANSFER, topic(sender), topic(recipient)],
            "data": "0x" + encode(["uint256"], [amount]).hex()}


def receipt(logs=None):
    return {"transactionHash": TX_HASH, "blockHash": "0x" + "bb" * 32,
            "blockNumber": "0x10", "status": "0x1", "gasUsed": "0x1",
            "effectiveGasPrice": "0x1", "logs": logs or []}


def observer(relay_client=None, status=None):
    return ArcObserver(object(), object(), {WALLET: {}},
                       relay_client=relay_client, on_status=status)


class ArcRelayAssociationTests(unittest.IsolatedAsyncioTestCase):
    async def test_solana_funded_delivery_becomes_a_buy_in_arc_usdc(self):
        events = []
        client = AsyncMock()
        client.lookup_by_destination_hash.return_value = arc_relay_document()
        signal = await observer(client, lambda e, d: events.append((e, d))) \
            ._associate_relay_delivery(credit_only_signal(), transaction(), receipt())
        self.assertEqual((signal.behavior, signal.stage), ("BUY", "relay_buy_evidenced"))
        # The wallet paid on Solana, so the input asset is Arc's own USDC, and
        # the amount comes from the source-chain deposit.
        self.assertEqual(signal.token_in, R.ARC.usdc_erc20)
        self.assertEqual((signal.amount_in_raw, signal.amount_out_raw), ("100", "90"))
        self.assertEqual(signal.evidence["source_chain_id"], str(SOLANA_CHAIN))
        self.assertEqual(signal.evidence["source_payer"], SOLANA_PAYER)
        # Both sides are six-decimal USDC here, so nothing is converted.
        self.assertEqual(signal.evidence["funding_normalization"],
                         "relay_source_currency_to_chain_5042_settlement_asset")
        # The unverified link is still declared rather than hidden.
        self.assertIn("relay_origin_chain_receipt_not_independently_rechecked",
                      signal.reasons)
        self.assertEqual([name for name, _ in events], ["arc_relay_buy_associated"])

    async def test_a_plain_token_transfer_is_not_looked_up(self):
        events = []
        client = AsyncMock()
        sender = "0x" + "99" * 20
        data = "0xa9059cbb" + encode(["address", "uint256"], [WALLET, 90]).hex()
        signal = await observer(client, lambda e, d: events.append((e, d))) \
            ._associate_relay_delivery(
                credit_only_signal(), transaction(to=TOKEN, data=data, sender=sender),
                receipt([transfer_log(TOKEN, sender, WALLET, 90)]))
        client.lookup_by_destination_hash.assert_not_awaited()
        self.assertEqual(signal.behavior, "EXTERNAL_DELIVERY_CANDIDATE")
        self.assertTrue(signal.evidence["relay_lookup_skipped"])
        self.assertEqual([name for name, _ in events], ["arc_relay_lookup_skipped"])

    async def test_an_unsettled_order_leaves_the_signal_for_review(self):
        events = []
        client = AsyncMock()
        client.lookup_by_destination_hash.side_effect = RelayNotReady("not settled")
        signal = await observer(client, lambda e, d: events.append((e, d))) \
            ._associate_relay_delivery(credit_only_signal(), transaction(), receipt())
        self.assertEqual((signal.behavior, signal.stage),
                         ("EXTERNAL_DELIVERY_CANDIDATE", "needs_review"))
        self.assertEqual([name for name, _ in events], ["arc_relay_lookup_pending"])

    async def test_a_rejected_order_never_promotes_the_signal(self):
        events = []
        client = AsyncMock()
        client.lookup_by_destination_hash.side_effect = RelayApiError("mismatch")
        signal = await observer(client, lambda e, d: events.append((e, d))) \
            ._associate_relay_delivery(credit_only_signal(), transaction(), receipt())
        self.assertEqual(signal.stage, "needs_review")
        self.assertEqual([name for name, _ in events], ["arc_relay_association_rejected"])

    async def test_an_order_for_another_wallet_is_refused(self):
        client = AsyncMock()
        document = arc_relay_document()
        document["requests"][0]["recipient"] = "0x" + "77" * 20
        client.lookup_by_destination_hash.return_value = document
        signal = await observer(client)._associate_relay_delivery(
            credit_only_signal(), transaction(), receipt())
        self.assertEqual(signal.stage, "needs_review")

    async def test_signals_outside_the_credit_only_shape_are_untouched(self):
        client = AsyncMock()
        traded = credit_only_signal()
        traded.behavior, traded.stage = "BUY", "swap_evidenced"
        signal = await observer(client)._associate_relay_delivery(
            traded, transaction(), receipt())
        self.assertIs(signal, traded)
        client.lookup_by_destination_hash.assert_not_awaited()

    async def test_without_a_relay_client_nothing_is_attempted(self):
        original = credit_only_signal()
        signal = await observer(None)._associate_relay_delivery(
            original, transaction(), receipt())
        self.assertIs(signal, original)


if __name__ == "__main__":
    unittest.main()
