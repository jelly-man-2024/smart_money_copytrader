#!/usr/bin/env python3
"""One-shot real-RPC validation of the local paper pipeline; never broadcasts."""
from __future__ import annotations

import asyncio
import json
import os

from smart_money import registry as R
from smart_money.config import load_endpoint_env
from smart_money.models import Signal, number
from smart_money.paper import AmountRule, PaperEngine, PaperExecutor, PaperValuator, signal_route_key
from smart_money.quotes import LiveQuoter, QuotePolicy
from smart_money.rpc import ReadOnlyRpc
from smart_money.store import Store


async def main() -> None:
    load_endpoint_env()
    rpc = ReadOnlyRpc(os.environ.get(
        "ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    if number(await rpc.call("eth_chainId")) != R.CHAIN_ID:
        raise ValueError("RPC is connected to the wrong chain")
    wallet = "0x0000000000000000000000000000000000000001"
    amount = "1000000000000000"
    signal = Signal(
        "0x" + "12" * 32, wallet, "validation", "BUY", "validation/v3",
        R.V3_ROUTER, "0x", stage="swap_evidenced", execution_status="success",
        canonical_status="safe", token_in=R.WETH, token_out=R.USDG,
        protocol="v3", exact_in=True,
        evidence={"hops": [{"token_in": R.WETH, "token_out": R.USDG, "fee": 500}]},
    )
    quoter = LiveQuoter(rpc)
    source_quote = await quoter.quote_exact_input(signal, amount)
    signal.evidence.update({
        "actual_input_debit_raw": amount,
        "actual_output_credit_raw": source_quote.amount_out_raw,
        "validation_source": "same_run_block_pinned_read_only_quote",
    })
    policy = QuotePolicy(max_adverse_deviation_bps=1000, max_price_impact_bps=1000,
                         max_slippage_bps=500,
                         max_gas_cost_wei="100000000000000000")
    store = Store(":memory:")
    try:
        store.put(signal)
        store.start_paper_budget_cycle("readonly-validation", "one_shot_validation")
        store.configure_paper_budget(wallet, "ETH_WETH", "10000000000000000")
        route = signal_route_key(signal)
        engine = PaperEngine(
            store, quoter, policy, "readonly-validation-v1", "swap_evidenced",
            frozenset({"v3"}), frozenset({R.WETH, R.USDG}), frozenset({route}),
        )
        decision = await engine.propose_buy(
            signal, AmountRule("fixed", fixed_amount_raw=amount))
        if not decision.accepted or not decision.proposal_id:
            raise ValueError(f"paper decision rejected: {decision.reason}")
        execution = await PaperExecutor(store, quoter, policy).execute(
            signal, decision.proposal_id)
        if execution.status != "filled":
            raise ValueError(f"paper execution not filled: {execution.reason}")
        lot_id = PaperExecutor._id(decision.proposal_id, "lot")
        mark = await PaperValuator(store, quoter, policy).mark(lot_id, signal)
        trade = store.paper_trades()[0]
        print(json.dumps({
            "event": "paper_readonly_validation_finished",
            "live_trading": False,
            "source_quote_block": source_quote.block_number,
            "source_quote_source": source_quote.source,
            "decision_id": decision.decision_id,
            "proposal_id": decision.proposal_id,
            "fill_id": execution.fill_id,
            "fill_amount_in_raw": trade["amount_in_raw"],
            "fill_amount_out_raw": trade["amount_out_raw"],
            "fill_gas_cost_wei": trade["gas_cost_wei"],
            "mark_block": mark.block_number,
            "mark_gross_value_raw": mark.gross_value_raw,
            "mark_unrealized_pnl_raw": mark.unrealized_pnl_raw,
            "mark_gas_cost_wei": mark.gas_cost_wei,
            "copy_eligible": signal.copy_eligible,
        }, sort_keys=True))
    finally:
        store.close()


if __name__ == "__main__":
    asyncio.run(main())
