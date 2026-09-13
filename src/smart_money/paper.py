"""Pure policy helpers for read-only paper copy trading."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import time

from . import registry as R
from .models import Signal, address
from .quotes import QuotePolicy, assess_market_quote, assess_quote
from .rpc import RpcError

RATIO_SCALE = 1_000_000
TRIGGER_MODES = frozenset({
    "feed_intent", "receipt_success", "swap_evidenced",
    "relay_sell_evidenced", "relay_buy_evidenced", "evidenced",
})
BUDGET_BUCKETS = frozenset({"USDG", "ETH_WETH"})


def normalized_route_key(protocol: str, assets: list[str],
                         hop_parameters: list[tuple]) -> str:
    normalized_assets = tuple(address(item) for item in assets)
    forward = (normalized_assets, tuple(hop_parameters))
    reverse = (tuple(reversed(normalized_assets)), tuple(reversed(hop_parameters)))
    chosen = min(forward, reverse)
    return json.dumps([protocol, chosen[0], chosen[1]], separators=(",", ":"))


def signal_route_key(signal: Signal) -> str | None:
    try:
        if signal.protocol == "v2":
            assets = signal.evidence.get("route")
            if not isinstance(assets, list) or not 2 <= len(assets) <= 8:
                return None
            parameters = [tuple() for _ in range(len(assets) - 1)]
        elif signal.protocol == "v3":
            hops = signal.evidence.get("hops")
            if isinstance(hops, list) and hops:
                assets = [hops[0]["token_in"], *(hop["token_out"] for hop in hops)]
                parameters = [(int(hop["fee"]),) for hop in hops]
            else:
                fee = signal.evidence.get("fee")
                if fee is None:
                    return None
                assets = [signal.token_in, signal.token_out]
                parameters = [(int(fee),)]
        elif signal.protocol == "v4":
            hops = signal.evidence.get("v4_hops")
            if isinstance(hops, list) and hops:
                assets = [hops[0]["token_in"], *(hop["token_out"] for hop in hops)]
                parameters = []
                for hop in hops:
                    key = hop["pool_key"]
                    hook_data = hop.get("hook_data", "0x")
                    if (not isinstance(hook_data, str) or not hook_data.startswith("0x")
                            or len(hook_data) > 8194):
                        return None
                    bytes.fromhex(hook_data[2:])
                    parameters.append((int(key[2]), int(key[3]), address(key[4]),
                                       hook_data.lower()))
            else:
                key = signal.evidence.get("pool_key")
                if not isinstance(key, list) or len(key) != 5:
                    return None
                assets = [signal.token_in, signal.token_out]
                hook_data = signal.evidence.get("hook_data", "0x")
                if (not isinstance(hook_data, str) or not hook_data.startswith("0x")
                        or len(hook_data) > 8194):
                    return None
                bytes.fromhex(hook_data[2:])
                parameters = [(int(key[2]), int(key[3]), address(key[4]),
                               hook_data.lower())]
        elif signal.protocol in {"0x", "kyber", "relay_solver"}:
            assets = [signal.token_in, signal.token_out]
            parameters = [tuple()]
        else:
            return None
        if assets[0] != signal.token_in or assets[-1] != signal.token_out:
            return None
        return normalized_route_key(signal.protocol, assets, parameters)
    except (KeyError, TypeError, ValueError, IndexError):
        return None


def execution_quote_signal(source: Signal, routes: tuple[dict, ...] | None,
                           output_asset: str | None = None) -> Signal:
    """Select one validated local quote route without changing source attribution."""
    output_asset = address(output_asset) if output_asset is not None else source.token_out
    if source.protocol in {"v2", "v3", "v4"} and output_asset == source.token_out:
        return source
    if source.protocol not in {"v2", "v3", "v4", "0x", "kyber", "relay_solver"}:
        raise ValueError("source protocol has no paper execution route")
    matches = []
    definitions = list(routes or ())
    dynamic = source.evidence.get("local_execution_route")
    if isinstance(dynamic, dict):
        definitions.append(dynamic)
    for definition in definitions:
        assets = definition.get("assets") if isinstance(definition, dict) else None
        if (not isinstance(assets, list) or len(assets) < 2
                or {assets[0], assets[-1]} != {source.token_in, output_asset}):
            continue
        forward = assets[0] == source.token_in
        ordered_assets = list(assets if forward else reversed(assets))
        protocol = definition.get("protocol")
        evidence = deepcopy(source.evidence)
        evidence.update({
            "paper_execution_source_protocol": source.protocol,
            "paper_execution_route": deepcopy(definition),
        })
        if protocol == "v2":
            evidence["route"] = ordered_assets
            contract = R.V2_ROUTER
        elif protocol == "v3":
            fees = definition.get("fees")
            if not isinstance(fees, list) or len(fees) != len(assets) - 1:
                continue
            ordered_fees = list(fees if forward else reversed(fees))
            evidence["hops"] = [
                {"token_in": ordered_assets[i], "token_out": ordered_assets[i + 1],
                 "fee": ordered_fees[i]}
                for i in range(len(ordered_fees))
            ]
            contract = R.V3_QUOTER
        elif protocol == "v4":
            fields = [definition.get(name) for name in (
                "fees", "tick_spacings", "hooks", "hook_data")]
            if any(not isinstance(value, list) or len(value) != len(assets) - 1
                   for value in fields):
                continue
            fees, ticks, hooks, hook_data = [
                list(value if forward else reversed(value)) for value in fields]
            evidence["v4_hops"] = []
            for i, (fee, tick, hook, data) in enumerate(
                    zip(fees, ticks, hooks, hook_data)):
                token_in, token_out = ordered_assets[i:i + 2]
                currency0, currency1 = sorted((token_in, token_out))
                evidence["v4_hops"].append({
                    "token_in": token_in, "token_out": token_out,
                    "pool_key": [currency0, currency1, fee, tick, hook],
                    "hook_data": data,
                })
            contract = R.V4_QUOTER
        else:
            continue
        matches.append(replace(
            source, protocol=protocol, contract=contract, token_out=output_asset,
            evidence=evidence,
            amount_out_raw=None, amount_limit_raw=None, exact_in=True))
    if len(matches) != 1:
        raise ValueError("source asset pair does not select one local execution route")
    return matches[0]


def budget_bucket(asset: str) -> str | None:
    asset = address(asset)
    if asset == R.USDG:
        return "USDG"
    if asset in {R.NATIVE, R.WETH}:
        return "ETH_WETH"
    return None


@dataclass(frozen=True)
class AmountRule:
    mode: str
    ratio_ppm: int | None = None
    fixed_amount_raw: str | None = None

    def __post_init__(self):
        if self.mode == "proportional":
            if not isinstance(self.ratio_ppm, int) or not 1 <= self.ratio_ppm <= RATIO_SCALE:
                raise ValueError("ratio_ppm must be between 1 and 1000000")
            if self.fixed_amount_raw is not None:
                raise ValueError("proportional mode cannot have a fixed amount")
        elif self.mode == "fixed":
            if self.ratio_ppm is not None:
                raise ValueError("fixed mode cannot have a ratio")
            if (not isinstance(self.fixed_amount_raw, str)
                    or not self.fixed_amount_raw.isdecimal()
                    or int(self.fixed_amount_raw) <= 0):
                raise ValueError("fixed amount must be a positive decimal integer string")
        else:
            raise ValueError("unknown amount mode")


def planned_input_amount(signal: Signal, rule: AmountRule) -> tuple[str | None, str | None]:
    """Return a raw integer amount and bucket, or fail closed with a reason."""
    if signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}:
        return None, "not_a_supported_trade_signal"
    budget_asset = signal.token_out if signal.behavior == "SELL" else signal.token_in
    if not budget_asset or (bucket := budget_bucket(budget_asset)) is None:
        return None, ("output_asset_has_no_budget_bucket" if signal.behavior == "SELL"
                      else "input_asset_has_no_budget_bucket")
    if rule.mode == "proportional":
        actual = signal.evidence.get("actual_input_debit_raw")
        if actual is None or not isinstance(actual, str) or not actual.isdecimal() or int(actual) <= 0:
            return None, "verified_actual_input_missing"
        amount = int(actual) * int(rule.ratio_ppm) // RATIO_SCALE
    else:
        amount = int(rule.fixed_amount_raw)
    if amount <= 0:
        return None, "planned_amount_rounds_to_zero"
    return str(amount), bucket


def trigger_allowed(signal: Signal, mode: str) -> tuple[bool, str | None]:
    if mode not in TRIGGER_MODES:
        raise ValueError("unknown trigger mode")
    if signal.canonical_status == "orphaned":
        return False, "source_signal_orphaned"
    if signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}:
        return False, "not_a_trade_signal"
    if signal.stage in {"needs_review", "failed"}:
        return False, "source_signal_not_eligible"
    if mode == "swap_evidenced":
        return (signal.stage == "swap_evidenced",
                None if signal.stage == "swap_evidenced" else "swap_evidence_required")
    if mode in {"relay_sell_evidenced", "relay_buy_evidenced"}:
        return (signal.stage == mode,
                None if signal.stage == mode else f"{mode}_required")
    if mode == "evidenced":
        allowed = signal.stage in {
            "swap_evidenced", "relay_sell_evidenced", "relay_buy_evidenced",
        }
        return allowed, None if allowed else "confirmed_exchange_evidence_required"
    if mode == "receipt_success":
        allowed = signal.execution_status == "success" and signal.stage != "needs_review"
        return allowed, None if allowed else "successful_unambiguous_receipt_required"
    allowed = signal.intent_status == "observed" and signal.fresh
    return allowed, None if allowed else "fresh_feed_intent_required"


def scope_reason(signal: Signal, allowed_protocols: frozenset[str] | None,
                 allowed_assets: frozenset[str] | None,
                 allowed_routes: frozenset[str] | None = None) -> str | None:
    """Validate trusted funding/intermediate assets and evidenced dynamic targets."""
    if allowed_protocols is not None and signal.protocol not in allowed_protocols:
        return "protocol_not_allowed"
    evidenced_stages = {
        "swap_evidenced", "relay_buy_evidenced", "relay_sell_evidenced",
    }
    dynamic_target = None
    if signal.execution_status == "success" and signal.stage in evidenced_stages:
        if signal.behavior == "BUY":
            dynamic_target = signal.token_out
        elif signal.behavior == "SELL":
            dynamic_target = signal.token_in
    if allowed_assets is not None:
        route_assets = {signal.token_in, signal.token_out}
        if signal.protocol == "v2" and isinstance(signal.evidence.get("route"), list):
            route_assets.update(signal.evidence["route"])
        elif signal.protocol == "v3" and isinstance(signal.evidence.get("hops"), list):
            for hop in signal.evidence["hops"]:
                if isinstance(hop, dict):
                    route_assets.update((hop.get("token_in"), hop.get("token_out")))
        elif signal.protocol == "v4" and isinstance(signal.evidence.get("v4_hops"), list):
            for hop in signal.evidence["v4_hops"]:
                if isinstance(hop, dict):
                    route_assets.update((hop.get("token_in"), hop.get("token_out")))
        if dynamic_target is not None:
            route_assets.discard(dynamic_target)
        if None in route_assets or not route_assets <= allowed_assets:
            return "asset_not_allowed"
    if allowed_routes is not None and signal.protocol not in {"0x", "kyber", "relay_solver"}:
        key = signal_route_key(signal)
        if key is None or (key not in allowed_routes and dynamic_target is None):
            return "route_not_allowed"
    return None


@dataclass(frozen=True)
class PaperDecision:
    decision_id: str
    accepted: bool
    reason: str | None
    proposal_id: str | None = None


class PaperEngine:
    """Build auditable paper proposals; it has no signing or broadcasting surface."""

    def __init__(self, store, quoter, quote_policy: QuotePolicy,
                 strategy_version: str, trigger_mode: str = "swap_evidenced",
                 allowed_protocols: frozenset[str] | None = None,
                 allowed_assets: frozenset[str] | None = None,
                 allowed_routes: frozenset[str] | None = None,
                 wallet_labels: dict[str, str] | None = None,
                 wallet_contexts: dict[str, dict] | None = None,
                 config_snapshot_hash: str | None = None,
                 execution_routes: tuple[dict, ...] | None = None,
                 shadow_only: bool = False):
        if trigger_mode not in TRIGGER_MODES or not strategy_version:
            raise ValueError("invalid paper engine configuration")
        self.store = store
        self.quoter = quoter
        self.quote_policy = quote_policy
        self.strategy_version = strategy_version
        self.trigger_mode = trigger_mode
        self.allowed_protocols = allowed_protocols
        self.allowed_assets = allowed_assets
        self.allowed_routes = allowed_routes
        self.wallet_labels = wallet_labels or {}
        self.wallet_contexts = wallet_contexts or {}
        self.config_snapshot_hash = config_snapshot_hash
        self.execution_routes = execution_routes
        self.shadow_only = shadow_only

    def _scope_reason(self, signal: Signal) -> str | None:
        return scope_reason(
            signal, self.allowed_protocols, self.allowed_assets, self.allowed_routes)

    def _id(self, signal: Signal, kind: str) -> str:
        relationship = self.wallet_contexts.get(signal.wallet, {}).get("relationship_id", "")
        value = f"{kind}:{signal.event_id}:{self.trigger_mode}:{self.strategy_version}"
        if relationship:
            value += f":{relationship}"
        if self.config_snapshot_hash:
            value += f":snapshot:{self.config_snapshot_hash}"
        return hashlib.sha256(value.encode()).hexdigest()

    def _attribution(self, signal: Signal) -> dict:
        context = self.wallet_contexts.get(signal.wallet, {})
        source_input = signal.evidence.get(
            "actual_input_debit_raw", signal.amount_in_raw)
        source_output = signal.evidence.get(
            "actual_output_credit_raw", signal.amount_out_raw)
        result = {
            "follower_wallet": context.get("follower_wallet"),
            "relationship_id": context.get("relationship_id"),
            "config_snapshot_hash": self.config_snapshot_hash,
            "smart_wallet": signal.wallet, "source_event_id": signal.event_id,
            "smart_wallet_label": self.wallet_labels.get(signal.wallet, signal.wallet),
            "source_tx_hash": signal.tx_hash, "source_mode": signal.mode,
            "source_behavior": signal.behavior, "source_stage": signal.stage,
            "source_amount_in_raw": source_input,
            "source_amount_out_raw": source_output,
            "trigger_mode": self.trigger_mode, "strategy_version": self.strategy_version,
        }
        route = signal.evidence.get("local_execution_route")
        if isinstance(route, dict):
            result["local_execution_route"] = deepcopy(route)
        return result

    def _ledger_wallet(self, signal: Signal) -> str:
        context = self.wallet_contexts.get(signal.wallet, {})
        return context.get("ledger_scope") or signal.wallet

    def _ledger_source_event(self, signal: Signal) -> str:
        relationship = self.wallet_contexts.get(signal.wallet, {}).get("relationship_id")
        value = (f"{signal.event_id}:relationship:{relationship}"
                 if relationship else signal.event_id)
        if self.config_snapshot_hash:
            value += f":snapshot:{self.config_snapshot_hash}"
        return value

    def _decision(self, signal: Signal, accepted: bool, reason: str | None,
                  payload: dict, proposal_id: str | None = None) -> PaperDecision:
        decision_id = self._id(signal, "decision")
        context = self.wallet_contexts.get(signal.wallet, {})
        payload = {
            "follower_wallet": context.get("follower_wallet"),
            "relationship_id": context.get("relationship_id"),
            "config_snapshot_hash": self.config_snapshot_hash,
            "source_event_id": signal.event_id,
            **payload,
        }
        self.store.record_paper_decision(
            decision_id, self._ledger_source_event(signal), self.trigger_mode,
            self.strategy_version,
            accepted, reason, payload,
        )
        return PaperDecision(decision_id, accepted, reason, proposal_id)

    async def propose_buy(self, signal: Signal, amount_rule: AmountRule,
                          now: float | None = None) -> PaperDecision:
        if signal.behavior not in {"BUY", "TOKEN_SWAP"}:
            return self._decision(signal, False, "not_a_supported_buy_signal", {
                "source_signal": signal.to_dict(),
            })
        if scope_reason := self._scope_reason(signal):
            return self._decision(signal, False, scope_reason, {
                "source_signal": signal.to_dict(),
            })
        allowed, reason = trigger_allowed(signal, self.trigger_mode)
        if not allowed:
            return self._decision(signal, False, reason, {"source_signal": signal.to_dict()})
        amount, bucket_or_reason = planned_input_amount(signal, amount_rule)
        if amount is None:
            return self._decision(
                signal, False, bucket_or_reason, {"source_signal": signal.to_dict()})
        try:
            try:
                quote_signal = execution_quote_signal(signal, self.execution_routes)
            except ValueError:
                discover = getattr(self.quoter, "discover_v3_route", None)
                if signal.protocol != "relay_solver" or not callable(discover):
                    raise
                signal.evidence["local_execution_route"] = await discover(signal, amount)
                self.store.put(signal)
                quote_signal = execution_quote_signal(signal, self.execution_routes)
            quote, reference, gas_price = await self.quoter.quote_with_reference(
                quote_signal, amount)
        except (RpcError, ValueError) as exc:
            return self._decision(signal, False, "quote_unavailable", {
                "source_signal": signal.to_dict(), "quote_error_type": type(exc).__name__,
            })
        accepted, reason, risk = assess_quote(
            signal, quote, reference, self.quote_policy, gas_price, now)
        quote_payload = {
            "quote": quote.to_dict(), "reference_quote": reference.to_dict(),
            "gas_price_wei": gas_price, "risk": risk,
            "execution_signal": quote_signal.to_dict(),
        }
        if not accepted:
            return self._decision(signal, False, reason, {
                "source_signal": signal.to_dict(), **quote_payload,
            })
        if self.shadow_only:
            return self._decision(signal, True, None, {
                "source_signal": signal.to_dict(), "shadow_only": True, **quote_payload,
            })
        proposal_id = self._id(signal, "proposal")
        attribution = self._attribution(signal)
        reserved, reserve_reason = self.store.reserve_paper_proposal({
            "proposal_id": proposal_id, "source_event_id": self._ledger_source_event(signal),
            "source_tx_hash": signal.tx_hash, "wallet": self._ledger_wallet(signal),
            "trigger_mode": self.trigger_mode, "strategy_version": self.strategy_version,
            "input_asset": signal.token_in, "output_asset": signal.token_out,
            "budget_bucket": bucket_or_reason, "amount_in_raw": amount,
            "quote": quote_payload, "attribution": attribution,
        })
        if not reserved:
            return self._decision(signal, False, reserve_reason, {
                "source_signal": signal.to_dict(), **quote_payload,
            })
        return self._decision(signal, True, None, {
            "source_signal": signal.to_dict(), "proposal_id": proposal_id, **quote_payload,
        }, proposal_id)

    async def propose_sell(self, signal: Signal, amount_rule: AmountRule,
                           now: float | None = None) -> PaperDecision:
        if signal.behavior != "SELL":
            return self._decision(signal, False, "not_a_supported_sell_signal", {
                "source_signal": signal.to_dict(),
            })
        if scope_reason := self._scope_reason(signal):
            return self._decision(signal, False, scope_reason, {
                "source_signal": signal.to_dict(),
            })
        allowed, reason = trigger_allowed(signal, self.trigger_mode)
        if not allowed:
            return self._decision(signal, False, reason, {"source_signal": signal.to_dict()})
        if amount_rule.mode == "proportional":
            actual = signal.evidence.get("actual_input_debit_raw")
            bucket_or_reason = budget_bucket(signal.token_out)
            if (not isinstance(actual, str) or not actual.isdecimal()
                    or int(actual) <= 0 or bucket_or_reason is None):
                return self._decision(
                    signal, False, "verified_actual_input_missing", {
                        "source_signal": signal.to_dict()})
            amount, amount_reason = self.store.paper_proportional_sell_amount(
                self._ledger_wallet(signal), signal.token_in, actual,
                amount_rule.ratio_ppm)
            if amount is None:
                return self._decision(signal, False, amount_reason, {
                    "source_signal": signal.to_dict()})
        else:
            amount, bucket_or_reason = planned_input_amount(signal, amount_rule)
            if amount is None:
                return self._decision(
                    signal, False, bucket_or_reason, {
                        "source_signal": signal.to_dict()})
        principal_asset, principal_reason = self.store.paper_sell_principal_asset(
            self._ledger_wallet(signal), signal.token_in, amount)
        if principal_asset is None:
            return self._decision(signal, False, principal_reason, {
                "source_signal": signal.to_dict(),
            })
        try:
            try:
                quote_signal = execution_quote_signal(
                    signal, self.execution_routes, principal_asset)
            except ValueError:
                route, route_reason = self.store.paper_sell_execution_route(
                    self._ledger_wallet(signal), signal.token_in,
                    principal_asset, amount)
                if route is None:
                    return self._decision(signal, False, route_reason, {
                        "source_signal": signal.to_dict(),
                    })
                signal.evidence["local_execution_route"] = route
                quote_signal = execution_quote_signal(
                    signal, self.execution_routes, principal_asset)
            quote, reference, gas_price = await self.quoter.quote_with_reference(
                quote_signal, amount)
        except (RpcError, ValueError) as exc:
            return self._decision(signal, False, "quote_unavailable", {
                "source_signal": signal.to_dict(), "quote_error_type": type(exc).__name__,
            })
        if principal_asset == signal.token_out:
            accepted, reason, risk = assess_quote(
                signal, quote, reference, self.quote_policy, gas_price, now)
        else:
            accepted, reason, risk = assess_market_quote(
                quote, reference, self.quote_policy, gas_price, now)
            risk["source_price_comparison"] = "not_comparable_output_asset_changed"
        quote_payload = {
            "quote": quote.to_dict(), "reference_quote": reference.to_dict(),
            "gas_price_wei": gas_price, "risk": risk,
            "execution_signal": quote_signal.to_dict(),
        }
        if not accepted:
            return self._decision(signal, False, reason, {
                "source_signal": signal.to_dict(), **quote_payload,
            })
        if self.shadow_only:
            return self._decision(signal, True, None, {
                "source_signal": signal.to_dict(), "shadow_only": True, **quote_payload,
            })
        proposal_id = self._id(signal, "proposal")
        attribution = self._attribution(signal)
        reserved, reserve_reason = self.store.reserve_paper_sell({
            "proposal_id": proposal_id, "source_event_id": self._ledger_source_event(signal),
            "source_tx_hash": signal.tx_hash, "wallet": self._ledger_wallet(signal),
            "trigger_mode": self.trigger_mode, "strategy_version": self.strategy_version,
            "input_asset": signal.token_in, "output_asset": principal_asset,
            "budget_bucket": budget_bucket(principal_asset), "amount_in_raw": amount,
            "quote": quote_payload, "attribution": attribution,
        })
        if not reserved:
            return self._decision(signal, False, reserve_reason, {
                "source_signal": signal.to_dict(), **quote_payload,
            })
        return self._decision(signal, True, None, {
            "source_signal": signal.to_dict(), "proposal_id": proposal_id, **quote_payload,
        }, proposal_id)


@dataclass(frozen=True)
class PaperExecution:
    proposal_id: str
    status: str
    reason: str | None = None
    fill_id: str | None = None


class PaperExecutor:
    """Requote and settle a reserved proposal in the local paper ledger only."""

    def __init__(self, store, quoter, quote_policy: QuotePolicy,
                 execution_routes: tuple[dict, ...] | None = None):
        self.store = store
        self.quoter = quoter
        self.quote_policy = quote_policy
        self.execution_routes = execution_routes

    @staticmethod
    def _id(proposal_id: str, kind: str) -> str:
        return hashlib.sha256(f"paper:{kind}:{proposal_id}".encode()).hexdigest()

    async def execute(self, signal: Signal, proposal_id: str,
                      now: float | None = None) -> PaperExecution:
        proposal = self.store.paper_proposal(proposal_id)
        if proposal is None or proposal["status"] != "reserved":
            return PaperExecution(proposal_id, "not_reserved", "proposal_not_reserved")
        try:
            quote_signal = execution_quote_signal(
                signal, self.execution_routes, proposal["output_asset"])
            stored_execution = (proposal.get("quote") or {}).get("execution_signal")
            if stored_execution is not None and stored_execution != quote_signal.to_dict():
                raise ValueError("paper execution route changed after reservation")
            quote, reference, gas_price = await self.quoter.quote_with_reference(
                quote_signal, proposal["amount_in_raw"])
            if proposal["output_asset"] == signal.token_out:
                accepted, reason, risk = assess_quote(
                    signal, quote, reference, self.quote_policy, gas_price, now)
            else:
                accepted, reason, risk = assess_market_quote(
                    quote, reference, self.quote_policy, gas_price, now)
                risk["source_price_comparison"] = "not_comparable_output_asset_changed"
        except (RpcError, ValueError):
            self.store.cancel_paper_proposal(proposal_id, "fill_requote_unavailable")
            return PaperExecution(proposal_id, "cancelled", "fill_requote_unavailable")
        original_risk = (proposal.get("quote") or {}).get("risk", {})
        original_minimum = original_risk.get("minimum_amount_out_raw")
        if (not accepted or not isinstance(original_minimum, str)
                or not original_minimum.isdecimal()
                or int(quote.amount_out_raw) < int(original_minimum)):
            cancel_reason = reason or "fill_below_original_minimum"
            self.store.cancel_paper_proposal(proposal_id, cancel_reason)
            return PaperExecution(proposal_id, "cancelled", cancel_reason)
        observed_at = datetime.fromtimestamp(quote.observed_at, timezone.utc).isoformat()
        filled_at = datetime.fromtimestamp(time.time() if now is None else now,
                                           timezone.utc).isoformat()
        common = {
            "order_id": self._id(proposal_id, "order"),
            "fill_id": self._id(proposal_id, "fill"),
            "amount_out_raw": quote.amount_out_raw,
            "fee_asset": proposal["output_asset"], "fee_amount_raw": "0",
            "gas_cost_wei": risk["estimated_gas_cost_wei"],
            "quote_observed_at": observed_at, "filled_at": filled_at,
        }
        if signal.behavior == "SELL":
            filled = self.store.fill_paper_sell(proposal_id, common)
        else:
            filled = self.store.fill_paper_buy(proposal_id, {
                **common, "lot_id": self._id(proposal_id, "lot"),
            })
        return PaperExecution(proposal_id, "filled" if filled else "not_reserved",
                              None if filled else "proposal_not_reserved",
                              common["fill_id"] if filled else None)


def reverse_quote_signal(source: Signal, principal_asset: str) -> Signal:
    """Build only a verified route reversal for marking an attributed open lot."""
    if (source.protocol not in {"v2", "v3", "v4"} or not source.token_in
            or not source.token_out or source.token_in != principal_asset):
        raise ValueError("source route cannot value this lot")
    evidence = deepcopy(source.evidence)
    evidence.pop("actual_input_debit_raw", None)
    evidence.pop("actual_output_credit_raw", None)
    if source.protocol == "v2":
        route = evidence.get("route")
        if not isinstance(route, list) or len(route) < 2:
            raise ValueError("V2 valuation route missing")
        evidence["route"] = list(reversed(route))
    elif source.protocol == "v3":
        hops = evidence.get("hops")
        if not isinstance(hops, list) or not hops:
            fee = evidence.get("fee")
            if fee is None:
                raise ValueError("V3 valuation route missing")
            hops = [{"token_in": source.token_in, "token_out": source.token_out,
                     "fee": fee}]
        evidence["hops"] = [{"token_in": hop["token_out"],
                             "token_out": hop["token_in"], "fee": hop["fee"]}
                            for hop in reversed(hops)]
    elif evidence.get("v4_hops"):
        hops = evidence["v4_hops"]
        if not isinstance(hops, list) or not hops:
            raise ValueError("V4 valuation route missing")
        evidence["v4_hops"] = [{
            **deepcopy(hop), "token_in": hop["token_out"],
            "token_out": hop["token_in"],
        } for hop in reversed(hops)]
    return replace(source, behavior="SELL", token_in=source.token_out,
                   token_out=source.token_in, amount_in_raw=None,
                   amount_out_raw=None, amount_limit_raw=None, exact_in=True,
                   evidence=evidence)


@dataclass(frozen=True)
class PaperMark:
    mark_id: str
    lot_id: str
    gross_value_raw: str
    unrealized_pnl_raw: str
    gas_cost_wei: str
    block_number: int


class PaperValuator:
    """Create immutable, block-pinned marks; it never mutates inventory or budgets."""

    def __init__(self, store, quoter, quote_policy: QuotePolicy,
                 execution_routes: tuple[dict, ...] | None = None):
        self.store = store
        self.quoter = quoter
        self.quote_policy = quote_policy
        self.execution_routes = execution_routes

    async def mark(self, lot_id: str, source_signal: Signal,
                   now: float | None = None) -> PaperMark:
        position = self.store.paper_position(lot_id)
        if position is None or position["status"] != "open":
            raise ValueError("open paper position required")
        buy_signal = execution_quote_signal(
            source_signal, self.execution_routes, position["token"])
        signal = reverse_quote_signal(buy_signal, position["principal_asset"])
        quote, reference, gas_price = await self.quoter.quote_with_reference(
            signal, position["token_remaining_raw"])
        accepted, reason, risk = assess_market_quote(
            quote, reference, self.quote_policy, gas_price, now)
        if not accepted:
            raise ValueError(reason)
        pnl = int(quote.amount_out_raw) - int(position["principal_remaining_raw"])
        mark_id = hashlib.sha256(
            f"mark:{lot_id}:{quote.block_hash}:{quote.amount_in_raw}".encode()).hexdigest()
        observed_at = datetime.fromtimestamp(quote.observed_at, timezone.utc).isoformat()
        self.store.record_paper_position_mark({
            "mark_id": mark_id, "lot_id": lot_id,
            "principal_asset": position["principal_asset"],
            "token_amount_raw": position["token_remaining_raw"],
            "gross_value_raw": quote.amount_out_raw,
            "principal_remaining_raw": position["principal_remaining_raw"],
            "unrealized_pnl_raw": str(pnl),
            "gas_cost_wei": risk["estimated_gas_cost_wei"],
            "block_number": quote.block_number, "block_hash": quote.block_hash,
            "quote_source": quote.source, "quote_observed_at": observed_at,
            "risk": risk,
        })
        return PaperMark(mark_id, lot_id, quote.amount_out_raw, str(pnl),
                         risk["estimated_gas_cost_wei"], quote.block_number)
