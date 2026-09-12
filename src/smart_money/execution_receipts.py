"""Read-only observation of externally broadcast execution transactions.

This module deliberately has no broadcast surface.  It only accepts a known hash,
loads the transaction through the read-only RPC client, proves that its public
fields still match a persisted plan, and records its observed lifecycle.
"""
from __future__ import annotations

from dataclasses import dataclass

from .models import address, number


def _hash(value: str, name: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ValueError(f"invalid {name}")
    try:
        bytes.fromhex(value[2:])
    except ValueError:
        raise ValueError(f"invalid {name}") from None
    return value.lower()


def _data(value: str) -> str:
    if not isinstance(value, str) or not value.startswith("0x"):
        raise ValueError("invalid transaction calldata")
    try:
        bytes.fromhex(value[2:])
    except ValueError:
        raise ValueError("invalid transaction calldata") from None
    return value.lower()


@dataclass(frozen=True)
class ExecutionObservation:
    tx_hash: str
    status: str
    block_number: int | None = None
    block_hash: str | None = None


class ReadOnlyExecutionTracker:
    """Track a signed hash or explicit replacement without submitting either."""

    def __init__(self, store, rpc):
        self.store, self.rpc = store, rpc

    @staticmethod
    def _validated_public_transaction(raw: dict, expected: dict,
                                      tx_hash: str, replacement: bool) -> dict:
        if not isinstance(raw, dict) or _hash(raw.get("hash"), "transaction hash") != tx_hash:
            raise ValueError("RPC transaction hash mismatch")
        calldata = raw.get("input", raw.get("data"))
        public = {
            "hash": tx_hash,
            "from": address(raw.get("from")),
            "to": address(raw.get("to")),
            "nonce": number(raw.get("nonce")),
            "chainId": number(raw.get("chainId")),
            "type": number(raw.get("type")),
            "gas": number(raw.get("gas")),
            "value": number(raw.get("value")),
            "data": _data(calldata),
            "maxFeePerGas": number(raw.get("maxFeePerGas")),
            "maxPriorityFeePerGas": number(raw.get("maxPriorityFeePerGas")),
        }
        exact = {
            "from": address(expected["from"]), "to": address(expected["to"]),
            "nonce": number(expected["nonce"]), "chainId": number(expected["chainId"]),
            "type": number(expected["type"]), "gas": number(expected["gas"]),
            "value": number(expected["value"]), "data": _data(expected["data"]),
        }
        if any(public[key] != value for key, value in exact.items()):
            raise ValueError("observed transaction does not match execution intent")
        expected_max = number(expected["maxFeePerGas"])
        expected_priority = number(expected["maxPriorityFeePerGas"])
        if replacement:
            if (public["maxFeePerGas"] < expected_max
                    or public["maxPriorityFeePerGas"] < expected_priority
                    or (public["maxFeePerGas"] == expected_max
                        and public["maxPriorityFeePerGas"] == expected_priority)):
                raise ValueError("replacement transaction fees were not increased")
        elif (public["maxFeePerGas"] != expected_max
              or public["maxPriorityFeePerGas"] != expected_priority):
            raise ValueError("observed transaction fees do not match signed plan")
        return public

    async def observe(self, proposal_id: str, tx_hash: str | None = None,
                      replaces_tx_hash: str | None = None) -> ExecutionObservation:
        plan = self.store.execution_plan(proposal_id)
        if plan is None or plan["status"] != "signed" or not plan["signed_tx_hash"]:
            raise ValueError("signed execution plan is unavailable")
        original_hash = _hash(plan["signed_tx_hash"], "signed transaction hash")
        tx_hash = _hash(tx_hash or original_hash, "transaction hash")
        replacement = replaces_tx_hash is not None
        if replacement:
            replaces_tx_hash = _hash(replaces_tx_hash, "replaced transaction hash")
            active = [item for item in self.store.execution_attempts(plan["plan_id"])
                      if item["status"] in {"signed", "observed_pending"}]
            if len(active) != 1 or active[0]["tx_hash"] != replaces_tx_hash:
                raise ValueError("replacement does not target the active execution attempt")
        elif tx_hash != original_hash:
            raise ValueError("unrecognized execution transaction hash")

        raw = await self.rpc.call("eth_getTransactionByHash", [tx_hash])
        if raw is None:
            return ExecutionObservation(tx_hash, "not_observed")
        expected = {**plan["transaction"], "from": plan["follower_wallet"]}
        if replacement:
            # A second replacement must outbid the currently active attempt, not
            # merely the original signed plan.
            expected["maxFeePerGas"] = active[0]["public_payload"]["maxFeePerGas"]
            expected["maxPriorityFeePerGas"] = active[0]["public_payload"][
                "maxPriorityFeePerGas"]
        public = self._validated_public_transaction(raw, expected, tx_hash, replacement)
        attempts = {item["tx_hash"]: item for item in
                    self.store.execution_attempts(plan["plan_id"])}
        if tx_hash not in attempts:
            if not replacement or not self.store.observe_execution_attempt(
                    plan["plan_id"], tx_hash, public, replaces_tx_hash):
                raise ValueError("execution attempt state changed concurrently")
        elif attempts[tx_hash]["status"] in {"signed", "orphaned"}:
            if not self.store.observe_execution_attempt(plan["plan_id"], tx_hash, public):
                raise ValueError("execution attempt state changed concurrently")

        receipt = await self.rpc.call("eth_getTransactionReceipt", [tx_hash])
        if receipt is None:
            return ExecutionObservation(tx_hash, "observed_pending")
        if (not isinstance(receipt, dict)
                or _hash(receipt.get("transactionHash"), "receipt transaction hash") != tx_hash):
            raise ValueError("RPC receipt transaction hash mismatch")
        block_number = number(receipt.get("blockNumber"))
        block_hash = _hash(receipt.get("blockHash"), "receipt block hash")
        block = await self.rpc.call("eth_getBlockByNumber", [hex(block_number), False])
        if block is None:
            return ExecutionObservation(tx_hash, "observed_pending")
        canonical_hash = _hash(block.get("hash"), "canonical block hash")
        if canonical_hash != block_hash:
            self.store.finalize_execution_attempt(
                tx_hash, "orphaned", block_number, block_hash)
            return ExecutionObservation(tx_hash, "orphaned", block_number, block_hash)
        receipt_status = number(receipt.get("status"))
        if receipt_status not in {0, 1}:
            raise ValueError("invalid receipt status")
        status = "confirmed" if receipt_status == 1 else "reverted"
        if not self.store.finalize_execution_attempt(tx_hash, status, block_number, block_hash):
            current = {item["tx_hash"]: item for item in
                       self.store.execution_attempts(plan["plan_id"])}.get(tx_hash)
            if current is None or current["status"] != status:
                raise ValueError("execution finalization state changed concurrently")
        return ExecutionObservation(tx_hash, status, block_number, block_hash)
