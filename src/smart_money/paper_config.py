"""Strict, secret-free configuration for read-only paper copy trading."""
from __future__ import annotations

from dataclasses import dataclass
from copy import deepcopy
import hashlib
import json
from pathlib import Path

from . import registry as R
from .models import address
from .paper import (
    AmountRule, BUDGET_BUCKETS, SUPPORTED_EXECUTION_PROVIDERS, TRIGGER_MODES,
    normalized_route_key,
)
from .quotes import QuotePolicy

SUPPORTED_PROTOCOLS = frozenset({"v2", "v3", "v4", "0x", "kyber", "relay_solver"})
ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
DEFAULT_EXECUTION_PROVIDERS = ("local",)


def _execution_providers(value) -> tuple[str, ...]:
    if value is None:
        return DEFAULT_EXECUTION_PROVIDERS
    if (not isinstance(value, list) or not value
            or any(not isinstance(item, str) for item in value)
            or len(value) != len(set(value))
            or not set(value) <= SUPPORTED_EXECUTION_PROVIDERS):
        raise ValueError("invalid execution providers")
    return tuple(value)


def _fields(value: dict, allowed: set[str], required: set[str], context: str) -> None:
    if not isinstance(value, dict) or set(value) - allowed or not required <= set(value):
        raise ValueError(f"invalid {context} fields")


def _amount_rule(value: dict) -> AmountRule:
    _fields(value, {"mode", "ratio_ppm", "fixed_amount_raw"}, {"mode"}, "amount rule")
    return AmountRule(value["mode"], value.get("ratio_ppm"), value.get("fixed_amount_raw"))


@dataclass(frozen=True)
class WalletPaperPolicy:
    wallet: str
    label: str
    follower_wallet: str | None
    relationship_id: str | None
    run_mode: str
    budget_limits: dict[str, str]
    buy_rules: dict[str, AmountRule]
    sell_rule: AmountRule
    strategy_version: str
    trigger_mode: str
    shadow_trigger_modes: tuple[str, ...]
    quote_policy: QuotePolicy
    allowed_protocols: frozenset[str]
    allowed_assets: frozenset[str]
    allowed_routes: frozenset[str]
    route_definitions: tuple[dict, ...]
    snapshot_hash: str
    execution_providers: tuple[str, ...] = DEFAULT_EXECUTION_PROVIDERS
    # Which chain this relationship copies on. Defaults to Robinhood Chain so
    # every existing configuration keeps its meaning.
    chain_id: int = R.CHAIN_ID

    @property
    def ledger_scope(self) -> str:
        """Stable public ledger namespace for one follower/smart relationship."""
        if self.follower_wallet and self.relationship_id:
            value = f"{self.follower_wallet}:{self.relationship_id}:{self.wallet}"
            return "relationship:" + hashlib.sha256(value.encode()).hexdigest()
        return self.wallet


@dataclass(frozen=True)
class PaperConfig:
    strategy_version: str
    trigger_mode: str
    shadow_trigger_modes: tuple[str, ...]
    quote_policy: QuotePolicy
    allowed_protocols: frozenset[str]
    allowed_assets: frozenset[str]
    allowed_routes: frozenset[str]
    route_definitions: tuple[dict, ...]
    wallets: dict[str, WalletPaperPolicy]
    relationships: tuple[WalletPaperPolicy, ...]
    snapshot_hash: str

    def policies_for(self, smart_wallet: str, chain_id: int) -> tuple[WalletPaperPolicy, ...]:
        """Policies for this wallet ON THIS CHAIN.

        The same smart wallet is routinely copied on more than one chain, and
        those relationships differ in run mode, settlement asset and budget. The
        chain is therefore required rather than defaulted: selecting by wallet
        alone would let a signal from one chain drive another chain's policy.
        """
        wallet = address(smart_wallet)
        if type(chain_id) is not int:
            raise ValueError("policy lookup requires a chain id")
        return tuple(policy for policy in self.relationships
                     if policy.wallet == wallet and policy.chain_id == chain_id)


def _route_key(value: dict, protocols: set[str], assets: set[str]) -> str:
    if not isinstance(value, dict) or "protocol" not in value or "assets" not in value:
        raise ValueError("invalid allowed route fields")
    protocol = value["protocol"]
    if protocol not in protocols:
        raise ValueError("route protocol is not allowed")
    route_assets = value["assets"]
    if not isinstance(route_assets, list) or not 2 <= len(route_assets) <= 8:
        raise ValueError("invalid route assets")
    route_assets = [address(item) for item in route_assets]
    if not set(route_assets) <= assets:
        raise ValueError("route contains an asset outside the allowlist")
    hop_count = len(route_assets) - 1
    if protocol == "v2":
        _fields(value, {"protocol", "assets"}, {"protocol", "assets"}, "V2 route")
        parameters = [tuple() for _ in range(hop_count)]
    elif protocol == "v3":
        _fields(value, {"protocol", "assets", "fees"},
                {"protocol", "assets", "fees"}, "V3 route")
        fees = value["fees"]
        if (not isinstance(fees, list) or len(fees) != hop_count
                or any(not isinstance(fee, int) or not 0 <= fee < 2 ** 24 for fee in fees)):
            raise ValueError("invalid V3 route fees")
        parameters = [(fee,) for fee in fees]
    elif protocol == "v4":
        _fields(value, {"protocol", "assets", "fees", "tick_spacings", "hooks", "hook_data"},
                {"protocol", "assets", "fees", "tick_spacings", "hooks", "hook_data"},
                "V4 route")
        fees, ticks = value["fees"], value["tick_spacings"]
        hooks, hook_data = value["hooks"], value["hook_data"]
        if (not isinstance(fees, list) or not isinstance(ticks, list)
                or not isinstance(hooks, list) or not isinstance(hook_data, list)
                or len(fees) != hop_count
                or len(ticks) != hop_count or len(hooks) != hop_count
                or len(hook_data) != hop_count
                or any(not isinstance(fee, int) or not 0 <= fee < 2 ** 24 for fee in fees)
                or any(not isinstance(tick, int) or not -(2 ** 23) <= tick < 2 ** 23
                       for tick in ticks)):
            raise ValueError("invalid V4 route parameters")
        hooks = [address(item) for item in hooks]
        for item in hook_data:
            if (not isinstance(item, str) or not item.startswith("0x")
                    or len(item) > 8194):
                raise ValueError("invalid V4 route hook data")
            try:
                bytes.fromhex(item[2:])
            except ValueError as exc:
                raise ValueError("invalid V4 route hook data") from exc
        parameters = list(zip(fees, ticks, hooks,
                              (item.lower() for item in hook_data)))
    else:
        _fields(value, {"protocol", "assets"}, {"protocol", "assets"},
                "aggregator/solver route")
        if hop_count != 1:
            raise ValueError("aggregator/solver route must contain exactly two assets")
        parameters = [tuple()]
    return normalized_route_key(protocol, route_assets, parameters)


def load_paper_config(path: str | Path) -> PaperConfig:
    source = Path(path)
    if not source.is_file() or source.stat().st_size > 1024 * 1024:
        raise ValueError("paper config missing or too large")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid paper config JSON") from exc
    return parse_paper_config(document)


def parse_paper_config(document: dict) -> PaperConfig:
    """Validate an already captured public configuration without file I/O."""
    _fields(document, {
        "version", "strategy_version", "trigger_mode", "shadow_trigger_modes",
        "quote_policy", "allowed_protocols", "allowed_assets", "allowed_routes", "wallets",
    }, {"version", "strategy_version", "quote_policy", "allowed_protocols",
        "allowed_assets", "allowed_routes", "wallets"}, "paper config")
    if document["version"] != 1:
        raise ValueError("unsupported paper config version")
    strategy = document["strategy_version"]
    if not isinstance(strategy, str) or not strategy or len(strategy) > 100:
        raise ValueError("invalid strategy version")
    trigger = document.get("trigger_mode", "swap_evidenced")
    shadows = document.get("shadow_trigger_modes", ["feed_intent", "receipt_success"])
    if trigger not in TRIGGER_MODES or not isinstance(shadows, list):
        raise ValueError("invalid trigger configuration")
    if (len(shadows) != len(set(shadows)) or trigger in shadows
            or any(item not in TRIGGER_MODES for item in shadows)):
        raise ValueError("invalid shadow trigger modes")
    quote_values = document["quote_policy"]
    quote_fields = {
        "max_age_seconds", "max_adverse_deviation_bps", "max_price_impact_bps",
        "max_slippage_bps", "max_gas_cost_wei", "min_amount_out_raw",
        # Optional sell-side overrides; absent means inherit the buy-side value.
        "sell_max_adverse_deviation_bps", "sell_max_price_impact_bps",
        "sell_min_amount_out_raw",
    }
    _fields(quote_values, quote_fields, set(), "quote policy")
    try:
        quote_policy = QuotePolicy(**quote_values)
    except (TypeError, AttributeError) as exc:
        raise ValueError("invalid quote policy values") from exc
    protocols = document["allowed_protocols"]
    if (not isinstance(protocols, list) or not protocols
            or any(not isinstance(item, str) for item in protocols)
            or len(protocols) != len(set(protocols))
            or not set(protocols) <= SUPPORTED_PROTOCOLS):
        raise ValueError("invalid allowed protocols")
    assets = document["allowed_assets"]
    if not isinstance(assets, list) or not assets:
        raise ValueError("allowed assets are required")
    normalized_assets = [address(item) for item in assets]
    if len(normalized_assets) != len(set(normalized_assets)):
        raise ValueError("duplicate allowed asset")
    route_rows = document["allowed_routes"]
    if not isinstance(route_rows, list):
        raise ValueError("allowed routes must be a list")
    route_keys = [_route_key(row, set(protocols), set(normalized_assets))
                  for row in route_rows]
    if len(route_keys) != len(set(route_keys)):
        raise ValueError("duplicate allowed route")
    wallet_rows = document["wallets"]
    if not isinstance(wallet_rows, list) or not wallet_rows:
        raise ValueError("wallet policies are required")
    wallets = {}
    relationships = []
    relationship_keys = set()
    for row in wallet_rows:
        _fields(row, {"wallet", "label", "follower_wallet", "relationship_id", "run_mode",
                      "budget_limits", "buy_rules", "sell_rule", "execution_providers",
                      "chain_id"},
                {"wallet", "budget_limits", "buy_rules", "sell_rule"}, "wallet policy")
        execution_providers = _execution_providers(row.get("execution_providers"))
        wallet = address(row["wallet"])
        if wallet == ZERO_ADDRESS:
            raise ValueError("zero smart wallet is forbidden")
        label = row.get("label", wallet)
        if not isinstance(label, str) or not label.strip() or len(label) > 100:
            raise ValueError("invalid wallet policy label")
        limits = row["budget_limits"]
        buy_rules = row["buy_rules"]
        if (not isinstance(limits, dict) or not limits or not set(limits) <= BUDGET_BUCKETS
                or not isinstance(buy_rules, dict) or set(buy_rules) != set(limits)):
            raise ValueError("budget limits and buy rules must use the same supported buckets")
        normalized_limits = {}
        for bucket, raw in limits.items():
            if not isinstance(raw, str) or not raw.isdecimal() or int(raw) <= 0:
                raise ValueError("budget limits must be positive decimal integer strings")
            normalized_limits[bucket] = raw
        follower = row.get("follower_wallet")
        if follower is not None:
            follower = address(follower)
            if follower == ZERO_ADDRESS:
                raise ValueError("zero follower wallet is forbidden")
        relationship_id = row.get("relationship_id")
        if relationship_id is not None and (
                not isinstance(relationship_id, str) or not relationship_id
                or len(relationship_id) > 100):
            raise ValueError("invalid relationship id")
        if (follower is None) != (relationship_id is None):
            raise ValueError("follower wallet and relationship id must be configured together")
        run_mode = row.get("run_mode", "paper")
        if run_mode not in {"paper", "mainnet_live"}:
            raise ValueError("invalid relationship run mode")
        chain_id = row.get("chain_id", R.CHAIN_ID)
        if type(chain_id) is not int:
            raise ValueError("invalid relationship chain id")
        chain = R.chain_for(chain_id)   # refuses a chain this build does not know
        # A bucket only exists where the chain has the asset behind it, so a
        # configuration naming another chain's bucket is a configuration error.
        settlement_bucket = "USDG" if chain.usdg is not None else "USDC"
        allowed_buckets = {settlement_bucket}
        if chain.weth is not None:
            allowed_buckets.add("ETH_WETH")
        if not set(normalized_limits) <= allowed_buckets:
            raise ValueError(
                f"chain {chain_id} supports buckets {sorted(allowed_buckets)}")
        if run_mode == "mainnet_live" and (
                follower is None or relationship_id is None
                or trigger not in {"swap_evidenced", "evidenced"}):
            raise ValueError(
                "mainnet_live requires a relationship identity and evidenced trigger")
        policy = WalletPaperPolicy(
            wallet, label.strip(), follower, relationship_id, run_mode, normalized_limits,
            {bucket: _amount_rule(rule) for bucket, rule in buy_rules.items()},
            _amount_rule(row["sell_rule"]), strategy, trigger, tuple(shadows),
            quote_policy, frozenset(protocols), frozenset(normalized_assets),
            frozenset(route_keys), tuple(deepcopy(route_rows)), "",
            execution_providers,
            chain_id,
        )
        relationship_key = (follower, relationship_id, wallet)
        if relationship_key in relationship_keys:
            raise ValueError("duplicate wallet relationship policy")
        if wallet in wallets and (follower is None or relationship_id is None):
            raise ValueError("duplicate wallet policy requires relationship identity")
        relationship_keys.add(relationship_key)
        relationships.append(policy)
        wallets.setdefault(wallet, policy)
    snapshot_hash = hashlib.sha256(json.dumps(
        document, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    relationships = [WalletPaperPolicy(
        policy.wallet, policy.label, policy.follower_wallet, policy.relationship_id,
        policy.run_mode, policy.budget_limits, policy.buy_rules, policy.sell_rule,
        policy.strategy_version, policy.trigger_mode, policy.shadow_trigger_modes,
        policy.quote_policy, policy.allowed_protocols, policy.allowed_assets,
        policy.allowed_routes, policy.route_definitions, snapshot_hash,
        policy.execution_providers, policy.chain_id,
    ) for policy in relationships]
    wallets = {}
    for policy in relationships:
        wallets.setdefault(policy.wallet, policy)
    return PaperConfig(strategy, trigger, tuple(shadows), quote_policy,
                       frozenset(protocols), frozenset(normalized_assets),
                       frozenset(route_keys), tuple(deepcopy(route_rows)), wallets,
                       tuple(relationships), snapshot_hash)
