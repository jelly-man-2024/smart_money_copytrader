"""Exact transaction-prestate account implementation lookup via bounded trace."""
from __future__ import annotations

from .registry import delegation


async def prestate_implementations(rpc, tx_hash: str,
                                   wallets: list[str]) -> tuple[dict[str, str], set[str]]:
    trace = await rpc.call("debug_traceTransaction", [tx_hash, {"tracer": "prestateTracer"}])
    if not isinstance(trace, dict):
        raise ValueError("invalid account prestate trace")
    accounts = {key.lower(): value for key, value in trace.items()
                if isinstance(key, str) and isinstance(value, dict)}
    implementations = {}
    missing = set()
    for wallet in wallets:
        account = accounts.get(wallet)
        if account is None:
            missing.add(wallet)
            continue
        code = account.get("code", "0x")
        impl = delegation(code)
        if impl:
            implementations[wallet] = impl
        elif code not in ("0x", "0x0"):
            missing.add(wallet)
    return implementations, missing
