"""Block-pinned, read-only Uniswap quotes for paper trading."""
from __future__ import annotations

from dataclasses import asdict, dataclass
import time

from eth_abi import decode, encode
from eth_utils import keccak

from . import registry as R
from .models import Signal, address, number

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
    floor = {"v2": 200_000, "v3": 300_000, "v4": 400_000}[quote.protocol]
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


class LiveQuoter:
    def __init__(self, rpc):
        self.rpc = rpc

    async def quote_exact_input(self, signal: Signal, amount_in_raw: str) -> Quote:
        if (signal.stage in {"needs_review", "failed"}
                or signal.canonical_status == "orphaned"
                or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}
                or signal.protocol not in {"v2", "v3", "v4"}
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
        header = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
        if not isinstance(header, dict) or not isinstance(header.get("hash"), str):
            raise ValueError("quote block header missing")
        quote = await self._quote_at(signal, amount_in_raw, header)
        reference = await self._quote_at(signal, str(reference_amount), header)
        gas_price = await self.rpc.call("eth_gasPrice")
        return quote, reference, str(number(gas_price))

    async def _quote_at(self, signal: Signal, amount_in_raw: str, header: dict) -> Quote:
        if (signal.stage in {"needs_review", "failed"}
                or signal.canonical_status == "orphaned"
                or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}
                or signal.protocol not in {"v2", "v3", "v4"}
                or not signal.token_in or not signal.token_out):
            raise ValueError("signal is not a supported quotable swap")
        if not amount_in_raw.isdecimal() or int(amount_in_raw) <= 0:
            raise ValueError("invalid quote input amount")
        block_number = number(header["number"])
        block_tag = hex(block_number)
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
