"""Arc counts one USDC balance in two scales; decisions must see only one."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from smart_money import registry as R
from smart_money.arc_observer import ArcObserver
from smart_money.models import Signal
from smart_money.quotes import LiveQuoter
from smart_money.registry import native_to_erc20_amount

WALLET = "0x" + "11" * 20
TOKEN = "0x" + "44" * 20
HASH = "0x" + "aa" * 32
HEADER = {"number": "0x10", "hash": "0x" + "22" * 32}


def native_buy(chain_id=R.ARC.chain_id, **kwargs):
    """A buy paid in the chain's native asset, as enrich leaves it."""
    fields = dict(
        stage="swap_evidenced", execution_status="success", canonical_status="confirmed",
        protocol="v4", exact_in=True, token_in=R.NATIVE, token_out=TOKEN,
        amount_in_raw="1000000000000000000", amount_out_raw="90",
        chain_id=chain_id,
        evidence={"actual_input_debit_raw": "1000000000000000000",
                  "actual_output_credit_raw": "90"})
    fields.update(kwargs)
    return Signal(HASH, WALLET, "third_party", "BUY", "incoming", TOKEN,
                  "0x12345678", **fields)


def observer():
    return ArcObserver(object(), object(), {WALLET: {}})


class NativeScaleNormalisationTests(unittest.TestCase):
    def test_native_leg_is_restated_in_the_erc20_scale(self):
        signal = observer()._normalize_native_scale(native_buy())
        # One USDC: 1e18 natively, 1e6 through the enshrined ERC-20.
        self.assertEqual(signal.amount_in_raw, "1000000")
        self.assertEqual(signal.evidence["actual_input_debit_raw"], "1000000")
        # The token leg was never in the native scale and is untouched.
        self.assertEqual(signal.amount_out_raw, "90")
        self.assertEqual(signal.evidence["actual_output_credit_raw"], "90")
        note = signal.evidence["native_scale_normalization"]
        self.assertEqual(note["divisor"], "1000000000000")
        self.assertEqual(note["dropped_dust_raw"], {})

    def test_dust_below_the_erc20_precision_is_declared_not_hidden(self):
        raw = "752486736963102522762"
        signal = observer()._normalize_native_scale(
            native_buy(amount_in_raw=raw,
                       evidence={"actual_input_debit_raw": raw}))
        self.assertEqual(signal.amount_in_raw, "752486736")
        self.assertEqual(
            signal.evidence["native_scale_normalization"]["dropped_dust_raw"],
            {"amount_in_raw": "963102522762",
             "actual_input_debit_raw": "963102522762"})

    def test_a_chain_whose_forms_share_a_scale_is_left_alone(self):
        # Robinhood's ETH and WETH are both 18 decimals: nothing to restate.
        signal = observer()._normalize_native_scale(native_buy(chain_id=R.CHAIN_ID))
        self.assertEqual(signal.amount_in_raw, "1000000000000000000")
        self.assertNotIn("native_scale_normalization", signal.evidence)

    def test_a_signal_without_a_native_leg_is_left_alone(self):
        signal = observer()._normalize_native_scale(
            native_buy(token_in=R.ARC.usdc_erc20, amount_in_raw="1000000"))
        self.assertEqual(signal.amount_in_raw, "1000000")
        self.assertNotIn("native_scale_normalization", signal.evidence)

    def test_a_limit_that_cannot_be_tied_to_a_leg_holds_the_signal(self):
        signal = observer()._normalize_native_scale(
            native_buy(exact_in=None, amount_limit_raw="5"))
        self.assertEqual(signal.stage, "needs_review")
        self.assertIn("native_amount_limit_scale_undetermined", signal.reasons)
        self.assertEqual(signal.amount_in_raw, "1000000000000000000")

    def test_the_limit_follows_the_direction_of_the_swap(self):
        # Exact input: the limit bounds the output, which here is the token.
        signal = observer()._normalize_native_scale(
            native_buy(exact_in=True, amount_limit_raw="80"))
        self.assertEqual(signal.amount_limit_raw, "80")
        # Exact output: the limit bounds the input, which is the native leg.
        signal = observer()._normalize_native_scale(
            native_buy(exact_in=False, amount_limit_raw="1000000000000000000"))
        self.assertEqual(signal.amount_limit_raw, "1000000")

    def test_helper_rejects_a_negative_or_non_integer_amount(self):
        for value in (-1, "5", 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                native_to_erc20_amount(value, R.ARC)


class QuoteScaleGuardTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_unnormalised_native_arc_signal_is_never_quoted(self):
        rpc = SimpleNamespace(call=AsyncMock())
        with self.assertRaisesRegex(ValueError, "ERC-20 scale before quoting"):
            await LiveQuoter(rpc)._quote_at(native_buy(), "1000000", HEADER)
        rpc.call.assert_not_awaited()

    async def test_a_normalised_signal_passes_the_guard(self):
        rpc = SimpleNamespace(call=AsyncMock())
        signal = observer()._normalize_native_scale(native_buy())
        # It gets past the scale guard and fails later, on the missing pool key.
        with self.assertRaises(ValueError) as caught:
            await LiveQuoter(rpc)._quote_at(signal, "1000000", HEADER)
        self.assertNotIn("ERC-20 scale", str(caught.exception))

    async def test_robinhood_native_quotes_are_unaffected(self):
        rpc = SimpleNamespace(call=AsyncMock())
        signal = native_buy(chain_id=R.CHAIN_ID, protocol="v2",
                            evidence={"route": [R.NATIVE, TOKEN]})
        with self.assertRaises(Exception) as caught:
            await LiveQuoter(rpc)._quote_at(signal, "1000000", HEADER)
        self.assertNotIn("ERC-20 scale", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
