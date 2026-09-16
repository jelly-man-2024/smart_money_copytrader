"""Read-only decision checks for a typed verified Feed intent.

No reservations, signatures or broadcasting. Caller must still atomically claim
the operation with funds, and repeat trial/config/preflight checks at execution.
"""
from __future__ import annotations

import time
from copy import deepcopy

from .early_intent import uint, timestamp
from .copy_operation import relationship_operation_key
from .registry import NATIVE
from .early_replay import _bound, _planned_amount
from .models import address
from .quotes import QuotePolicy, assess_quote
from .verified_feed_intent import VerifiedFeedIntent
from .zeroex import ZeroExApiError


class EarlyDecisionEngine:
    def __init__(self, quoter):
        self.quoter = quoter

    async def evaluate(self, intent, policy, portfolio, now=None):
        if not isinstance(intent, VerifiedFeedIntent):
            raise ValueError("typed verified Feed intent required")
        policy, portfolio = deepcopy(policy), deepcopy(portfolio)
        at = time.time() if now is None else now
        attribution = intent.revalidate(at)
        c = intent.candidate
        for snapshot in (policy, portfolio):
            if not 0 <= at - timestamp(snapshot.get("observed_at")) <= 3:
                raise ValueError("early decision snapshot expired or in future")
        if (policy.get("enabled") is not True or policy.get("stop_active") is not False
                or policy.get("smart_wallet") != c.wallet or not policy.get("config_snapshot_hash")):
            raise ValueError("early relationship disabled or mismatched")
        _bound(portfolio, policy)
        follower = address(policy["follower"])
        if follower == NATIVE:
            raise ValueError("early follower is zero")
        key = relationship_operation_key(c.wallet, c.order_id, policy["relationship_id"], follower)
        if key in portfolio["consumed_operation_keys"]:
            raise ValueError("operation_already_consumed")
        if portfolio.get("source_orphaned") is not False:
            raise ValueError("source canonical status unavailable")
        source_protocol = "relay_solver" if c.side == "BUY" else "kyber"
        if (source_protocol not in policy["allowed_protocols"]
                or policy["execution_providers"] not in (["kyber"], ["zeroex"], ["zeroex", "kyber"])):
            raise ValueError("early source protocol or aggregator-only provider required")
        trusted = {address(a) for a in policy["allowed_assets"]}
        if (c.token_in if c.side == "BUY" else c.token_out) not in trusted:
            raise ValueError("funding_asset_not_allowed")
        if c.side == "SELL":
            for lot in portfolio.get("lots", []):
                if lot.get("token") == c.token_in and lot.get("source_position_status") != "confirmed":
                    raise ValueError("source_position_basis_unconfirmed")
        amount, output = _planned_amount(c, policy, portfolio, attribution)
        if amount <= 0 or amount > uint(policy["max_input_raw"]):
            raise ValueError("early planned amount outside limits")
        if output != c.token_out:
            raise ValueError("cross_asset_source_price_basis_missing")
        for provider in policy["execution_providers"]:
            signal = intent.quote_signal(at, provider=provider)
            try:
                quote, reference, gas_price = await self.quoter.quote_with_reference(signal, str(amount))
                break
            except ZeroExApiError:
                if provider != "zeroex" or policy["execution_providers"][-1] != "kyber":
                    raise
        at = time.time() if now is None else now
        intent.revalidate(at)
        for snapshot in (policy, portfolio):
            if not 0 <= at - timestamp(snapshot["observed_at"]) <= 3:
                raise ValueError("early decision snapshot expired after quote")
        if quote.protocol != signal.protocol or quote.amount_in_raw != str(amount):
            raise ValueError("early quote request mismatch")
        accepted, reason, risk = assess_quote(signal, quote, reference,
                                              QuotePolicy(**policy["quote_policy"]), gas_price, at)
        if not accepted:
            raise ValueError(reason)
        return {"decision_checks_passed": True, "copy_eligible": False,
                "reservation_created": False, "relationship_key": key,
                "relationship_id": policy["relationship_id"], "follower": follower,
                "config_snapshot_hash": policy["config_snapshot_hash"],
                "amount_in_raw": str(amount), "input_asset": c.token_in, "output_asset": output,
                "minimum_output_raw": str(max(uint(risk["minimum_amount_out_raw"]),
                                               uint(risk["scaled_source_minimum_out_raw"]))),
                "amount_basis": signal.evidence["amount_basis"],
                "source_position_status": "pending", "source_signal": signal.to_dict(),
                "quote": quote.to_dict(), "reference_quote": reference.to_dict(),
                "risk": risk, "checked_at": at}
