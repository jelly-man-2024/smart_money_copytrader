"""Arc v4 pool safety gate: prove a pool can be SOLD OUT OF before we buy into it.

Sampling Arc mainnet v4 pools (2026-09-17) showed that ~94% carry a hook, that
the dominant hook is a per-token transaction-tax template (uniform 5175-byte
bytecode, flags 0x2044 = afterSwap + afterSwapReturnsDelta, tax rate stored as
two immutable bytes), and — critically — that on most sampled pools a BUY quotes
fine while every SELL quote reverts at every size, including dust, with
liquidity present. "Bought in but cannot sell out" is therefore the dominant
Arc meme risk, and the existing adverse-price gate cannot see it: that gate only
compares the buy side against the smart-money price.

This module simulates a full round trip (quote asset -> token -> quote asset)
through the v4 Quoter and refuses the pool unless the sell side actually quotes,
the embedded tax rate is within policy, and the round-trip loss is within policy.

It is strictly read-only: every RPC call is an ``eth_call`` against a quoter, it
never signs, broadcasts, or touches the ledger. A transport failure is never
interpreted as a pool verdict — it propagates so the caller fails closed with an
accurate reason instead of mislabelling a network blip as a honeypot.
"""
from __future__ import annotations

from dataclasses import dataclass

from eth_abi import decode, encode
from eth_utils import keccak

from .models import address
from .registry import chain_for
from .rpc import RpcError

POOL_KEY = "(address,address,uint24,int24,address)"
_QUOTE_PARAMS = f"({POOL_KEY},bool,uint128,bytes)"
_UINT128_MAX = 2**128 - 1

# The known Arc tax-hook template (see module docstring). The rate lives at a
# fixed offset in the deployed code because it is an immutable, so it can be
# read with one eth_getCode instead of a hook-specific ABI call.
TAX_HOOK_CODE_SIZE = 5175
TAX_HOOK_RATE_OFFSET = 855


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


@dataclass(frozen=True)
class ArcPoolSafetyPolicy:
    """Operator-tunable thresholds for the Arc round-trip gate.

    max_tax_bps          refuse pools whose hook embeds a higher transaction tax
    max_round_trip_loss_bps  refuse pools that give back too little on a probe
                         round trip (tax + hook skim + fees + probe slippage)
    probe_amount_raw     quote-asset amount used for the probe, in raw units
                         (Arc USDC has 6 decimals, so 1_000_000 == 1 USDC)
    """

    max_tax_bps: int = 500
    max_round_trip_loss_bps: int = 1500
    probe_amount_raw: int = 1_000_000

    def __post_init__(self):
        for name in ("max_tax_bps", "max_round_trip_loss_bps"):
            value = getattr(self, name)
            if type(value) is not int or not 0 <= value <= 10_000:
                raise ValueError(f"invalid {name}")
        if type(self.probe_amount_raw) is not int or not 0 < self.probe_amount_raw <= _UINT128_MAX:
            raise ValueError("invalid probe_amount_raw")


def _normalise_pool_key(pool_key):
    if not isinstance(pool_key, (list, tuple)) or len(pool_key) != 5:
        raise ValueError("evidenced V4 pool key required")
    key = (address(pool_key[0]), address(pool_key[1]), int(pool_key[2]),
           int(pool_key[3]), address(pool_key[4]))
    if key[0] == key[1]:
        raise ValueError("degenerate V4 pool key")
    return key


async def quote_exact_input_single(rpc, quoter, pool_key, zero_for_one, amount,
                                   *, hook_data=b"", block_tag="latest"):
    """One exact-input v4 quote. Returns None when that direction REVERTS.

    A revert is a verdict about the pool; a transport failure is not, so the
    latter is re-raised rather than folded into None.
    """
    if not 0 < amount <= _UINT128_MAX:
        raise ValueError("quote amount out of uint128 range")
    data = _selector(f"quoteExactInputSingle({_QUOTE_PARAMS})") + encode(
        [_QUOTE_PARAMS], [(pool_key, bool(zero_for_one), amount, hook_data)])
    try:
        raw = await rpc.call("eth_call", [
            {"to": quoter, "data": "0x" + data.hex()}, block_tag])
    except RpcError as exc:
        if (exc.diagnostic or {}).get("kind") == "transport_or_response_error":
            raise
        return None  # execution reverted: this direction is not tradable
    if not isinstance(raw, str) or not raw.startswith("0x"):
        raise ValueError("invalid quote RPC result")
    result = bytes.fromhex(raw[2:])
    if len(result) < 64:
        return None
    amount_out, _gas = decode(["uint256", "uint256"], result[:64])
    return int(amount_out)


async def read_hook_tax_bps(rpc, hook, *, block_tag="latest"):
    """Immutable tax rate (bps) of the known Arc tax-hook template, else None.

    None means "unknown hook shape", never "no tax" — the caller must not treat
    an unrecognised hook as tax-free. The round-trip loss check below is what
    bounds an unknown hook.
    """
    hook = address(hook)
    if int(hook, 16) == 0:
        return None
    code = await rpc.call("eth_getCode", [hook, block_tag])
    if not isinstance(code, str) or not code.startswith("0x"):
        return None
    body = bytes.fromhex(code[2:])
    if len(body) != TAX_HOOK_CODE_SIZE:
        return None
    return int.from_bytes(body[TAX_HOOK_RATE_OFFSET:TAX_HOOK_RATE_OFFSET + 2], "big")


async def verify_arc_pool_sellable(rpc, pool_key, *, chain_id, policy,
                                   quote_asset=None, hook_data=b"", block_tag="latest"):
    """Round-trip probe of an Arc v4 pool. Read-only; never executes anything.

    Returns ``{"accepted": bool, "reason": str | None, ...evidence}``. Amounts in
    the evidence are decimal strings so they survive JSON without precision loss.
    Refusal reasons:
      pool_not_quoted_in_quote_asset  the probe asset is not a pool currency
      buy_not_quotable                even the buy side reverts
      pool_cannot_be_sold_out_of      buy quotes but sell reverts (honeypot /
                                      ungraduated bonding curve — indistinguishable
                                      on chain and identical in consequence)
      tax_rate_above_policy           hook's immutable tax exceeds max_tax_bps
      round_trip_loss_above_policy    probe came back short of max_round_trip_loss_bps
    """
    chain = chain_for(chain_id)
    key = _normalise_pool_key(pool_key)
    quoter = chain.v4_quoter
    if quoter is None:
        raise ValueError(f"chain {chain.chain_id} has no V4 quoter configured")
    if quote_asset is None:
        quote_asset = chain.usdc_erc20
        if quote_asset is None:
            raise ValueError(f"chain {chain.chain_id} has no default quote asset")
    quote_asset = address(quote_asset)
    evidence = {
        "chain_id": chain.chain_id,
        "quote_asset": quote_asset,
        "hook": key[4],
        "probe_amount_raw": str(policy.probe_amount_raw),
        "bought_raw": None,
        "returned_raw": None,
        "round_trip_loss_bps": None,
        "tax_bps": None,
    }

    def verdict(accepted, reason):
        return {"accepted": accepted, "reason": reason, **evidence}

    if quote_asset not in (key[0], key[1]):
        return verdict(False, "pool_not_quoted_in_quote_asset")
    buy_zero_for_one = quote_asset == key[0]

    bought = await quote_exact_input_single(
        rpc, quoter, key, buy_zero_for_one, policy.probe_amount_raw,
        hook_data=hook_data, block_tag=block_tag)
    if not bought:
        return verdict(False, "buy_not_quotable")
    evidence["bought_raw"] = str(bought)
    if bought > _UINT128_MAX:
        return verdict(False, "buy_not_quotable")

    returned = await quote_exact_input_single(
        rpc, quoter, key, not buy_zero_for_one, bought,
        hook_data=hook_data, block_tag=block_tag)
    if not returned:
        # The make-or-break Arc check: we could buy in but not sell back out.
        return verdict(False, "pool_cannot_be_sold_out_of")
    evidence["returned_raw"] = str(returned)
    loss_bps = max(0, (policy.probe_amount_raw - returned) * 10_000 // policy.probe_amount_raw)
    evidence["round_trip_loss_bps"] = loss_bps

    tax_bps = await read_hook_tax_bps(rpc, key[4], block_tag=block_tag)
    evidence["tax_bps"] = tax_bps
    if tax_bps is not None and tax_bps > policy.max_tax_bps:
        return verdict(False, "tax_rate_above_policy")
    if loss_bps > policy.max_round_trip_loss_bps:
        return verdict(False, "round_trip_loss_above_policy")
    return verdict(True, None)
