"""Read-only checks for unsigned execution plans. No signing or broadcast surface."""
from __future__ import annotations

from dataclasses import dataclass
import time

from eth_utils import keccak
from eth_abi import encode

from eth_abi import decode
from eth_abi.exceptions import DecodingError

from .kyber import KyberSwapTransaction, decode_kyber_swap
from .models import address, number
from .paper import AGGREGATOR_PROVIDERS, AGGREGATOR_ROUTERS, scope_reason
from .quotes import Quote
from .registry import (
    CHAIN_ID, KNOWN_V4_HOOK_CODE_HASHES, NATIVE, UNIVERSAL_ROUTER,
    V2_ROUTER, V3_ROUTER, WETH,
)
from .rpc import RpcError
from .simulation_diagnostics import AggregatorSimulationError, simulation_failure

POOL_KEY = "(address,address,uint24,int24,address)"
LOCAL_EXECUTION_TARGETS = frozenset({V2_ROUTER, V3_ROUTER, UNIVERSAL_ROUTER})
AGGREGATOR_EXECUTION_TARGETS = frozenset(AGGREGATOR_ROUTERS.values())
EXECUTION_TARGETS = LOCAL_EXECUTION_TARGETS | AGGREGATOR_EXECUTION_TARGETS


def _uint(value: str, name: str) -> int:
    if not isinstance(value, str) or not value.isdecimal() or int(value) < 0:
        raise ValueError(f"invalid {name}")
    return int(value)


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _erc20(asset: str) -> str:
    asset = address(asset)
    return WETH if asset == NATIVE else asset


def build_execution_plan(signal, follower_wallet: str, relationship_id: str,
                         proposal_id: str, quote: Quote,
                         minimum_amount_out_raw: str, deadline: int,
                         gas_limit: int, max_fee_per_gas: str,
                         max_priority_fee_per_gas: str,
                         allowed_protocols: frozenset[str],
                         allowed_assets: frozenset[str],
                         allowed_routes: frozenset[str]) -> "UnsignedExecutionPlan":
    """Build evidenced exact-input calldata for tightly bounded V2/V3/V4 paths."""
    follower = address(follower_wallet)
    if (signal.stage not in {"swap_evidenced", "relay_buy_evidenced",
                             "relay_sell_evidenced"}
            or signal.execution_status != "success"
            or signal.canonical_status == "orphaned"
            or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}
            or signal.exact_in is not True or signal.protocol not in {"v2", "v3", "v4"}):
        raise ValueError("signal is not eligible for execution construction")
    reason = scope_reason(signal, allowed_protocols, allowed_assets, allowed_routes)
    if reason:
        raise ValueError(reason)
    amount = _uint(quote.amount_in_raw, "quote amount in")
    minimum = _uint(minimum_amount_out_raw, "minimum amount out")
    if (quote.protocol != signal.protocol or quote.input_asset != signal.token_in
            or quote.output_asset != signal.token_out or amount <= 0
            or minimum <= 0 or minimum > _uint(quote.amount_out_raw, "quote amount out")):
        raise ValueError("quote does not match execution signal")
    value = 0
    if signal.protocol == "v2":
        route = signal.evidence.get("route")
        if not isinstance(route, list) or not 2 <= len(route) <= 8:
            raise ValueError("bounded V2 execution route required")
        route = [_erc20(item) for item in route]
        if route[0] != _erc20(signal.token_in) or route[-1] != _erc20(signal.token_out):
            raise ValueError("V2 execution route mismatch")
        if signal.token_in == NATIVE:
            signature = "swapExactETHForTokens(uint256,address[],address,uint256)"
            values = [minimum, route, follower, deadline]
            types = ["uint256", "address[]", "address", "uint256"]
            value = amount
        elif signal.token_out == NATIVE:
            signature = "swapExactTokensForETH(uint256,uint256,address[],address,uint256)"
            values = [amount, minimum, route, follower, deadline]
            types = ["uint256", "uint256", "address[]", "address", "uint256"]
        else:
            signature = "swapExactTokensForTokens(uint256,uint256,address[],address,uint256)"
            values = [amount, minimum, route, follower, deadline]
            types = ["uint256", "uint256", "address[]", "address", "uint256"]
        target = V2_ROUTER
        data = _selector(signature) + encode(types, values)
    elif signal.protocol == "v3":
        if NATIVE in {signal.token_in, signal.token_out}:
            raise ValueError("V3 native execution requires separately verified wrap semantics")
        hops = signal.evidence.get("hops")
        if not isinstance(hops, list) or not hops:
            fee = signal.evidence.get("fee")
            if fee is None:
                raise ValueError("bounded V3 execution hops required")
            hops = [{"token_in": signal.token_in, "token_out": signal.token_out, "fee": fee}]
        expected = signal.token_in
        path = bytearray(bytes.fromhex(expected[2:]))
        for hop in hops:
            token_in, token_out = address(hop.get("token_in")), address(hop.get("token_out"))
            fee = hop.get("fee")
            if token_in != expected or not isinstance(fee, int) or not 0 <= fee < 2 ** 24:
                raise ValueError("V3 execution path mismatch")
            path.extend(fee.to_bytes(3, "big"))
            path.extend(bytes.fromhex(token_out[2:]))
            expected = token_out
        if expected != signal.token_out:
            raise ValueError("V3 execution output mismatch")
        if len(hops) == 1:
            signature = "exactInputSingle((address,address,uint24,address,uint256,uint256,uint160))"
            types = ["(address,address,uint24,address,uint256,uint256,uint160)"]
            values = [(signal.token_in, signal.token_out, hops[0]["fee"], follower,
                       amount, minimum, 0)]
        else:
            signature = "exactInput((bytes,address,uint256,uint256))"
            types = ["(bytes,address,uint256,uint256)"]
            values = [(bytes(path), follower, amount, minimum)]
        target = V3_ROUTER
        data = _selector(signature) + encode(types, values)
    else:
        if signal.token_in != NATIVE:
            raise ValueError("V4 token input requires separately verified Permit2 semantics")
        if signal.evidence.get("v4_hops"):
            raise ValueError("V4 multihop execution is not enabled")
        key = signal.evidence.get("pool_key")
        hook_data = signal.evidence.get("hook_data", "0x")
        if (not isinstance(key, list) or len(key) != 5
                or not isinstance(hook_data, str) or not hook_data.startswith("0x")):
            raise ValueError("evidenced V4 PoolKey and hookData are required")
        pool_key = (address(key[0]), address(key[1]), int(key[2]),
                    int(key[3]), address(key[4]))
        if ({signal.token_in, signal.token_out} != {pool_key[0], pool_key[1]}
                or pool_key[4] != NATIVE
                and pool_key[4] not in KNOWN_V4_HOOK_CODE_HASHES):
            raise ValueError("V4 pool or hook is not approved for execution")
        try:
            hook_bytes = bytes.fromhex(hook_data[2:])
        except ValueError:
            raise ValueError("invalid V4 hookData") from None
        swap_param = encode(
            [f"({POOL_KEY},bool,uint128,uint128,uint256,bytes)"],
            [(pool_key, signal.token_in == pool_key[0], amount, minimum, 0, hook_bytes)],
        )
        settle = encode(["address", "uint256"], [signal.token_in, amount])
        take = encode(["address", "uint256"], [signal.token_out, minimum])
        action_payload = encode(
            ["bytes", "bytes[]"],
            [bytes([0x06, 0x0C, 0x0F]), [swap_param, settle, take]],
        )
        data = _selector("execute(bytes,bytes[],uint256)") + encode(
            ["bytes", "bytes[]", "uint256"], [bytes([0x10]), [action_payload], deadline])
        target = UNIVERSAL_ROUTER
        value = amount
    return UnsignedExecutionPlan(
        follower_wallet=follower, relationship_id=relationship_id,
        proposal_id=proposal_id, to=target, data="0x" + data.hex(),
        value_raw=str(value), input_asset=signal.token_in,
        amount_in_raw=str(amount), minimum_amount_out_raw=str(minimum),
        gas_limit=gas_limit, max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        quote_observed_at=quote.observed_at, quote_block_number=quote.block_number,
        quote_block_hash=quote.block_hash, deadline=deadline,
    )


def build_aggregator_execution_plan(
        signal, follower_wallet: str, relationship_id: str, proposal_id: str,
        quote: Quote, minimum_amount_out_raw: str, swap: KyberSwapTransaction,
        gas_limit: int, max_fee_per_gas: str, max_priority_fee_per_gas: str,
        allowed_protocols: frozenset[str], allowed_assets: frozenset[str],
        allowed_routes: frozenset[str]) -> "UnsignedExecutionPlan":
    """Wrap an aggregator-built swap after verifying every field we can decode.

    The inner executor payload is opaque, so this only admits calldata whose
    router is allowlisted, whose top-level description names the follower as the
    sole recipient for the exact planned input, and whose on-chain minimum output
    is at least the locally computed slippage floor. The pipeline additionally
    simulates the call before signing and before broadcast.
    """
    follower = address(follower_wallet)
    if (signal.stage not in {"swap_evidenced", "relay_buy_evidenced",
                             "relay_sell_evidenced"}
            or signal.execution_status != "success"
            or signal.canonical_status == "orphaned"
            or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}
            or signal.exact_in is not True or signal.protocol not in AGGREGATOR_PROVIDERS):
        raise ValueError("signal is not eligible for aggregator execution construction")
    reason = scope_reason(signal, allowed_protocols, allowed_assets, allowed_routes)
    if reason:
        raise ValueError(reason)
    return _validated_aggregator_plan(signal, follower, relationship_id, proposal_id,
        quote, minimum_amount_out_raw, swap, gas_limit, max_fee_per_gas, max_priority_fee_per_gas)


def build_early_aggregator_execution_plan(
        intent, follower_wallet, relationship_id, proposal_id, quote, minimum_amount_out_raw,
        swap, gas_limit, max_fee_per_gas, max_priority_fee_per_gas,
        allowed_protocols, allowed_assets, now=None):
    """Separate typed-intent entrance; never promote intent into execution evidence."""
    from .verified_feed_intent import VerifiedFeedIntent
    if not isinstance(intent, VerifiedFeedIntent):
        raise ValueError("verified Feed intent required for early construction")
    signal = intent.quote_signal(time.time() if now is None else now)
    source_protocol = "relay_solver" if signal.behavior == "BUY" else "kyber"
    funding = signal.token_in if signal.behavior == "BUY" else signal.token_out
    if source_protocol not in allowed_protocols or funding not in allowed_assets:
        raise ValueError("early source or funding asset outside policy")
    source_minimum = ((int(signal.amount_limit_raw) * int(quote.amount_in_raw)
                       + int(signal.amount_in_raw) - 1) // int(signal.amount_in_raw))
    # Unlike ordinary quote rounding tolerance, never shave one raw unit off the
    # signed source intent's price limit.
    if int(swap.minimum_amount_out_raw) < source_minimum:
        raise ValueError("aggregator minimum violates verified source intent")
    return _validated_aggregator_plan(signal, address(follower_wallet), relationship_id, proposal_id,
        quote, minimum_amount_out_raw, swap, gas_limit, max_fee_per_gas, max_priority_fee_per_gas)


def _validated_aggregator_plan(signal, follower, relationship_id, proposal_id, quote,
        minimum_amount_out_raw, swap, gas_limit, max_fee_per_gas, max_priority_fee_per_gas):
    """Common exact-calldata and router validation, independent of evidence stage."""
    if not isinstance(swap, KyberSwapTransaction):
        raise ValueError("aggregator transaction is not a verified Kyber swap")
    amount = _uint(quote.amount_in_raw, "quote amount in")
    floor = _uint(minimum_amount_out_raw, "minimum amount out")
    if (quote.protocol != signal.protocol or quote.input_asset != signal.token_in
            or quote.output_asset != signal.token_out or amount <= 0 or floor <= 0
            or floor > _uint(quote.amount_out_raw, "quote amount out")):
        raise ValueError("quote does not match execution signal")
    target = address(swap.to)
    if (target != AGGREGATOR_ROUTERS[signal.protocol]
            or target not in AGGREGATOR_EXECUTION_TARGETS):
        raise ValueError("aggregator router is not allowlisted")
    if signal.token_in == NATIVE or signal.token_out == NATIVE:
        raise ValueError("aggregator execution supports ERC-20 pairs only")
    decoded = decode_kyber_swap(swap.data)
    on_chain_minimum = _uint(swap.minimum_amount_out_raw, "aggregator minimum out")
    if (swap.input_asset != signal.token_in or swap.output_asset != signal.token_out
            or swap.recipient != follower or _uint(swap.amount_in_raw, "swap amount") != amount
            or _uint(swap.value_raw, "swap value") != 0
            or decoded["src_token"] != signal.token_in
            or decoded["dst_token"] != signal.token_out
            or decoded["dst_receiver"] != follower
            or _uint(decoded["amount_raw"], "decoded amount") != amount
            or _uint(decoded["minimum_amount_out_raw"], "decoded minimum") != on_chain_minimum):
        raise ValueError("aggregator calldata does not match the execution plan")
    # The aggregator derives its minimum from its own output figure, which can
    # round one raw unit below the route quote we assessed. Allow exactly that
    # unit; any larger gap means the price moved and the plan must be rebuilt.
    if on_chain_minimum + 1 < floor:
        raise ValueError("aggregator minimum output is below the plan slippage floor")
    if on_chain_minimum > _uint(swap.amount_out_raw, "aggregator amount out"):
        raise ValueError("aggregator minimum output exceeds its own quote")
    return UnsignedExecutionPlan(
        follower_wallet=follower, relationship_id=relationship_id,
        proposal_id=proposal_id, to=target, data=swap.data.lower(),
        value_raw="0", input_asset=signal.token_in,
        amount_in_raw=str(amount), minimum_amount_out_raw=str(on_chain_minimum),
        gas_limit=gas_limit, max_fee_per_gas=max_fee_per_gas,
        max_priority_fee_per_gas=max_priority_fee_per_gas,
        quote_observed_at=quote.observed_at, quote_block_number=quote.block_number,
        quote_block_hash=quote.block_hash, deadline=swap.deadline,
        execution_provider=signal.protocol,
    )


async def simulate_aggregator_execution(rpc, plan: "UnsignedExecutionPlan") -> dict:
    """Simulate the exact aggregator call from the follower; fail closed on any doubt."""
    if plan.execution_provider not in AGGREGATOR_PROVIDERS:
        raise ValueError("simulation is only defined for aggregator execution plans")
    if address(plan.to) not in AGGREGATOR_EXECUTION_TARGETS:
        raise ValueError("aggregator router is not allowlisted")
    call = {"from": plan.follower_wallet, "to": plan.to, "data": plan.data,
            "value": hex(_uint(plan.value_raw, "value")), "gas": hex(plan.gas_limit)}
    started_at, started_clock = time.time(), time.monotonic()

    def failure(message, kind, rpc_error=None, result_evidence=None):
        return AggregatorSimulationError(message, simulation_failure(
            plan, call, started_at, time.time(), (time.monotonic() - started_clock) * 1000,
            kind, rpc_error, result_evidence))

    try:
        raw = await rpc.call("eth_call", [call, "pending"])
    except RpcError as exc:
        detail = exc.diagnostic or {"kind": "rpc_failure", "code": None}
        # Never interpolate arbitrary provider/transport text into durable logs.
        code = detail.get("code")
        suffix = f"RPC eth_call error code {code}" if type(code) is int else "RPC eth_call failed"
        raise failure(f"aggregator execution simulation reverted: {suffix}",
                      "rpc_failure", detail) from None
    if not isinstance(raw, str) or not raw.startswith("0x") or len(raw) < 130:
        raise failure("aggregator execution simulation returned no output", "missing_output")
    try:
        return_amount, gas_used = decode(["uint256", "uint256"], bytes.fromhex(raw[2:]))
    except (ValueError, DecodingError):
        raise failure("aggregator execution simulation result is undecodable", "undecodable_output") from None
    minimum = _uint(plan.minimum_amount_out_raw, "minimum amount out")
    if int(return_amount) < minimum:
        raise failure("aggregator execution simulation output is below the minimum", "below_minimum",
                      result_evidence={"return_amount_raw": str(int(return_amount)),
                                       "gas_used_raw": str(int(gas_used))})
    return {
        "simulated": True, "simulated_return_amount_raw": str(int(return_amount)),
        "simulated_gas_used": str(int(gas_used)), "simulation_block": "pending",
    }


@dataclass(frozen=True)
class UnsignedExecutionPlan:
    follower_wallet: str
    relationship_id: str
    proposal_id: str
    to: str
    data: str
    value_raw: str
    input_asset: str
    amount_in_raw: str
    minimum_amount_out_raw: str
    gas_limit: int
    max_fee_per_gas: str
    max_priority_fee_per_gas: str
    quote_observed_at: float
    quote_block_number: int
    quote_block_hash: str
    deadline: int
    chain_id: int = CHAIN_ID
    execution_provider: str = "local"

    def validate(self, allowed_targets: frozenset[str], now: float | None = None,
                 max_quote_age_seconds: float = 2.0) -> None:
        now = time.time() if now is None else now
        follower, target = address(self.follower_wallet), address(self.to)
        address(self.input_asset)
        if target not in allowed_targets or self.chain_id != CHAIN_ID:
            raise ValueError("execution target or chain is not allowed")
        if self.execution_provider != "local" and (
                self.execution_provider not in AGGREGATOR_PROVIDERS
                or target != AGGREGATOR_ROUTERS[self.execution_provider]):
            raise ValueError("execution provider does not match the plan target")
        if self.execution_provider == "local" and target in AGGREGATOR_EXECUTION_TARGETS:
            raise ValueError("local execution plan targets an aggregator router")
        if (not self.relationship_id or not self.proposal_id
                or not isinstance(self.gas_limit, int) or self.gas_limit <= 0
                or not isinstance(self.quote_block_number, int) or self.quote_block_number < 0
                or not isinstance(self.deadline, int) or self.deadline <= int(now)):
            raise ValueError("invalid unsigned execution plan")
        if follower == "0x0000000000000000000000000000000000000000":
            raise ValueError("zero follower wallet is forbidden")
        for value, name in ((self.value_raw, "value"), (self.amount_in_raw, "amount in"),
                            (self.minimum_amount_out_raw, "minimum amount out"),
                            (self.max_fee_per_gas, "max fee per gas"),
                            (self.max_priority_fee_per_gas, "priority fee")):
            _uint(value, name)
        if _uint(self.amount_in_raw, "amount in") <= 0:
            raise ValueError("amount in must be positive")
        if _uint(self.max_priority_fee_per_gas, "priority fee") > _uint(
                self.max_fee_per_gas, "max fee per gas"):
            raise ValueError("priority fee exceeds max fee")
        if (not isinstance(self.quote_observed_at, (int, float))
                or now - self.quote_observed_at < 0
                or now - self.quote_observed_at > max_quote_age_seconds):
            raise ValueError("execution quote is missing or expired")
        if (not isinstance(self.quote_block_hash, str)
                or len(self.quote_block_hash) != 66
                or not self.quote_block_hash.startswith("0x")):
            raise ValueError("invalid quote block hash")
        if not isinstance(self.data, str) or not self.data.startswith("0x"):
            raise ValueError("invalid execution calldata")
        try:
            bytes.fromhex(self.data[2:])
            bytes.fromhex(self.quote_block_hash[2:])
        except ValueError:
            raise ValueError("invalid execution hex data") from None
        if self.input_asset == NATIVE and _uint(self.value_raw, "value") != _uint(
                self.amount_in_raw, "amount in"):
            raise ValueError("native transaction value does not match input")
        if self.input_asset != NATIVE and _uint(self.value_raw, "value") != 0:
            raise ValueError("token input transaction cannot carry native value")


class ReadOnlyExecutionPreflight:
    def __init__(self, rpc, allowed_targets: frozenset[str], max_gas_cost_wei: str,
                 *, max_quote_age_seconds: float = 2.0):
        if (type(max_quote_age_seconds) not in (int, float)
                or not 0 < max_quote_age_seconds <= 60):
            raise ValueError("invalid execution quote age limit")
        self.rpc = rpc
        self.allowed_targets = allowed_targets
        self.max_gas_cost = _uint(max_gas_cost_wei, "gas cost limit")
        self.max_quote_age_seconds = max_quote_age_seconds

    async def check(self, plan: UnsignedExecutionPlan, now: float | None = None) -> dict:
        plan.validate(self.allowed_targets, now, self.max_quote_age_seconds)
        pending_nonce = number(await self.rpc.call(
            "eth_getTransactionCount", [plan.follower_wallet, "pending"]))
        native_balance = number(await self.rpc.call(
            "eth_getBalance", [plan.follower_wallet, "pending"]))
        network_gas_price = number(await self.rpc.call("eth_gasPrice"))
        max_fee = _uint(plan.max_fee_per_gas, "max fee per gas")
        if max_fee < network_gas_price:
            raise ValueError("max fee is below current gas price")
        gas_cost = plan.gas_limit * max_fee
        if gas_cost > self.max_gas_cost:
            raise ValueError("execution gas cost limit exceeded")
        required_native = gas_cost + _uint(plan.value_raw, "value")
        if native_balance < required_native:
            raise ValueError("insufficient native balance for value and gas")
        token_balance = None
        token_allowance = None
        if plan.input_asset != NATIVE:
            balance_selector = keccak(text="balanceOf(address)")[:4]
            calldata = "0x" + (balance_selector + bytes(12) + bytes.fromhex(
                plan.follower_wallet[2:])).hex()
            token_balance = number(await self.rpc.call(
                "eth_call", [{"to": plan.input_asset, "data": calldata}, "pending"]))
            if token_balance < _uint(plan.amount_in_raw, "amount in"):
                raise ValueError("insufficient token balance")
            allowance_selector = keccak(text="allowance(address,address)")[:4]
            allowance_data = "0x" + (
                allowance_selector + bytes(12) + bytes.fromhex(plan.follower_wallet[2:])
                + bytes(12) + bytes.fromhex(plan.to[2:])
            ).hex()
            token_allowance = number(await self.rpc.call(
                "eth_call", [{"to": plan.input_asset, "data": allowance_data}, "pending"]))
            if token_allowance < _uint(plan.amount_in_raw, "amount in"):
                raise ValueError("insufficient token allowance")
        return {
            "pending_nonce": pending_nonce,
            "native_balance_raw": str(native_balance),
            "token_balance_raw": str(token_balance) if token_balance is not None else None,
            "token_allowance_raw": str(token_allowance) if token_allowance is not None else None,
            "network_gas_price_wei": str(network_gas_price),
            "maximum_gas_cost_wei": str(gas_cost),
            "quote_max_age_seconds": self.max_quote_age_seconds,
            "checked_at": time.time() if now is None else now,
            "read_only": True,
        }
