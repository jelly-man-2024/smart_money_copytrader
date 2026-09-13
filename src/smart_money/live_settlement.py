"""Canonical receipt settlement for a confirmed live ERC-20 execution."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib

from .models import address, number
from .receipts import TRANSFER


def _topic_address(value: str) -> str:
    if not isinstance(value, str) or len(value) != 66 or not value.startswith("0x"):
        raise ValueError("invalid indexed transfer address")
    return address("0x" + value[-40:])


def wallet_erc20_deltas(receipt: dict, wallet: str) -> dict[str, int]:
    wallet = address(wallet)
    deltas: dict[str, int] = {}
    for log in receipt.get("logs", []):
        topics = log.get("topics", []) if isinstance(log, dict) else []
        if (log.get("removed") or len(topics) != 3
                or topics[0].lower() != TRANSFER):
            continue
        token = address(log.get("address"))
        sender, recipient = _topic_address(topics[1]), _topic_address(topics[2])
        raw = log.get("data")
        if not isinstance(raw, str) or not raw.startswith("0x"):
            raise ValueError("invalid transfer amount")
        amount = int(raw, 16)
        if sender == wallet:
            deltas[token] = deltas.get(token, 0) - amount
        if recipient == wallet:
            deltas[token] = deltas.get(token, 0) + amount
    return deltas


async def settle_confirmed_execution(store, rpc, proposal_id: str,
                                     tx_hash: str) -> dict:
    proposal = store.paper_proposal(proposal_id)
    plan = store.execution_plan(proposal_id)
    if (proposal is None or proposal["status"] != "reserved" or plan is None
            or plan.get("status") != "signed"):
        raise ValueError("confirmed reserved execution is unavailable")
    attempts = [attempt for attempt in store.execution_attempts(plan["plan_id"])
                if attempt.get("tx_hash", "").lower() == tx_hash.lower()
                and attempt.get("status") == "confirmed"]
    if len(attempts) != 1:
        raise ValueError("confirmed execution attempt is unavailable")
    attempt = attempts[0]
    receipt = await rpc.call("eth_getTransactionReceipt", [tx_hash])
    if (not isinstance(receipt, dict)
            or str(receipt.get("transactionHash", "")).lower() != tx_hash.lower()
            or number(receipt.get("status", 0)) != 1):
        raise ValueError("successful execution receipt is unavailable")
    block_number = number(receipt.get("blockNumber"))
    if (attempt.get("block_number") != block_number
            or str(attempt.get("block_hash", "")).lower()
            != str(receipt.get("blockHash", "")).lower()):
        raise ValueError("execution attempt does not match receipt block")
    header = await rpc.call("eth_getBlockByNumber", [hex(block_number), False])
    if (not isinstance(header, dict)
            or str(header.get("hash", "")).lower()
            != str(receipt.get("blockHash", "")).lower()):
        raise ValueError("execution receipt is not on the canonical block")
    follower = address(proposal["attribution"].get("follower_wallet"))
    input_asset, output_asset = (address(proposal["input_asset"]),
                                 address(proposal["output_asset"]))
    deltas = wallet_erc20_deltas(receipt, follower)
    actual_input = -deltas.get(input_asset, 0)
    actual_output = deltas.get(output_asset, 0)
    if actual_input != int(proposal["amount_in_raw"]) or actual_output <= 0:
        raise ValueError("execution receipt balance deltas do not match proposal")
    gas_cost = number(receipt.get("gasUsed")) * number(receipt.get("effectiveGasPrice"))
    plan_body = plan.get("unsigned_plan") or {}
    observed = float(plan_body.get("quote_observed_at", 0))
    block_time = number(header.get("timestamp", 0))
    def identity(kind: str) -> str:
        return hashlib.sha256(f"live:{kind}:{proposal_id}".encode()).hexdigest()
    common = {
        "order_id": identity("order"), "fill_id": identity("fill"),
        "amount_out_raw": str(actual_output), "fee_asset": output_asset,
        "fee_amount_raw": "0", "gas_cost_wei": str(gas_cost),
        "quote_observed_at": datetime.fromtimestamp(
            observed, timezone.utc).isoformat(),
        "filled_at": datetime.fromtimestamp(block_time, timezone.utc).isoformat(),
    }
    behavior = proposal["attribution"].get("source_behavior")
    if behavior == "SELL":
        filled = store.fill_paper_sell(proposal_id, common)
    else:
        filled = store.fill_paper_buy(proposal_id, {
            **common, "lot_id": identity("lot"),
        })
    if not filled:
        raise ValueError("confirmed execution could not settle reserved proposal")
    return {
        "proposal_id": proposal_id, "tx_hash": tx_hash.lower(),
        "fill_id": common["fill_id"], "side": "SELL" if behavior == "SELL" else "BUY",
        "actual_input_raw": str(actual_input), "actual_output_raw": str(actual_output),
        "gas_cost_wei": str(gas_cost), "block_number": block_number,
        "block_hash": receipt["blockHash"].lower(),
    }
