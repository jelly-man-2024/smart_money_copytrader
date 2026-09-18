"""Sell-side price checks: an exit is not refused for the smart wallet's own impact.

Pure functions on synthetic quotes; no network, keys or broadcasts.
"""
import json
from pathlib import Path
import tempfile
import unittest

from smart_money import registry as R
from smart_money.models import Signal
from smart_money.paper_config import load_paper_config
from smart_money.quotes import (
    PRICE_CHECK_DISABLED_BPS, Quote, QuotePolicy, assess_market_quote, assess_quote,
    validate_quote,
)

HASH = "0x" + "11" * 32
BLOCK = "0x" + "22" * 32
WALLET = "0x3004ab92565deeea0a2eaa27e40e297bb457e1a6"
TOKEN = "0x1111111111111111111111111111111111111111"
NOW = 1_800_000_000.0
GAS_PRICE = "56256000"  # ~0.056 gwei, the ledger's median

INHERIT = QuotePolicy(max_age_seconds=6.0, max_adverse_deviation_bps=500,
                      max_price_impact_bps=500, max_slippage_bps=300,
                      max_gas_cost_wei="1000000000000000", min_amount_out_raw="1")
SELL_OFF = QuotePolicy(**{**INHERIT.__dict__,
                          "sell_max_adverse_deviation_bps": PRICE_CHECK_DISABLED_BPS,
                          "sell_max_price_impact_bps": PRICE_CHECK_DISABLED_BPS,
                          "sell_min_amount_out_raw": "10000"})


def quote(amount_in, amount_out, token_in, token_out):
    return Quote("kyber", "0x" + "33" * 20, 16, BLOCK, NOW, token_in, token_out,
                 str(amount_in), str(amount_out), "250000")


def sell(actual_in, actual_out, **overrides):
    """The smart wallet sold ``actual_in`` token for ``actual_out`` USDG."""
    evidence = {"actual_input_debit_raw": str(actual_in), "actual_output_credit_raw": str(actual_out)}
    fields = dict(stage="relay_sell_evidenced", execution_status="success",
                  execution_success=True, token_in=TOKEN, token_out=R.USDG,
                  protocol="0x", evidence=evidence)
    fields.update(overrides)
    return Signal(HASH, WALLET, "third_party", "SELL", "userop/0/1/relay/1", None, "", **fields)


def buy(actual_in, actual_out):
    evidence = {"actual_input_debit_raw": str(actual_in), "actual_output_credit_raw": str(actual_out)}
    return Signal(HASH, WALLET, "third_party", "BUY", "incoming", None, "",
                  stage="relay_buy_evidenced", execution_status="success",
                  execution_success=True, token_in=R.USDG, token_out=TOKEN,
                  protocol="relay_solver", evidence=evidence)


class SellPriceCheckTests(unittest.TestCase):
    # Shaped like the ledger's worst real case: they sold for $1000 at 1e-15 USDG
    # raw per token raw; seconds later our 1e18 token quotes 268 raw USDG, 73%
    # under their fill price, and the 1/100 reference rounds to a single unit.
    THEIR_IN, THEIR_OUT = 10 ** 24, 1_000_000_000
    OUR_IN, OUR_OUT, REF_OUT = 10 ** 18, 268, 1

    def full_and_reference(self, our_in, our_out, ref_out, token_in=TOKEN, token_out=R.USDG):
        return (quote(our_in, our_out, token_in, token_out),
                quote(our_in // 100, ref_out, token_in, token_out))

    def test_inherited_policy_still_refuses_the_exit(self):
        full, reference = self.full_and_reference(self.OUR_IN, self.OUR_OUT, self.REF_OUT)
        ok, reason, risk = assess_quote(sell(self.THEIR_IN, self.THEIR_OUT), full, reference,
                                        INHERIT, GAS_PRICE, NOW)
        self.assertEqual((ok, reason), (False, "adverse_price_deviation_exceeded"))
        self.assertEqual(risk["adverse_price_deviation_bps"], "7320")
        self.assertEqual(risk["adverse_deviation_cap_bps"], "500")

    def test_disabled_sell_checks_record_but_do_not_refuse(self):
        # Proceeds of 12_000 raw ($0.012) clear the floor; deviation and impact
        # are far over the buy-side caps and must only be recorded.
        full, reference = self.full_and_reference(100 * self.OUR_IN, 12_000, 1_000)
        ok, reason, risk = assess_quote(sell(self.THEIR_IN, self.THEIR_OUT), full, reference,
                                        SELL_OFF, GAS_PRICE, NOW)
        self.assertEqual((ok, reason), (True, None), risk)
        self.assertEqual(risk["adverse_price_deviation_bps"], "8800")
        self.assertEqual(risk["estimated_price_impact_bps"], "8800")
        self.assertEqual(risk["adverse_deviation_cap_bps"], str(PRICE_CHECK_DISABLED_BPS))
        self.assertEqual(risk["price_impact_cap_bps"], str(PRICE_CHECK_DISABLED_BPS))
        self.assertIn("estimated_price_impact_bps", risk)
        self.assertEqual(risk["price_check_side"], "SELL")
        # Slippage protection is untouched: the on-chain minimum is still derived.
        self.assertEqual(risk["minimum_amount_out_raw"], str(12_000 * 9_700 // 10_000))

    def test_dust_exit_is_refused_by_the_proceeds_floor_not_by_ratios(self):
        full, reference = self.full_and_reference(self.OUR_IN, self.OUR_OUT, self.REF_OUT)
        ok, reason, risk = assess_quote(sell(self.THEIR_IN, self.THEIR_OUT), full, reference,
                                        SELL_OFF, GAS_PRICE, NOW)
        self.assertEqual((ok, reason), (False, "sell_proceeds_below_floor"))
        self.assertNotIn("adverse_price_deviation_bps", risk)

    def test_sell_without_source_price_passes_only_when_deviation_is_disabled(self):
        full, reference = self.full_and_reference(100 * self.OUR_IN, 12_000, 120)
        unpriced = sell(0, 0)
        unpriced.evidence.clear()
        ok, reason, _ = assess_quote(unpriced, full, reference, INHERIT, GAS_PRICE, NOW)
        self.assertEqual((ok, reason), (False, "source_execution_price_missing"))
        ok, reason, risk = assess_quote(unpriced, full, reference, SELL_OFF, GAS_PRICE, NOW)
        self.assertEqual((ok, reason), (True, None), risk)
        self.assertEqual(risk["source_price_comparison"], "skipped_sell_adverse_deviation_disabled")

    def test_early_intent_limit_check_is_not_affected(self):
        # The early lane's sell has no fill yet; it still has to meet the source's
        # own scaled limit price. That is not one of the two disabled checks.
        full, reference = self.full_and_reference(100 * self.OUR_IN, 12_000, 120)
        intent = sell(0, 0, stage="intent", exact_in=True, amount_in_raw=str(100 * self.OUR_IN),
                      amount_limit_raw="20000")
        intent.evidence.clear()
        for policy in (INHERIT, SELL_OFF):
            ok, reason, risk = validate_quote(intent, full, policy, NOW)
            self.assertEqual((ok, reason), (False, "intent_price_limit_not_met"))
            self.assertEqual(risk["scaled_source_minimum_out_raw"], "20000")

    def test_buy_side_is_unchanged_by_sell_overrides(self):
        # They bought 1e21 token for $1000; our 0.1 USDG buys 9% fewer per dollar
        # and our own order moves the pool 7%. Both remain refusals on a buy.
        theirs = buy(1_000_000_000, 10 ** 21)
        full, reference = self.full_and_reference(100_000, 91 * 10 ** 15, 10 ** 15,
                                                  token_in=R.USDG, token_out=TOKEN)
        for policy in (INHERIT, SELL_OFF):
            ok, reason, risk = assess_quote(theirs, full, reference, policy, GAS_PRICE, NOW)
            self.assertEqual((ok, reason), (False, "adverse_price_deviation_exceeded"))
            self.assertEqual(risk["adverse_deviation_cap_bps"], "500")
        fair = buy(1_000_000_000, 91 * 10 ** 19)  # same price as our quote
        for policy in (INHERIT, SELL_OFF):
            ok, reason, risk = assess_quote(fair, full, reference, policy, GAS_PRICE, NOW)
            self.assertEqual((ok, reason), (False, "price_impact_exceeded"))
            self.assertEqual(risk["price_impact_cap_bps"], "500")
        # A buy output of 651 raw token is fine: the sell floor is sell-only.
        tiny_full, tiny_ref = self.full_and_reference(100_000, 651, 6, token_in=R.USDG, token_out=TOKEN)
        ok, reason, _ = assess_quote(fair, tiny_full, tiny_ref, SELL_OFF, GAS_PRICE, NOW)
        self.assertNotEqual(reason, "sell_proceeds_below_floor")

    def test_market_assessment_honours_side_only_when_asked(self):
        # Sells whose output asset differs from the source's (no source price to
        # compare) and position marks both go through assess_market_quote.
        full, reference = self.full_and_reference(66_165_959_965_639_583_152, 153_056, 2_451)
        ok, reason, _ = assess_market_quote(full, reference, SELL_OFF, GAS_PRICE, NOW)
        self.assertEqual((ok, reason), (False, "price_impact_exceeded"))
        ok, reason, risk = assess_market_quote(full, reference, SELL_OFF, GAS_PRICE, NOW, side="SELL")
        self.assertEqual((ok, reason), (True, None), risk)
        self.assertEqual(risk["estimated_price_impact_bps"], "3755")
        ok, reason, _ = assess_market_quote(full, reference, SELL_OFF, GAS_PRICE, NOW, side="BUY")
        self.assertEqual((ok, reason), (False, "price_impact_exceeded"))

    def test_policy_validation(self):
        for field, bad in (("sell_max_adverse_deviation_bps", 10_001),
                           ("sell_max_adverse_deviation_bps", -1),
                           ("sell_max_price_impact_bps", True),
                           ("sell_max_price_impact_bps", "500"),
                           ("sell_min_amount_out_raw", "0"),
                           ("sell_min_amount_out_raw", 10_000),
                           ("sell_min_amount_out_raw", "1e3")):
            with self.subTest(field=field, bad=bad):
                with self.assertRaises(ValueError):
                    QuotePolicy(**{field: bad})
        policy = QuotePolicy()
        self.assertEqual(policy.adverse_deviation_cap_bps("SELL"), policy.max_adverse_deviation_bps)
        self.assertEqual(policy.price_impact_cap_bps("SELL"), policy.max_price_impact_bps)
        self.assertEqual(policy.output_floor("SELL"), (1, "quote_has_insufficient_output"))
        self.assertEqual(SELL_OFF.output_floor("BUY"), (1, "quote_has_insufficient_output"))
        self.assertEqual(SELL_OFF.output_floor(None), (1, "quote_has_insufficient_output"))

    def test_config_accepts_and_rejects_the_optional_fields(self):
        document = {
            "version": 1, "strategy_version": "t", "trigger_mode": "evidenced",
            "shadow_trigger_modes": [],
            "quote_policy": {"max_age_seconds": 6.0, "max_adverse_deviation_bps": 500,
                             "max_price_impact_bps": 500, "max_slippage_bps": 300,
                             "max_gas_cost_wei": "1000000000000000", "min_amount_out_raw": "1",
                             "sell_max_adverse_deviation_bps": 10000,
                             "sell_max_price_impact_bps": 10000,
                             "sell_min_amount_out_raw": "10000"},
            "allowed_protocols": ["kyber"], "allowed_assets": [R.USDG], "allowed_routes": [],
            "wallets": [{"wallet": WALLET, "label": "w",
                         "budget_limits": {"USDG": "1000000"},
                         "buy_rules": {"USDG": {"mode": "fixed", "fixed_amount_raw": "100000"}},
                         "sell_rule": {"mode": "proportional", "ratio_ppm": 1000000},
                         "execution_providers": ["kyber"]}],
        }
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "paper.json"
            path.write_text(json.dumps(document))
            policy = load_paper_config(str(path)).quote_policy
            self.assertEqual(policy.sell_max_adverse_deviation_bps, PRICE_CHECK_DISABLED_BPS)
            self.assertEqual(policy.sell_min_amount_out_raw, "10000")
            del document["quote_policy"]["sell_min_amount_out_raw"]
            document["quote_policy"]["sell_typo"] = 1
            path.write_text(json.dumps(document))
            with self.assertRaises(ValueError):
                load_paper_config(str(path))


if __name__ == "__main__":
    unittest.main()
