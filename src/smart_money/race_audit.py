"""Explicit post-hoc deployment audit. Never creates early decision snapshots."""
import asyncio
from collections import Counter
import time

from eth_utils import keccak

from .early_intent import hash32, parse_candidates
from .early_replay import transaction_from_record
from .registry import CHAIN_ID
from .relay_race import RACE_ADDRESS, RACE_CODE_HASH, RACE_RULE, verify_deployment


async def audit_deployments(cases, rpc):
    if len(cases) > 1000:
        raise ValueError("race_audit_case_limit")
    if int(await rpc.call("eth_chainId"), 16) != CHAIN_ID:
        raise ValueError("race_audit_wrong_chain")
    selected, hashes = [], set()
    for i, case in enumerate(cases):
        try:
            tx = transaction_from_record(case["transaction"])
            candidates = parse_candidates(tx, case["wallet"]).candidates
        except (ValueError, TypeError, KeyError, AttributeError):
            selected.append({"index": i, "status": "invalid_case"})
            continue
        if not any(c.route_kind == "relay_wrapper" for c in candidates):
            continue
        row = {"index": i, "tx_hash": tx.hash, "wallet": case["wallet"],
               "observation_source": tx.observation_source}
        truth = case.get("truth") or {}
        try:
            if truth.get("tx_hash") != tx.hash or truth.get("wallet") != case["wallet"]:
                raise ValueError("audit_truth_identity_mismatch")
            block = hash32(truth.get("evidence", {}).get("block_hash"))
            row["block_hash"] = block
            hashes.add(block)
        except ValueError:
            row["status"] = "historical_block_unavailable"
        selected.append(row)
    if len(hashes) > 256:
        raise ValueError("race_audit_block_limit")
    gate = asyncio.Semaphore(2)

    async def inspect(block):
        async with gate:
            try:
                code = await rpc.call("eth_getCode", [RACE_ADDRESS, {
                    "blockHash": block, "requireCanonical": True}])
                try:
                    result = verify_deployment({"chain_id": CHAIN_ID, "contract": RACE_ADDRESS,
                                                "rule": RACE_RULE, "code": code})
                    return block, {"status": "matched", "runtime_code_hash": result["runtime_code_hash"],
                                   "observed_at": time.time()}
                except ValueError:
                    raw = bytes.fromhex(code[2:]) if isinstance(code, str) and code.startswith("0x") else b""
                    return block, {"status": "mismatch", "runtime_code_hash": "0x" + keccak(raw).hex(),
                                   "observed_at": time.time()}
            except Exception as exc:
                return block, {"status": "lookup_failed", "error_type": type(exc).__name__,
                               "observed_at": time.time()}

    results = dict(await asyncio.gather(*(inspect(b) for b in sorted(hashes))))
    for row in selected:
        if "block_hash" in row:
            row.update(results[row["block_hash"]])
    return {"scope": "posthoc_historical_deployment_not_early_availability",
            "observed_at": time.time(), "contract": RACE_ADDRESS, "expected_code_hash": RACE_CODE_HASH,
            "queried_blocks": len(hashes), "selected_records": len(selected),
            "counts": dict(Counter(row["status"] for row in selected)), "rows": selected,
            "counts_by_source": dict(Counter(row.get("observation_source", "invalid") + ":" + row["status"]
                                             for row in selected)),
            "source_verified": False, "early_snapshots_created": False, "live_enabled": False}
