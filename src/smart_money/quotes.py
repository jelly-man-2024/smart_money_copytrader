"""Block-pinned, read-only Uniswap quotes for paper trading."""
from __future__ import annotations

from copy import deepcopy
from collections import OrderedDict
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict, dataclass, replace
import hashlib
import json
import time

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_utils import keccak

from . import registry as R
from .models import Signal, address, number
from .rpc import RpcError

POOL_KEY = "(address,address,uint24,int24,address)"
V4_PATH_KEY = "(address,uint24,int24,address,bytes)"


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _erc20(asset: str) -> str:
    asset = address(asset)
    return R.WETH if asset == R.NATIVE else asset


@dataclass(frozen=True)
class Quote:
    protocol: str
    source: str
    block_number: int
    block_hash: str
    observed_at: float
    input_asset: str
    output_asset: str
    amount_in_raw: str
    amount_out_raw: str
    gas_estimate_raw: str | None = None

    def to_dict(self) -> dict:
        result = asdict(self)
        result["amount_in_raw"] = str(self.amount_in_raw)
        result["amount_out_raw"] = str(self.amount_out_raw)
        if result["gas_estimate_raw"] is not None:
            result["gas_estimate_raw"] = str(result["gas_estimate_raw"])
        return result


@dataclass(frozen=True)
class QuotePolicy:
    max_age_seconds: float = 2.0
    max_adverse_deviation_bps: int = 300
    max_price_impact_bps: int = 500
    max_slippage_bps: int = 300
    max_gas_cost_wei: str = "10000000000000000"
    min_amount_out_raw: str = "1"

    def __post_init__(self):
        if not 0 < self.max_age_seconds <= 60:
            raise ValueError("invalid quote max age")
        if not 0 <= self.max_adverse_deviation_bps <= 10_000:
            raise ValueError("invalid deviation bps")
        if not 0 <= self.max_price_impact_bps <= 10_000:
            raise ValueError("invalid impact bps")
        if not 0 <= self.max_slippage_bps < 10_000:
            raise ValueError("invalid slippage bps")
        if not self.max_gas_cost_wei.isdecimal() or int(self.max_gas_cost_wei) <= 0:
            raise ValueError("invalid maximum gas cost")
        if not self.min_amount_out_raw.isdecimal() or int(self.min_amount_out_raw) <= 0:
            raise ValueError("invalid minimum output")


def validate_quote(signal: Signal, quote: Quote, policy: QuotePolicy,
                   now: float | None = None) -> tuple[bool, str | None, dict]:
    now = time.time() if now is None else now
    evidence = {"quote_age_ms": str(max(0, int((now - quote.observed_at) * 1000)))}
    if now < quote.observed_at or now - quote.observed_at > policy.max_age_seconds:
        return False, "quote_missing_or_expired", evidence
    if quote.input_asset != signal.token_in or quote.output_asset != signal.token_out:
        return False, "quote_asset_mismatch", evidence
    if int(quote.amount_out_raw) < int(policy.min_amount_out_raw):
        return False, "quote_has_insufficient_output", evidence
    target_in = signal.evidence.get("actual_input_debit_raw")
    target_out = signal.evidence.get("actual_output_credit_raw")
    if not (isinstance(target_in, str) and target_in.isdecimal() and int(target_in) > 0
            and isinstance(target_out, str) and target_out.isdecimal() and int(target_out) > 0):
        if (signal.stage != "intent" or signal.exact_in is not True
                or not isinstance(signal.amount_in_raw, str)
                or not signal.amount_in_raw.isdecimal() or int(signal.amount_in_raw) <= 0
                or not isinstance(signal.amount_limit_raw, str)
                or not signal.amount_limit_raw.isdecimal() or int(signal.amount_limit_raw) <= 0):
            return False, "source_execution_price_missing", evidence
        scaled_minimum = ((int(signal.amount_limit_raw) * int(quote.amount_in_raw)
                           + int(signal.amount_in_raw) - 1) // int(signal.amount_in_raw))
        evidence["source_price_basis"] = "intent_exact_in_minimum"
        evidence["scaled_source_minimum_out_raw"] = str(scaled_minimum)
        if int(quote.amount_out_raw) < scaled_minimum:
            return False, "intent_price_limit_not_met", evidence
        return True, None, evidence
    denominator = int(target_out) * int(quote.amount_in_raw)
    difference = denominator - int(quote.amount_out_raw) * int(target_in)
    adverse_bps = max(0, difference * 10_000 // denominator)
    evidence["adverse_price_deviation_bps"] = str(adverse_bps)
    if adverse_bps > policy.max_adverse_deviation_bps:
        return False, "adverse_price_deviation_exceeded", evidence
    return True, None, evidence


def assess_quote(signal: Signal, quote: Quote, reference: Quote, policy: QuotePolicy,
                 gas_price_wei: str, now: float | None = None) -> tuple[bool, str | None, dict]:
    allowed, reason, evidence = validate_quote(signal, quote, policy, now)
    if not allowed:
        return allowed, reason, evidence
    return assess_market_quote(quote, reference, policy, gas_price_wei, now, evidence)


def assess_market_quote(quote: Quote, reference: Quote, policy: QuotePolicy,
                        gas_price_wei: str, now: float | None = None,
                        evidence: dict | None = None) -> tuple[bool, str | None, dict]:
    """Assess an executable quote without requiring a source-wallet execution price."""
    now = time.time() if now is None else now
    evidence = dict(evidence or {})
    if now < reference.observed_at or now - reference.observed_at > policy.max_age_seconds:
        return False, "reference_quote_missing_or_expired", evidence
    evidence.setdefault("quote_age_ms", str(max(0, int((now - quote.observed_at) * 1000))))
    if now < quote.observed_at or now - quote.observed_at > policy.max_age_seconds:
        return False, "quote_missing_or_expired", evidence
    if int(quote.amount_out_raw) < int(policy.min_amount_out_raw):
        return False, "quote_has_insufficient_output", evidence
    if (reference.protocol != quote.protocol or reference.block_hash != quote.block_hash
            or reference.input_asset != quote.input_asset
            or reference.output_asset != quote.output_asset):
        return False, "reference_quote_mismatch", evidence
    ref_in, ref_out = int(reference.amount_in_raw), int(reference.amount_out_raw)
    full_in, full_out = int(quote.amount_in_raw), int(quote.amount_out_raw)
    if ref_in <= 0 or ref_out <= 0 or ref_in >= full_in:
        return False, "invalid_reference_quote", evidence
    denominator = ref_out * full_in
    impact = max(0, (denominator - full_out * ref_in) * 10_000 // denominator)
    evidence["estimated_price_impact_bps"] = str(impact)
    if impact > policy.max_price_impact_bps:
        return False, "price_impact_exceeded", evidence
    if not isinstance(gas_price_wei, str) or not gas_price_wei.isdecimal():
        return False, "gas_price_missing", evidence
    floor = {"v2": 200_000, "v3": 300_000, "v4": 400_000, "kyber": 350_000, "zeroex": 350_000}[quote.protocol]
    quoted_gas = int(quote.gas_estimate_raw or "0")
    gas_units = max(floor, quoted_gas + 100_000 if quoted_gas else 0)
    gas_cost = gas_units * int(gas_price_wei)
    evidence["gas_price_wei"] = gas_price_wei
    evidence["estimated_gas_units"] = str(gas_units)
    evidence["estimated_gas_cost_wei"] = str(gas_cost)
    if gas_cost > int(policy.max_gas_cost_wei):
        return False, "gas_cost_limit_exceeded", evidence
    min_out = full_out * (10_000 - policy.max_slippage_bps) // 10_000
    if min_out <= 0:
        return False, "slippage_minimum_rounds_to_zero", evidence
    evidence["minimum_amount_out_raw"] = str(min_out)
    evidence["max_slippage_bps"] = str(policy.max_slippage_bps)
    return True, None, evidence


AGGREGATOR_PROTOCOLS = frozenset({"kyber", "zeroex"})


class LiveQuoter:
    def __init__(self, rpc, aggregators: dict | None = None):
        self.rpc = rpc
        self.aggregators = dict(aggregators or {})
        self._context = ContextVar("execution_quote_context", default=None)
        self.shared_routes_enabled = False
        self._shared_routes = OrderedDict()
        self._route_flights = {}
        if any(name not in AGGREGATOR_PROTOCOLS for name in self.aggregators):
            raise ValueError("unsupported aggregator provider")

    @contextmanager
    def execution_context(self, operation: str, follower: str, snapshot: str,
                          max_age_seconds: float, slippage_bps: int = 300):
        """Per-operation, per-task cache; never persists or resets quote timestamps.

        All balance/allowance/nonce/config checks remain outside this cache.
        Changed inputs miss it; expiry discards the entire market bundle.
        """
        if not operation or not follower or not snapshot or not 0 < max_age_seconds <= 60:
            raise ValueError("invalid execution quote context")
        context = {"binding": (operation, follower, snapshot), "max_age": max_age_seconds,
                   "bundle": None, "routes": {}, "route_requests": 0,
                   "build_requests": 0, "quote_reuses": 0, "refreshes": 0}
        context.update(excluded_sources=(), route_retry=None, swaps={}, slippage_bps=slippage_bps)
        token = self._context.set(context)
        try:
            yield context
        finally:
            self._context.reset(token)

    @staticmethod
    def _quote_key(signal, amount):
        return hashlib.sha256(json.dumps(
            [signal.to_dict(), amount], sort_keys=True, separators=(",", ":")
        ).encode()).hexdigest()

    def _quotable_protocols(self) -> frozenset[str]:
        return frozenset({"v2", "v3", "v4"} | set(self.aggregators))

    def _aggregator(self, protocol: str):
        client = self.aggregators.get(protocol)
        if client is None:
            raise ValueError(f"aggregator provider is not configured: {protocol}")
        return client

    def begin_simulation_route_retry(self, signal, amount_in_raw, diagnostic):
        """One operation-local alternative, not a diagnosis or a token blacklist."""
        context = self._context.get()
        error = diagnostic.get("rpc_error") or {}
        if (signal.protocol != "kyber" or context is None or context["route_retry"] is not None
                or diagnostic.get("failure_kind") != "rpc_failure"
                or error.get("message_category") != "execution_reverted"):
            return None
        route = context["routes"].get(self._quote_key(signal, amount_in_raw))
        if route is None or "uniswap-v4" not in route.sources():
            return None
        evidence = {"status": "started", "excluded_sources": ["uniswap-v4"],
                    "reason": "simulation_revert_with_v4_route_not_proven_v4_cause",
                    "original_route_response_hash": route.response_hash,
                    "original_simulation_failure": diagnostic}
        context["route_retry"] = evidence
        context["excluded_sources"] = ("uniswap-v4",)
        context["bundle"] = None
        context["routes"].clear()
        context["refreshes"] += 1
        return evidence

    async def _request_route(self, signal, amount_in_raw):
        context = self._context.get()
        kwargs = {}
        if signal.protocol == "kyber" and context and context["excluded_sources"]:
            kwargs["excluded_sources"] = context["excluded_sources"]
        key = None
        if self.shared_routes_enabled and context and signal.protocol == "kyber":
            # Share market data, NEVER a decision/authorization or built transaction.
            key = (signal.chain_id, signal.tx_hash, signal.wallet,
                   context["binding"][1:], signal.protocol, signal.token_in,
                   signal.token_out, amount_in_raw, tuple(kwargs.get("excluded_sources", ())))
            route = self._shared_routes.get(key)
            if route is not None:
                if 0 <= time.time() - route.observed_at <= context["max_age"]:
                    context["shared_route_reuses"] = context.get("shared_route_reuses", 0) + 1
                    return deepcopy(route)
                del self._shared_routes[key]
            if key in self._route_flights:
                route = await asyncio.shield(self._route_flights[key])
                if 0 <= time.time() - route.observed_at <= context["max_age"]:
                    context["shared_route_reuses"] = context.get("shared_route_reuses", 0) + 1
                    return deepcopy(route)
            if len(self._route_flights) >= 64:
                key = None
        future = None
        if key is not None:
            future = asyncio.get_running_loop().create_future()
            future.add_done_callback(lambda f: None if f.cancelled() else f.exception())
            self._route_flights[key] = future
        try:
            if context:
                context["route_requests"] += 1
            route = await self._aggregator(signal.protocol).route(
                signal.token_in, signal.token_out, amount_in_raw, **kwargs)
            if key is not None:
                self._shared_routes[key] = deepcopy(route)
                self._shared_routes.move_to_end(key)
                while len(self._shared_routes) > 64:
                    self._shared_routes.popitem(last=False)
                future.set_result(deepcopy(route))
            return route
        except BaseException as exc:
            if future is not None and not future.done():
                future.set_exception(ValueError("shared route request cancelled")
                                     if isinstance(exc, asyncio.CancelledError) else exc)
            raise
        finally:
            if key is not None and self._route_flights.get(key) is future:
                del self._route_flights[key]

    async def build_aggregator_transaction(self, signal: Signal, amount_in_raw: str,
                                           follower_wallet: str, slippage_bps: int,
                                           deadline: int):
        """Ask the configured aggregator for the follower's own swap transaction."""
        if (signal.protocol not in AGGREGATOR_PROTOCOLS
                or signal.stage in {"needs_review", "failed"}
                or signal.canonical_status == "orphaned"
                or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}
                or not signal.token_in or not signal.token_out):
            raise ValueError("signal is not eligible for aggregator execution")
        if not amount_in_raw.isdecimal() or int(amount_in_raw) <= 0:
            raise ValueError("invalid aggregator input amount")
        client = self._aggregator(signal.protocol)
        context = self._context.get()
        key = self._quote_key(signal, amount_in_raw)
        if signal.protocol == "zeroex":
            swap = context["swaps"].get(key) if context else None
            if (swap is None or context["binding"][1] != follower_wallet
                    or context["slippage_bps"] != slippage_bps
                    or not 0 <= time.time()-swap.observed_at <= context["max_age"]
                    or swap.deadline <= time.time() or swap.deadline > deadline):
                raise ValueError("0x executable quote missing, expired or mismatched")
            return swap
        route = context["routes"].get(key) if context else None
        if context and follower_wallet != context["binding"][1]:
            raise ValueError("aggregator context follower mismatch")
        if route is not None and not 0 <= time.time() - route.observed_at <= context["max_age"]:
            raise ValueError("aggregator route expired before build")
        if route is None:
            route = await self._request_route(signal, amount_in_raw)
        if context:
            context["build_requests"] += 1
        swap = await client.build(route, follower_wallet, slippage_bps, deadline)
        if context and not 0 <= time.time() - route.observed_at <= context["max_age"]:
            raise ValueError("aggregator route expired during build")
        return swap

    async def quote_exact_input(self, signal: Signal, amount_in_raw: str) -> Quote:
        if (signal.stage in {"needs_review", "failed"}
                or signal.canonical_status == "orphaned"
                or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}
                or signal.protocol not in self._quotable_protocols()
                or not signal.token_in or not signal.token_out):
            raise ValueError("signal is not a supported quotable swap")
        if not amount_in_raw.isdecimal() or int(amount_in_raw) <= 0:
            raise ValueError("invalid quote input amount")
        header = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(header, dict) or not isinstance(header.get("hash"), str):
            raise ValueError("quote block header missing")
        return await self._quote_at(signal, amount_in_raw, header)

    async def quote_with_reference(self, signal: Signal, amount_in_raw: str,
                                   divisor: int = 100) -> tuple[Quote, Quote, str]:
        if not 2 <= divisor <= 1000:
            raise ValueError("invalid reference quote divisor")
        amount = int(amount_in_raw)
        reference_amount = max(1, amount // divisor)
        if reference_amount >= amount:
            raise ValueError("amount too small for a reference quote")
        context = self._context.get() if signal.protocol in AGGREGATOR_PROTOCOLS else None
        key = (self._quote_key(signal, amount_in_raw), divisor)
        if context and context["bundle"]:
            old_key, bundle = context["bundle"]
            now = time.time()
            if old_key == key and all(0 <= now - q.observed_at <= context["max_age"]
                                      for q in bundle[:2]):
                context["quote_reuses"] += 1
                return bundle
            context["bundle"] = None
            context["routes"].clear()
            context["swaps"].clear()
            context["refreshes"] += 1
        header = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(header, dict) or not isinstance(header.get("hash"), str):
            raise ValueError("quote block header missing")
        async def full_quote():
            if signal.protocol != "zeroex":
                return await self._quote_at(signal, amount_in_raw, header)
            if context is None:
                raise ValueError("0x executable quote requires a bound operation context")
            context["route_requests"] += 1
            swap = await self._aggregator("zeroex").quote(signal.token_in, signal.token_out,
                amount_in_raw, context["binding"][1], context["slippage_bps"], int(time.time())+120)
            context["swaps"][self._quote_key(signal, amount_in_raw)] = swap
            return Quote("zeroex", swap.to, number(header["number"]), header["hash"].lower(),
                swap.observed_at, signal.token_in, signal.token_out, amount_in_raw,
                swap.amount_out_raw, str(swap.gas_estimate))
        results = await asyncio.gather(
            full_quote(),
            self._quote_at(signal, str(reference_amount), header),
            self.rpc.call("eth_gasPrice"), return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                if context:
                    context["routes"].clear()
                raise result
        quote, reference, gas_price = results
        result = quote, reference, str(number(gas_price))
        if context:
            context["bundle"] = (key, result)
        return result

    async def discover_v3_route(
            self, signal: Signal, amount_in_raw: str,
            fee_tiers: tuple[int, ...] = (100, 500, 3000, 10000)) -> dict:
        """Find one bounded direct V3 route at one pinned canonical block.

        This is intentionally not a general-purpose path search. It checks only
        the four standard fee tiers on the configured V3 factory, verifies each
        returned pool's code and immutable pair/fee, and chooses the pool with
        the greatest quote for the caller's actual planned input amount.
        """
        if (signal.stage in {"needs_review", "failed"}
                or signal.canonical_status == "orphaned"
                or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}
                or not signal.token_in or not signal.token_out):
            raise ValueError("signal is not eligible for V3 route discovery")
        if not amount_in_raw.isdecimal() or int(amount_in_raw) <= 0:
            raise ValueError("invalid route discovery input amount")
        if fee_tiers != (100, 500, 3000, 10000):
            raise ValueError("only the bounded standard V3 fee tiers are supported")

        token_in, token_out = _erc20(signal.token_in), _erc20(signal.token_out)
        if token_in == token_out:
            raise ValueError("V3 route assets must differ")
        header = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if (not isinstance(header, dict) or not isinstance(header.get("hash"), str)
                or "number" not in header):
            raise ValueError("route discovery block header missing")
        block_number = number(header["number"])
        block_tag = hex(block_number)
        candidates: list[tuple[int, int, str]] = []
        for fee in fee_tiers:
            try:
                raw_pool = await self._call(
                    R.V3_FACTORY,
                    _selector("getPool(address,address,uint24)") + encode(
                        ["address", "address", "uint24"],
                        [token_in, token_out, fee]),
                    block_tag)
                pool = address(decode(["address"], raw_pool)[0])
                if pool == R.NATIVE:
                    continue
                code = await self.rpc.call("eth_getCode", [pool, block_tag])
                if not isinstance(code, str) or code.lower() in {"0x", "0x0", "0x00"}:
                    continue
                pool_token0 = address(decode(
                    ["address"], await self._call(
                        pool, _selector("token0()"), block_tag))[0])
                pool_token1 = address(decode(
                    ["address"], await self._call(
                        pool, _selector("token1()"), block_tag))[0])
                pool_fee = int(decode(
                    ["uint24"], await self._call(
                        pool, _selector("fee()"), block_tag))[0])
                if {pool_token0, pool_token1} != {token_in, token_out} or pool_fee != fee:
                    continue
                evidence = deepcopy(signal.evidence)
                evidence["hops"] = [{
                    "token_in": signal.token_in,
                    "token_out": signal.token_out,
                    "fee": fee,
                }]
                quote_signal = replace(
                    signal, protocol="v3", contract=R.V3_QUOTER,
                    exact_in=True, amount_out_raw=None, amount_limit_raw=None,
                    evidence=evidence)
                quote = await self._quote_at(quote_signal, amount_in_raw, header)
                candidates.append((int(quote.amount_out_raw), fee, pool))
            except (RpcError, DecodingError, ValueError, TypeError, IndexError):
                continue
        if not candidates:
            raise ValueError("no verified quotable direct V3 pool")
        amount_out, fee, pool = sorted(
            candidates, key=lambda item: (-item[0], item[1], item[2]))[0]
        return {
            "protocol": "v3",
            "assets": [signal.token_in, signal.token_out],
            "fees": [fee],
            "verified_pool": pool,
            "verified_block_number": str(block_number),
            "verified_block_hash": header["hash"].lower(),
            "route_discovery": "v3_factory_bounded_best_quote",
            "route_discovery_amount_in_raw": amount_in_raw,
            "route_discovery_amount_out_raw": str(amount_out),
            "evaluated_fee_tiers": list(fee_tiers),
        }

    async def _quote_at(self, signal: Signal, amount_in_raw: str, header: dict) -> Quote:
        if (signal.stage in {"needs_review", "failed"}
                or signal.canonical_status == "orphaned"
                or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}
                or signal.protocol not in self._quotable_protocols()
                or not signal.token_in or not signal.token_out):
            raise ValueError("signal is not a supported quotable swap")
        if not amount_in_raw.isdecimal() or int(amount_in_raw) <= 0:
            raise ValueError("invalid quote input amount")
        block_number = number(header["number"])
        block_tag = hex(block_number)
        if signal.protocol in AGGREGATOR_PROTOCOLS:
            # Header is sampled at consumption (a shared route may predate it).
            # Keep the API's ORIGINAL observed_at; this is not a pinned state read.
            context = self._context.get()
            route = await self._request_route(signal, amount_in_raw)
            if context:
                context["routes"][self._quote_key(signal, amount_in_raw)] = route
            return Quote(signal.protocol, route.router, block_number,
                         header["hash"].lower(), route.observed_at, signal.token_in,
                         signal.token_out, amount_in_raw, route.amount_out_raw,
                         str(route.gas_estimate))
        if signal.protocol == "v2":
            output, gas = await self._v2(signal, int(amount_in_raw), block_tag)
            source = R.V2_ROUTER
        elif signal.protocol == "v3":
            output, gas = await self._v3(signal, int(amount_in_raw), block_tag)
            source = R.V3_QUOTER
        else:
            output, gas = await self._v4(signal, int(amount_in_raw), block_tag)
            source = R.V4_QUOTER
        if output <= 0:
            raise ValueError("quoter returned no output")
        return Quote(signal.protocol, source, block_number, header["hash"].lower(), time.time(),
                     signal.token_in, signal.token_out, amount_in_raw, str(output),
                     str(gas) if gas is not None else None)

    async def _call(self, to: str, data: bytes, block_tag: str) -> bytes:
        raw = await self.rpc.call("eth_call", [{"to": to, "data": "0x" + data.hex()}, block_tag])
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise ValueError("invalid quote RPC result")
        result = bytes.fromhex(raw[2:])
        if len(result) > 1024 * 1024:
            raise ValueError("quote result too large")
        return result

    async def _v2(self, signal: Signal, amount: int, block_tag: str) -> tuple[int, None]:
        route = signal.evidence.get("route")
        if not isinstance(route, list) or len(route) < 2 or len(route) > 8:
            raise ValueError("bounded V2 route required")
        route = [_erc20(item) for item in route]
        if route[0] != _erc20(signal.token_in) or route[-1] != _erc20(signal.token_out):
            raise ValueError("V2 quote route mismatch")
        data = _selector("getAmountsOut(uint256,address[])") + encode(
            ["uint256", "address[]"], [amount, route])
        values = decode(["uint256[]"], await self._call(R.V2_ROUTER, data, block_tag))[0]
        if len(values) != len(route) or values[0] != amount:
            raise ValueError("invalid V2 quote path result")
        return int(values[-1]), None

    async def _v3(self, signal: Signal, amount: int, block_tag: str) -> tuple[int, int | None]:
        hops = signal.evidence.get("hops")
        if not isinstance(hops, list) or not 1 <= len(hops) <= 7:
            fee = signal.evidence.get("fee")
            if fee is None:
                raise ValueError("bounded V3 hops required")
            hops = [{"token_in": signal.token_in, "token_out": signal.token_out, "fee": fee}]
        path = bytearray()
        expected = _erc20(signal.token_in)
        path.extend(bytes.fromhex(expected[2:]))
        for hop in hops:
            token_in, token_out, fee = _erc20(hop["token_in"]), _erc20(hop["token_out"]), int(hop["fee"])
            if token_in != expected or not 0 <= fee < 2 ** 24:
                raise ValueError("V3 quote path mismatch")
            path.extend(fee.to_bytes(3, "big"))
            path.extend(bytes.fromhex(token_out[2:]))
            expected = token_out
        if expected != _erc20(signal.token_out):
            raise ValueError("V3 quote output mismatch")
        data = _selector("quoteExactInput(bytes,uint256)") + encode(
            ["bytes", "uint256"], [bytes(path), amount])
        raw = await self._call(R.V3_QUOTER, data, block_tag)
        if len(raw) < 32:
            raise ValueError("invalid V3 quote result")
        # Quoter v1 returns one word; Quoter v2's first return word is also amountOut.
        return int.from_bytes(raw[:32], "big"), None

    async def _v4(self, signal: Signal, amount: int, block_tag: str) -> tuple[int, int]:
        key = signal.evidence.get("pool_key")
        hops = signal.evidence.get("v4_hops")
        if hops:
            if not isinstance(hops, list) or not 2 <= len(hops) <= 7:
                raise ValueError("bounded evidenced V4 path required")
            expected = signal.token_in
            path = []
            for hop in hops:
                if not isinstance(hop, dict) or hop.get("token_in") != expected:
                    raise ValueError("V4 quote path discontinuity")
                token_out = address(hop.get("token_out"))
                pool_key = hop.get("pool_key")
                hook_data = hop.get("hook_data", "0x")
                if (not isinstance(pool_key, list) or len(pool_key) != 5
                        or not isinstance(hook_data, str) or not hook_data.startswith("0x")):
                    raise ValueError("invalid V4 quote hop")
                currency0, currency1 = address(pool_key[0]), address(pool_key[1])
                if {expected, token_out} != {currency0, currency1}:
                    raise ValueError("V4 quote hop pool mismatch")
                path.append((token_out, int(pool_key[2]), int(pool_key[3]),
                             address(pool_key[4]), bytes.fromhex(hook_data[2:])))
                expected = token_out
            if expected != signal.token_out:
                raise ValueError("V4 quote output mismatch")
            params_type = f"(address,{V4_PATH_KEY}[],uint128)"
            data = _selector(f"quoteExactInput({params_type})") + encode(
                [params_type], [(signal.token_in, path, amount)])
            raw = await self._call(R.V4_QUOTER, data, block_tag)
            output, gas = decode(["uint256", "uint256"], raw)
            return int(output), int(gas)
        if not isinstance(key, list) or len(key) != 5:
            raise ValueError("evidenced V4 pool key required")
        key = (address(key[0]), address(key[1]), int(key[2]), int(key[3]), address(key[4]))
        if {signal.token_in, signal.token_out} != {key[0], key[1]}:
            raise ValueError("V4 quote pool key mismatch")
        hook_data = signal.evidence.get("hook_data", "0x")
        if not isinstance(hook_data, str) or not hook_data.startswith("0x"):
            raise ValueError("invalid V4 hook data")
        params = (key, signal.token_in == key[0], amount, bytes.fromhex(hook_data[2:]))
        data = _selector(f"quoteExactInputSingle(({POOL_KEY},bool,uint128,bytes))") + encode(
            [f"({POOL_KEY},bool,uint128,bytes)"], [params])
        raw = await self._call(R.V4_QUOTER, data, block_tag)
        output, gas = decode(["uint256", "uint256"], raw)
        return int(output), int(gas)
