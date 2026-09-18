"""Relationship-bound ERC-20 approvals for verified local execution routers."""
from __future__ import annotations

from dataclasses import dataclass
import time

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from .execution_pipeline import ReadOnlyBroadcastReview
from .key_source import LiveDatabaseSigner
from .models import address, number
from .registry import (
    CHAIN_ID, KYBER_META_AGGREGATION_ROUTER_V2, NATIVE, USDG, V2_ROUTER, V3_ROUTER,
    chain_for,
)

USDG_BUDGET_APPROVAL_MULTIPLIER = 200
def approval_spenders(chain_id: int) -> frozenset[str]:
    """Contracts this chain may be asked to approve, resolved from its registry.

    0x is deliberately absent on every chain: its allowance is granted by the
    reviewed one-off operator path, not by this one, and the live executor skips
    approvals for 0x accordingly. A chain that deploys none of these allows no
    approval at all.
    """
    chain = chain_for(chain_id)
    return frozenset(spender for spender in (
        chain.v2_router, chain.v3_router, chain.kyber_router,
    ) if spender is not None)


# Robinhood-chain alias kept for existing call sites and tests.
APPROVAL_SPENDERS = approval_spenders(CHAIN_ID)


@dataclass(frozen=True)
class ApprovalResult:
    tx_hash: str | None
    asset: str
    spender: str
    amount_raw: str
    previous_allowance_raw: str
    submitted: bool


def _allowance_data(owner: str, spender: str) -> str:
    return "0x" + (keccak(text="allowance(address,address)")[:4]
                   + encode(["address", "address"], [owner, spender])).hex()


async def approve_relationship_token(
        policy, rpc, relationship_gate, broadcaster, token: str,
        amount_raw: str, spender: str = V3_ROUTER,
        signer_factory=LiveDatabaseSigner, *,
        minimum_required_raw: str | None = None) -> ApprovalResult:
    """Ensure the required allowance, approving at most the bounded target."""
    if (policy.run_mode != "mainnet_live" or policy.follower_wallet is None
            or policy.relationship_id is None):
        raise ValueError("relationship is not eligible for token approval")
    token, spender = address(token), address(spender)
    # A policy without an explicit chain means Robinhood Chain, matching the
    # relationship dataclass default.
    chain_id = getattr(policy, "chain_id", CHAIN_ID)
    if token == NATIVE or spender not in approval_spenders(chain_id):
        raise ValueError("token or spender is not eligible for approval")
    if (not isinstance(amount_raw, str) or not amount_raw.isdecimal()
            or int(amount_raw) <= 0 or int(amount_raw) >= 2 ** 256):
        raise ValueError("relationship token approval amount is invalid")
    minimum_required_raw = (amount_raw if minimum_required_raw is None
                            else minimum_required_raw)
    if (not isinstance(minimum_required_raw, str)
            or not minimum_required_raw.isdecimal()
            or int(minimum_required_raw) <= 0
            or int(minimum_required_raw) > int(amount_raw)):
        raise ValueError("relationship token minimum allowance is invalid")
    relationship_gate.validate(
        policy.relationship_id, policy.follower_wallet, policy.wallet,
        policy.snapshot_hash)
    code = await rpc.call("eth_getCode", [token, "pending"])
    if not isinstance(code, str) or code in {"0x", "0x0", "0x00"}:
        raise ValueError("approval asset has no contract code")
    allowance = number(await rpc.call(
        "eth_call", [{"to": token, "data": _allowance_data(
            policy.follower_wallet, spender)}, "pending"]))
    if allowance >= int(minimum_required_raw):
        return ApprovalResult(
            None, token, spender, amount_raw, str(allowance), False)
    nonce = number(await rpc.call(
        "eth_getTransactionCount", [policy.follower_wallet, "pending"]))
    latest_nonce = number(await rpc.call(
        "eth_getTransactionCount", [policy.follower_wallet, "latest"]))
    if nonce != latest_nonce:
        raise ValueError("follower wallet has a pending transaction")
    gas_price = number(await rpc.call("eth_gasPrice"))
    max_fee = (gas_price * 12 + 9) // 10
    gas_limit = 100_000
    gas_cost = gas_limit * max_fee
    if gas_cost > int(policy.quote_policy.max_gas_cost_wei):
        raise ValueError("approval gas cost exceeds relationship limit")
    balance = number(await rpc.call(
        "eth_getBalance", [policy.follower_wallet, "pending"]))
    if balance < gas_cost:
        raise ValueError("insufficient native balance for approval gas")
    approval_data = "0x" + (keccak(text="approve(address,uint256)")[:4]
                             + encode(["address", "uint256"],
                                      [spender, int(amount_raw)])).hex()
    simulation = await rpc.call("eth_call", [{
        "from": policy.follower_wallet, "to": token, "data": approval_data,
    }, "pending"])
    if number(simulation) != 1:
        raise ValueError("token approval simulation did not return true")
    transaction = {
        "chainId": chain_id, "nonce": nonce, "to": to_checksum_address(token),
        "value": 0, "data": approval_data, "gas": gas_limit,
        "maxFeePerGas": max_fee, "maxPriorityFeePerGas": 0, "type": 2,
    }
    signer = signer_factory(
        policy.follower_wallet, policy.relationship_id, policy.snapshot_hash)
    raw = signer.sign_transaction(transaction)
    signed_hash = "0x" + keccak(raw).hex()
    review = ReadOnlyBroadcastReview(
        f"approval:{policy.relationship_id}:{policy.snapshot_hash}:{token}:{spender}",
        signed_hash, time.time(), {
            "broadcast_performed": False, "relationship_revalidated": True,
            "asset": token, "spender": spender, "amount_raw": amount_raw,
            "previous_allowance_raw": str(allowance),
            "gas_cost_limit_wei": str(gas_cost),
        })
    result = await broadcaster.broadcast(
        review, raw, follower_wallet=policy.follower_wallet,
        relationship_id=policy.relationship_id,
        config_snapshot_hash=policy.snapshot_hash)
    return ApprovalResult(
        result.tx_hash, token, spender, amount_raw, str(allowance), True)


async def confirm_relationship_token_approval(
        rpc, result: ApprovalResult, owner: str, *, attempts: int = 120,
        interval: float = 0.5) -> dict | None:
    """Wait for a submitted approval's successful canonical receipt and allowance."""
    if not result.submitted:
        return None
    receipt = await rpc.receipt(result.tx_hash, attempts=attempts, interval=interval)
    if (not isinstance(receipt, dict)
            or str(receipt.get("transactionHash", "")).lower() != result.tx_hash.lower()
            or number(receipt.get("status", 0)) != 1):
        raise ValueError("successful approval receipt is unavailable")
    block_number = number(receipt.get("blockNumber"))
    block_hash = str(receipt.get("blockHash", "")).lower()
    header = await rpc.call("eth_getBlockByNumber", [hex(block_number), False])
    if (not isinstance(header, dict)
            or str(header.get("hash", "")).lower() != block_hash):
        raise ValueError("approval receipt is not on the canonical block")
    allowance = number(await rpc.call(
        "eth_call", [{"to": result.asset, "data": _allowance_data(
            owner, result.spender)}, "latest"]))
    if allowance < int(result.amount_raw):
        raise ValueError("confirmed approval allowance is insufficient")
    return {
        "tx_hash": result.tx_hash, "asset": result.asset,
        "spender": result.spender, "amount_raw": result.amount_raw,
        "allowance_raw": str(allowance), "block_number": block_number,
        "block_hash": block_hash,
    }


async def approve_relationship_usdg(policy, rpc, relationship_gate, broadcaster,
                                    signer_factory=LiveDatabaseSigner, *,
                                    minimum_required_raw: str | None = None,
                                    spender: str = V3_ROUTER) -> ApprovalResult:
    """Approve a bounded multiple of one relationship's USDG budget."""
    if USDG not in policy.allowed_assets:
        raise ValueError("relationship is not eligible for USDG approval")
    budget = policy.budget_limits.get("USDG")
    if not isinstance(budget, str) or not budget.isdecimal() or int(budget) <= 0:
        raise ValueError("relationship USDG budget is invalid")
    amount_value = int(budget) * USDG_BUDGET_APPROVAL_MULTIPLIER
    if amount_value >= 2 ** 256:
        raise ValueError("relationship USDG approval exceeds uint256")
    return await approve_relationship_token(
        policy, rpc, relationship_gate, broadcaster, USDG, str(amount_value),
        spender, signer_factory,
        minimum_required_raw=minimum_required_raw)
