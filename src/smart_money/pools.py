"""Historical, read-only V2/V3 factory and pool identity verification."""
from __future__ import annotations

from eth_abi import decode, encode
from eth_utils import keccak

from .models import Signal, address, number
from . import registry as R


ZERO = "0x" + "00" * 20


def _selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def _call_data(signature: str, types: list[str] | None = None, values: list | None = None) -> str:
    return "0x" + (_selector(signature) + encode(types or [], values or [])).hex()


def _address_result(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("invalid eth_call result")
    return address(decode(["address"], bytes.fromhex(value[2:]))[0])


async def verify_signal_pools(rpc, signals: list[Signal], receipt: dict) -> dict[str, dict]:
    """Return strict evidence keyed by event_id; failures remain reviewable data."""
    block = hex(number(receipt.get("blockNumber", 0)))
    results = {}
    factory_checked: dict[str, bool] = {}

    async def call(to: str, data: str):
        return await rpc.call("eth_call", [{"to": to, "data": data}, block])

    async def factory_has_code(factory: str) -> bool:
        if factory not in factory_checked:
            factory_checked[factory] = (await rpc.call("eth_getCode", [factory, block])) not in ("0x", "0x0")
        return factory_checked[factory]

    for signal in signals:
        if signal.protocol not in ("v2", "v3", "v4") or signal.behavior not in {"BUY", "SELL", "TOKEN_SWAP"}:
            continue
        evidence = {"verified": False, "block_number": str(number(receipt.get("blockNumber", 0))),
                    "protocol": signal.protocol, "pools": []}
        results[signal.event_id] = evidence
        try:
            if signal.protocol == "v4":
                keys = ([signal.evidence.get("pool_key", [])] if "pool_key" in signal.evidence else
                        [hop.get("pool_key", []) for hop in signal.evidence.get("v4_hops", [])])
                if not keys:
                    raise ValueError("invalid_v4_pool_key")
                manager_code = await rpc.call("eth_getCode", [R.V4_MANAGER, block])
                if manager_code in ("0x", "0x0"):
                    raise ValueError("v4_manager_has_no_code_at_receipt_block")
                computed_ids = []
                for key in keys:
                    if len(key) != 5 or address(key[0]) >= address(key[1]) or int(key[3]) <= 0:
                        raise ValueError("invalid_v4_pool_key")
                    normalized = (address(key[0]), address(key[1]), int(key[2]),
                                  int(key[3]), address(key[4]))
                    computed = "0x" + keccak(encode(
                        ["(address,address,uint24,int24,address)"], [normalized])).hex()
                    computed_ids.append(computed)
                    hook = normalized[4]
                    pool_evidence = {"pool_id": computed, "pool_key": list(normalized), "hook": hook}
                    if hook != R.NATIVE:
                        hook_code = await rpc.call("eth_getCode", [hook, block])
                        observed_hash = "0x" + keccak(bytes.fromhex(hook_code[2:])).hex()
                        if R.KNOWN_V4_HOOK_CODE_HASHES.get(hook) != observed_hash:
                            raise ValueError("v4_hook_code_not_recognized")
                        pool_evidence["hook_code_hash"] = observed_hash
                    evidence["pools"].append(pool_evidence)
                declared = ([signal.pool_id] if signal.pool_id else signal.evidence.get("v4_pool_ids", []))
                if computed_ids != declared:
                    raise ValueError("v4_pool_id_mismatch")
                evidence.update({"verified": True, "manager": R.V4_MANAGER,
                                 "pool_ids": computed_ids})
                if len(evidence["pools"]) == 1:
                    evidence["pool_id"] = computed_ids[0]
                    evidence["hook"] = evidence["pools"][0]["hook"]
                    if "hook_code_hash" in evidence["pools"][0]:
                        evidence["hook_code_hash"] = evidence["pools"][0]["hook_code_hash"]
                continue
            if signal.protocol == "v2":
                route = [address(a) for a in signal.evidence.get("route", [])]
                if len(route) < 2:
                    raise ValueError("v2_route_unavailable")
                factory, router = R.V2_FACTORY, R.V2_ROUTER
                hops = [(a, b, None) for a, b in zip(route, route[1:])]
                lookup = "getPair(address,address)"
            else:
                factory, router = R.V3_FACTORY, R.V3_ROUTER
                if "hops" in signal.evidence:
                    hops = [(address(h["token_in"]), address(h["token_out"]), int(h["fee"]))
                            for h in signal.evidence["hops"]]
                    if not hops:
                        raise ValueError("v3_path_fees_unavailable")
                elif "fee" in signal.evidence:
                    hops = [(signal.token_in, signal.token_out, int(signal.evidence["fee"]))]
                else:
                    raise ValueError("v3_path_fees_unavailable")
                lookup = "getPool(address,address,uint24)"

            if not await factory_has_code(factory):
                raise ValueError("factory_has_no_code_at_receipt_block")
            if signal.contract == router:
                observed_factory = _address_result(await call(router, _call_data("factory()")))
                if observed_factory != factory:
                    raise ValueError("router_factory_mismatch")

            for token_in, token_out, fee in hops:
                types, values = ["address", "address"], [token_in, token_out]
                if fee is not None:
                    types.append("uint24")
                    values.append(fee)
                pool = _address_result(await call(factory, _call_data(lookup, types, values)))
                if pool == ZERO:
                    raise ValueError("factory_pool_missing")
                if (await rpc.call("eth_getCode", [pool, block])) in ("0x", "0x0"):
                    raise ValueError("pool_has_no_code_at_receipt_block")
                token0 = _address_result(await call(pool, _call_data("token0()")))
                token1 = _address_result(await call(pool, _call_data("token1()")))
                if {token0, token1} != {address(token_in), address(token_out)}:
                    raise ValueError("pool_token_pair_mismatch")
                evidence["pools"].append({"address": pool, "token0": token0, "token1": token1,
                                          **({"fee": str(fee)} if fee is not None else {})})
            evidence["factory"] = factory
            evidence["verified"] = True
        except (ValueError, TypeError) as exc:
            evidence["reason"] = str(exc)
    return results
