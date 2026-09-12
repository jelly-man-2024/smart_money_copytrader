"""Read-only checks for unsigned execution plans. No signing or broadcast surface."""
from __future__ import annotations

from dataclasses import dataclass
import time

from eth_utils import keccak
from eth_abi import encode

from .models import address, number
from .paper import scope_reason
from .quotes import Quote
from .registry import (
    CHAIN_ID, KNOWN_V4_HOOK_CODE_HASHES, NATIVE, UNIVERSAL_ROUTER,
    V2_ROUTER, V3_ROUTER, WETH,
)

POOL_KEY = "(address,address,uint24,int24,address)"


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
    if (signal.stage != "swap_evidenced" or signal.execution_status != "success"
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

    def validate(self, allowed_targets: frozenset[str], now: float | None = None,
                 max_quote_age_seconds: float = 2.0) -> None:
        now = time.time() if now is None else now
        follower, target = address(self.follower_wallet), address(self.to)
        address(self.input_asset)
        if target not in allowed_targets or self.chain_id != CHAIN_ID:
            raise ValueError("execution target or chain is not allowed")
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
    def __init__(self, rpc, allowed_targets: frozenset[str], max_gas_cost_wei: str):
        self.rpc = rpc
        self.allowed_targets = allowed_targets
        self.max_gas_cost = _uint(max_gas_cost_wei, "gas cost limit")

    async def check(self, plan: UnsignedExecutionPlan, now: float | None = None) -> dict:
        plan.validate(self.allowed_targets, now)
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
            "checked_at": time.time() if now is None else now,
            "read_only": True,
        }
