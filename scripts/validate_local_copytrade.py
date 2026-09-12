#!/usr/bin/env python3
"""Anvil-only two-wallet BUY/SELL copy-trade integration test.

This deliberately uses Anvil impersonation instead of handling private keys.  Every
mutating RPC call is guarded by loopback URL, chain id 31337 and Anvil client identity.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import secrets
import sys
import time
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from eth_abi import encode
from eth_utils import keccak, to_checksum_address

from smart_money.models import Signal
from smart_money.paper import AmountRule, PaperEngine, PaperExecutor
from smart_money.quotes import Quote, QuotePolicy
from smart_money.store import Store


RPC_URL = "http://127.0.0.1:8545"
LOCAL_CHAIN_ID = 31337
ZERO = "0x" + "00" * 20
ROOT = Path(__file__).resolve().parents[1]


class LocalRpc:
    def __init__(self, url: str):
        parsed = urlparse(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("local test RPC must be loopback HTTP")
        self.url = url
        self.request_id = 0

    def call(self, method: str, params: list | None = None):
        self.request_id += 1
        body = json.dumps({"jsonrpc": "2.0", "id": self.request_id,
                           "method": method, "params": params or []}).encode()
        with urlopen(Request(self.url, data=body,
                             headers={"Content-Type": "application/json"}), timeout=5) as response:
            payload = json.load(response)
        if payload.get("error"):
            raise RuntimeError(f"local RPC {method} failed: {payload['error'].get('message')}")
        return payload["result"]

    def assert_anvil(self) -> None:
        if int(self.call("eth_chainId"), 16) != LOCAL_CHAIN_ID:
            raise RuntimeError("refusing non-local-test chain id")
        if "anvil" not in self.call("web3_clientVersion").lower():
            raise RuntimeError("refusing non-Anvil RPC")


def selector(signature: str) -> bytes:
    return keccak(text=signature)[:4]


def artifact(name: str) -> dict:
    path = ROOT / "var/local-chain-artifacts/LocalCopyTrade.sol" / f"{name}.json"
    return json.loads(path.read_text())


def wait_receipt(rpc: LocalRpc, tx_hash: str) -> dict:
    for _ in range(100):
        receipt = rpc.call("eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            if int(receipt["status"], 16) != 1:
                raise RuntimeError(f"local transaction reverted: {tx_hash}")
            return receipt
        time.sleep(0.05)
    raise TimeoutError(f"local receipt unavailable: {tx_hash}")


def send(rpc: LocalRpc, sender: str, *, to: str | None = None,
         data: str = "0x", value: int = 0) -> dict:
    tx = {"from": sender, "data": data, "value": hex(value)}
    if to is not None:
        tx["to"] = to
    return wait_receipt(rpc, rpc.call("eth_sendTransaction", [tx]))


def deploy(rpc: LocalRpc, sender: str, name: str, args: bytes = b"", value: int = 0) -> str:
    bytecode = artifact(name)["bytecode"]["object"]
    bytecode = bytecode[2:] if bytecode.startswith("0x") else bytecode
    receipt = send(rpc, sender, data="0x" + bytecode + args.hex(), value=value)
    return receipt["contractAddress"].lower()


def random_address() -> str:
    return to_checksum_address("0x" + secrets.token_hex(20)).lower()


def balance_of(rpc: LocalRpc, token: str, wallet: str) -> int:
    data = "0x" + (selector("balanceOf(address)") + encode(["address"], [wallet])).hex()
    return int(rpc.call("eth_call", [{"to": token, "data": data}, "latest"]), 16)


def swap_from_receipt(receipt: dict, pool: str, trader: str) -> dict:
    topic = "0x" + keccak(text="Swap(address,address,address,uint256,uint256)").hex()
    matches = [log for log in receipt["logs"]
               if log["address"].lower() == pool and log["topics"][0].lower() == topic]
    if len(matches) != 1:
        raise RuntimeError("expected exactly one local pool Swap event")
    log = matches[0]
    indexed_trader = "0x" + log["topics"][1][-40:]
    if indexed_trader.lower() != trader:
        raise RuntimeError("Swap trader attribution mismatch")
    token_in = "0x" + log["topics"][2][-40:]
    token_out = "0x" + log["topics"][3][-40:]
    amount_in, amount_out = [int(value) for value in
                             (int(log["data"][2:66], 16), int(log["data"][66:130], 16))]
    return {"token_in": token_in, "token_out": token_out,
            "amount_in_raw": str(amount_in), "amount_out_raw": str(amount_out)}


def evidenced_signal(receipt: dict, swap: dict, wallet: str, pool: str,
                     behavior: str) -> Signal:
    return Signal(
        receipt["transactionHash"].lower(), wallet, "direct", behavior,
        "local_testfixed_rate_pool", pool, "local-test", stage="swap_evidenced",
        execution_status="success", canonical_status="local_canonical",
        token_in=swap["token_in"], token_out=swap["token_out"],
        amount_in_raw=swap["amount_in_raw"], amount_out_raw=swap["amount_out_raw"],
        recipient=wallet, protocol="v2", exact_in=True, execution_success=True,
        fresh=True, evidence={
            "actual_input_debit_raw": swap["amount_in_raw"],
            "actual_output_credit_raw": swap["amount_out_raw"],
            "route": [swap["token_in"], swap["token_out"]],
            "local_test_pool": pool,
            "receipt_block_number": str(int(receipt["blockNumber"], 16)),
            "receipt_block_hash": receipt["blockHash"].lower(),
        }, chain_id=LOCAL_CHAIN_ID,
    )


class LocalFixedRateQuoter:
    def __init__(self, rpc: LocalRpc, pool: str):
        self.rpc, self.pool = rpc, pool

    async def quote_with_reference(self, signal: Signal, amount: str):
        header = self.rpc.call("eth_getBlockByNumber", ["latest", False])
        amount_int = int(amount)
        output = (amount_int * 1000 if signal.token_in == ZERO
                  else amount_int // 1000)
        reference_in = max(1, amount_int // 100)
        reference_out = (reference_in * 1000 if signal.token_in == ZERO
                         else reference_in // 1000)
        if output <= 0 or reference_out <= 0:
            raise ValueError("local quote rounds to zero")
        now = time.time()
        common = ("v2", self.pool, int(header["number"], 16),
                  header["hash"].lower(), now, signal.token_in, signal.token_out)
        return (Quote(*common, amount, str(output), "50000"),
                Quote(*common, str(reference_in), str(reference_out), "50000"), "1")


def fill_payload(prefix: str, proposal_id: str, amount_out: str,
                 fee_asset: str, *, buy: bool) -> dict:
    stamp = datetime.now(timezone.utc).isoformat()
    result = {
        "order_id": f"{prefix}-order-{proposal_id}",
        "fill_id": f"{prefix}-fill-{proposal_id}",
        "amount_out_raw": amount_out, "fee_asset": fee_asset,
        "fee_amount_raw": "0", "gas_cost_wei": "0",
        "quote_observed_at": stamp, "filled_at": stamp,
    }
    if buy:
        result["lot_id"] = f"local-lot-{proposal_id}"
    return result


async def run() -> dict:
    rpc = LocalRpc(RPC_URL)
    rpc.assert_anvil()
    deployer = rpc.call("eth_accounts")[0].lower()
    smart_wallet, follower_wallet = random_address(), random_address()
    for wallet in (smart_wallet, follower_wallet):
        rpc.call("anvil_setBalance", [wallet, hex(10**19)])
        rpc.call("anvil_impersonateAccount", [wallet])

    token = deploy(rpc, deployer, "LocalUSDG", encode(["uint256"], [10**27]))
    pool = deploy(rpc, deployer, "LocalFixedRatePool",
                  encode(["address"], [token]), value=10**20)
    liquidity = 5 * 10**26
    send(rpc, deployer, to=token, data="0x" + (
        selector("transfer(address,uint256)") + encode(["address", "uint256"],
                                                        [pool, liquidity])).hex())

    smart_buy = send(rpc, smart_wallet, to=pool,
                     data="0x" + selector("buy()").hex(), value=10**17)
    buy_signal = swap_from_receipt(smart_buy, pool, smart_wallet)
    buy_source = evidenced_signal(smart_buy, buy_signal, smart_wallet, pool, "BUY")

    ledger_path = ROOT / "var" / f"local-copytrade-{secrets.token_hex(8)}.sqlite3"
    store = Store(ledger_path)
    store.start_paper_budget_cycle("local-test-cycle", "local_anvil_integration")
    store.configure_paper_budget(follower_wallet, "ETH_WETH", str(10**18))
    context = {smart_wallet: {
        "follower_wallet": follower_wallet,
        "relationship_id": "local-test-relationship",
        "ledger_scope": follower_wallet,
    }}
    quoter = LocalFixedRateQuoter(rpc, pool)
    policy = QuotePolicy(max_age_seconds=10, max_adverse_deviation_bps=0,
                         max_price_impact_bps=0, max_slippage_bps=100,
                         max_gas_cost_wei=str(10**18))
    engine = PaperEngine(
        store, quoter, policy, "local-test-v1", wallet_contexts=context,
        wallet_labels={smart_wallet: "local-smart-wallet"},
        config_snapshot_hash=keccak(text="local-test-v1").hex(),
        allowed_protocols=frozenset({"v2"}),
        allowed_assets=frozenset({ZERO, token}),
    )
    store.put(buy_source)
    buy_decision = await engine.propose_buy(
        buy_source, AmountRule("proportional", ratio_ppm=500_000))
    if not buy_decision.accepted or buy_decision.proposal_id is None:
        raise RuntimeError(f"local BUY proposal rejected: {buy_decision.reason}")
    follower_buy_amount = int(store.paper_proposal(
        buy_decision.proposal_id)["amount_in_raw"])
    follower_buy = send(rpc, follower_wallet, to=pool,
                        data="0x" + selector("buy()").hex(), value=follower_buy_amount)
    follower_buy_fill = swap_from_receipt(follower_buy, pool, follower_wallet)
    if not store.fill_paper_buy(buy_decision.proposal_id, fill_payload(
            "buy", buy_decision.proposal_id, follower_buy_fill["amount_out_raw"],
            ZERO, buy=True)):
        raise RuntimeError("local BUY fill was not persisted")

    smart_sell_amount = int(buy_signal["amount_out_raw"]) // 2
    approve_data = selector("approve(address,uint256)") + encode(
        ["address", "uint256"], [pool, smart_sell_amount])
    send(rpc, smart_wallet, to=token, data="0x" + approve_data.hex())
    smart_sell = send(rpc, smart_wallet, to=pool, data="0x" + (
        selector("sell(uint256)") + encode(["uint256"], [smart_sell_amount])).hex())
    sell_signal = swap_from_receipt(smart_sell, pool, smart_wallet)
    sell_source = evidenced_signal(smart_sell, sell_signal, smart_wallet, pool, "SELL")
    store.put(sell_source)
    sell_decision = await engine.propose_sell(
        sell_source, AmountRule("proportional", ratio_ppm=500_000))
    if not sell_decision.accepted or sell_decision.proposal_id is None:
        raise RuntimeError(f"local SELL proposal rejected: {sell_decision.reason}")

    follower_sell_amount = int(store.paper_proposal(
        sell_decision.proposal_id)["amount_in_raw"])
    follower_approve = selector("approve(address,uint256)") + encode(
        ["address", "uint256"], [pool, follower_sell_amount])
    send(rpc, follower_wallet, to=token, data="0x" + follower_approve.hex())
    follower_sell = send(rpc, follower_wallet, to=pool, data="0x" + (
        selector("sell(uint256)") + encode(["uint256"], [follower_sell_amount])).hex())
    follower_sell_fill = swap_from_receipt(follower_sell, pool, follower_wallet)
    if not store.fill_paper_sell(sell_decision.proposal_id, fill_payload(
            "sell", sell_decision.proposal_id, follower_sell_fill["amount_out_raw"],
            ZERO, buy=False)):
        raise RuntimeError("local SELL fill was not persisted")

    if (int(follower_buy_fill["amount_in_raw"]) * 2 != int(buy_signal["amount_in_raw"])
            or int(follower_buy_fill["amount_out_raw"]) * 2
            != int(buy_signal["amount_out_raw"])
            or int(follower_sell_fill["amount_in_raw"]) * 2
            != int(sell_signal["amount_in_raw"])
            or int(follower_sell_fill["amount_out_raw"]) * 2
            != int(sell_signal["amount_out_raw"])):
        raise RuntimeError("50 percent copy strategy was not preserved")

    trades = store.paper_trades()
    budget = store.paper_budget(follower_wallet, "ETH_WETH")
    if len(trades) != 2 or [trade["side"] for trade in trades] != ["BUY", "SELL"]:
        raise RuntimeError("attributed BUY/SELL ledger is incomplete")
    if budget["invested_raw"] != str(25 * 10**15):
        raise RuntimeError("sell did not restore attributed principal budget")
    result = {
        "environment": "anvil_local_only", "chain_id": LOCAL_CHAIN_ID,
        "private_keys_used_by_project": False, "mainnet_rpc_used": False,
        "smart_wallet": smart_wallet, "follower_wallet": follower_wallet,
        "token": token, "pool": pool, "strategy": "50_percent",
        "smart_buy": {"tx_hash": smart_buy["transactionHash"], **buy_signal},
        "follower_buy": {"tx_hash": follower_buy["transactionHash"], **follower_buy_fill},
        "smart_sell": {"tx_hash": smart_sell["transactionHash"], **sell_signal},
        "follower_sell": {"tx_hash": follower_sell["transactionHash"], **follower_sell_fill},
        "final_token_balances": {
            "smart_wallet": str(balance_of(rpc, token, smart_wallet)),
            "follower_wallet": str(balance_of(rpc, token, follower_wallet)),
        },
        "ledger": {
            "path": str(ledger_path), "paper_trades": len(trades),
            "sides": [trade["side"] for trade in trades],
            "relationship_id": trades[0]["attribution"]["relationship_id"],
            "source_chain_ids": [
                trade["source_signal_at_decision"]["chain_id"] for trade in trades],
            "budget": budget,
            "realized_pnl_raw": store.paper_realized_pnl(
                trades[1]["fill_id"])[0]["realized_pnl_raw"],
        },
        "copy_eligible": False, "live_trading": False,
    }
    store.close()
    return result


def main() -> int:
    print(json.dumps(asyncio.run(run()), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
