"""Operator-run, one-shot USDG approval. Default is public read-only checks.

Never imported by the monitor. Signing uses the existing gated key DB source;
only --execute --confirm-approve-100-usdg permits signing and one broadcast.

An allowance is a cumulative spending budget, not a per-trade cap: the first
grant of 10 USDG paid for exactly 100 copied buys of 0.1 USDG and then read
zero, after which every buy fell back to Kyber without a word. The journal
path carries the amount so a later, larger grant is a new one-shot rather
than a blocked repeat.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import time

from eth_abi import encode
from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

from .approval import ApprovalResult, _allowance_data, confirm_relationship_token_approval
from .broadcast import MainnetBroadcaster
from .config import load_endpoint_env
from .execution_controls import require_mainnet_signing_enabled
from .execution_pipeline import ReadOnlyBroadcastReview
from .key_source import LiveDatabaseSigner
from .models import number
from .mysql_config import load_enabled_relationship_policy, mysql_connection
from .registry import CHAIN_ID, USDG
from .rpc import ReadOnlyRpc
from .runtime_safety import runtime_instance_lock

FOLLOWER = "0x3004ab92565deeea0a2eaa27e40e297bb457e1a6"
# Verified against official 0x Contracts docs and this chain's quote response.
SPENDER = "0x0000000000001ff3684f28c67538d4d072c22734"
AMOUNT = 100000000
JOURNAL = Path(f"var/zeroex-usdg-approval-{AMOUNT}.jsonl")

# Plan status is a preparation/signing lifecycle, NOT the receipt lifecycle.
# A signed plan is resolved only with exactly one terminal attempt and no live
# or unknown attempts. Replaced ancestors are allowed alongside that terminal.
UNRESOLVED_PLANS_SQL = """
SELECT COUNT(*) AS n FROM execution_plans p
WHERE p.follower_wallet=%s AND (
    p.status IS NULL OR p.status NOT IN ('signed','cancelled')
    OR EXISTS (SELECT 1 FROM execution_attempts a WHERE a.plan_id=p.plan_id
               AND (a.status IS NULL OR a.status NOT IN ('confirmed','reverted','replaced')))
    OR (p.status='signed' AND
        (SELECT COUNT(*) FROM execution_attempts a WHERE a.plan_id=p.plan_id
         AND a.status IN ('confirmed','reverted')) <> 1)
    OR (p.status='cancelled' AND
        EXISTS (SELECT 1 FROM execution_attempts a WHERE a.plan_id=p.plan_id))
)
"""


class UnresolvedExecutionPlans(ValueError):
    pass


def check_execution_history():
    """SELECT only; never instantiate a Store or repair historical records."""
    connection = mysql_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(UNRESOLVED_PLANS_SQL, (FOLLOWER,))
            count = cursor.fetchone()["n"]
            if count:
                raise UnresolvedExecutionPlans("unresolved execution plans require review before approval")
            return {"unresolved_execution_plans": count}
    finally:
        connection.close()


def approval_data():
    return "0x" + (keccak(text="approve(address,uint256)")[:4]
                   + encode(["address", "uint256"], [SPENDER, AMOUNT])).hex()


async def inspect(policy, rpc, *, fixed_transaction=None):
    if (policy.run_mode != "mainnet_live" or policy.follower_wallet != FOLLOWER
            or USDG not in policy.allowed_assets or not policy.relationship_id):
        raise ValueError("relationship does not match fixed approval scope")
    if number(await rpc.call("eth_chainId")) != CHAIN_ID:
        raise ValueError("wrong chain")
    for target in (USDG, SPENDER):
        code = await rpc.call("eth_getCode", [target, "pending"])
        if not isinstance(code, str) or code in {"0x", "0x0", "0x00"}:
            raise ValueError("token or spender contract missing")
    decimals = number(await rpc.call("eth_call", [{"to": USDG, "data": "0x313ce567"}, "pending"]))
    if decimals != 6:
        raise ValueError("USDG decimals mismatch")
    allowance = number(await rpc.call("eth_call", [{"to": USDG,
        "data": _allowance_data(FOLLOWER, SPENDER)}, "pending"]))
    evidence = {"chain_id": CHAIN_ID, "follower": FOLLOWER, "token": USDG,
                "spender": SPENDER, "amount_raw": str(AMOUNT),
                "allowance_raw": str(allowance), "private_key_read": False,
                "broadcast_performed": False}
    if allowance >= AMOUNT:
        return None, {**evidence, "status": "already_sufficient"}
    # Explicitly approved upgrade from the previous 0.1 USDG trial allowance.
    # This sets the TOTAL to 10 USDG; it does not increment by 10.
    if allowance not in {0, 100000}:
        raise ValueError("nonzero partial allowance requires operator review")
    pending = number(await rpc.call("eth_getTransactionCount", [FOLLOWER, "pending"]))
    latest = number(await rpc.call("eth_getTransactionCount", [FOLLOWER, "latest"]))
    if pending != latest:
        raise ValueError("wallet has pending transactions")
    gas_price = number(await rpc.call("eth_gasPrice"))
    # Revalidate the ORIGINAL fee cap, not a newly suggested +20% cap. Normal
    # gas-price movement must not mutate a transaction awaiting/signature review.
    max_fee = ((gas_price * 12 + 9) // 10 if fixed_transaction is None
               else fixed_transaction.get("maxFeePerGas"))
    if type(max_fee) is not int or max_fee <= 0 or gas_price <= 0 or max_fee < gas_price:
        raise ValueError("original approval fee cap is below current gas requirement")
    gas = 100000
    if max_fee <= 0 or gas * max_fee > min(int(policy.quote_policy.max_gas_cost_wei), 10**15):
        raise ValueError("approval gas exceeds limit")
    if number(await rpc.call("eth_getBalance", [FOLLOWER, "pending"])) < gas * max_fee:
        raise ValueError("insufficient gas balance")
    transaction = {"chainId": CHAIN_ID, "nonce": pending, "to": to_checksum_address(USDG),
                   "value": 0, "data": approval_data(), "gas": gas,
                   "maxFeePerGas": max_fee, "maxPriorityFeePerGas": 0, "type": 2}
    if fixed_transaction is not None and transaction != fixed_transaction:
        raise ValueError("approval identity or nonce changed; no broadcast")
    result = await rpc.call("eth_call", [{"from": FOLLOWER, "to": USDG,
        "data": transaction["data"], "value": "0x0", "gas": hex(gas)}, "pending"])
    if number(result) != 1:
        raise ValueError("approval simulation failed")
    return transaction, {**evidence, "status": "ready", "nonce": pending,
                         "maximum_gas_cost_wei": str(gas * max_fee)}


def validate_signature(raw, transaction):
    if Account.recover_transaction(raw).lower() != FOLLOWER:
        raise ValueError("signed sender mismatch")
    decoded = TypedTransaction.from_bytes(HexBytes(raw)).as_dict()
    for field, expected in transaction.items():
        actual = decoded[field]
        if field in {"to", "data"}:
            if bytes(actual) != bytes.fromhex(expected[2:]):
                raise ValueError("signed payload mismatch")
        elif actual != expected:
            raise ValueError("signed transaction mismatch")


def record(stream, event):
    stream.write(json.dumps(event, sort_keys=True) + "\n")
    stream.flush()
    os.fsync(stream.fileno())


async def execute(policy, rpc, broadcaster, *, journal=JOURNAL, signer_factory=LiveDatabaseSigner):
    """Caller must hold runtime_instance_lock; no retry or replacement exists."""
    identity = (FOLLOWER, policy.relationship_id, policy.snapshot_hash)
    require_mainnet_signing_enabled(*identity)
    transaction, evidence = await inspect(policy, rpc)
    if transaction is None:
        return evidence
    journal.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive durable marker BEFORE key access. Any interrupted attempt blocks rerun.
    fd = os.open(journal, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as stream:
        record(stream, {**evidence, "status": "signing_started", "transaction": transaction})
        require_mainnet_signing_enabled(*identity)
        # Recheck public state immediately before accessing the key source.
        final_tx, _ = await inspect(policy, rpc, fixed_transaction=transaction)
        if final_tx != transaction:
            raise ValueError("approval state changed; inspect journal before retry")
        raw = signer_factory(*identity).sign_transaction(transaction)
        validate_signature(raw, transaction)
        tx_hash = "0x" + keccak(raw).hex()
        record(stream, {"status": "signed_not_yet_submitted", "tx_hash": tx_hash})
        # A slow key DB or an external wallet must not turn this into a replacement.
        final_tx, _ = await inspect(policy, rpc, fixed_transaction=transaction)
        if final_tx != transaction:
            raise ValueError("approval state changed after signing; no broadcast")
        review = ReadOnlyBroadcastReview("zeroex-usdg-approval-10000000", tx_hash, time.time(),
                                       {"broadcast_performed": False})
        record(stream, {"status": "broadcast_attempt", "tx_hash": tx_hash})
        await broadcaster.broadcast(review, raw, follower_wallet=FOLLOWER,
            relationship_id=policy.relationship_id, config_snapshot_hash=policy.snapshot_hash)
        del raw
        record(stream, {"status": "submitted", "tx_hash": tx_hash})
        receipt = await confirm_relationship_token_approval(rpc,
            ApprovalResult(tx_hash, USDG, SPENDER, str(AMOUNT), evidence["allowance_raw"], True), FOLLOWER)
        result = {"status": "confirmed", "tx_hash": tx_hash, "receipt": receipt,
                  "private_key_read": True, "broadcast_performed": True}
        record(stream, result)
        return result


async def run(args):
    load_endpoint_env()
    policy = load_enabled_relationship_policy(args.relationship)
    endpoint = os.environ.get("ROBINHOOD_RPC_URL")
    if not endpoint:
        raise ValueError("ROBINHOOD_RPC_URL missing")
    rpc = ReadOnlyRpc(endpoint)
    if not args.execute:
        _, result = await inspect(policy, rpc)
        return result
    # Same lock as sm-copy run; does not overwrite the monitor PID file.
    with runtime_instance_lock(pid_path="var/zeroex-approval.pid"):
        check_execution_history()
        return await execute(policy, rpc, MainnetBroadcaster(endpoint))


def main():
    parser = argparse.ArgumentParser(description="One-shot approval: total 100 USDG to 0x on Robinhood. Default: check only.")
    parser.add_argument("--relationship", required=True)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-approve-100-usdg", action="store_true", dest="confirm")
    args = parser.parse_args()
    if args.execute != args.confirm:
        parser.error("execution requires both --execute and --confirm-approve-100-usdg")
    try:
        print(json.dumps(asyncio.run(run(args)), sort_keys=True))
    except Exception as exc:
        # Never echo DB/provider exception text or signed bytes into the terminal.
        print(json.dumps({"status": "stopped", "error_type": type(exc).__name__,
                          "reason": "unresolved_execution_plans" if isinstance(exc, UnresolvedExecutionPlans) else "check_failed",
                          "action": "Check monitor lock, live gates, public preflight and journal; do not blindly retry."}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
