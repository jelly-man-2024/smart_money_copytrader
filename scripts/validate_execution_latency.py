#!/usr/bin/env python3
"""Bounded public quote/eth_call comparison. No store, signer or broadcast calls.

Requires explicit public recipient and output token. Does not buy, approve, read
wallet keys, change budgets, or claim historical pending-state reproducibility.
"""
import argparse
import asyncio
from dataclasses import replace
import json
import os
import time

from smart_money.config import load_endpoint_env
from smart_money.execution_prep import build_aggregator_execution_plan, ReadOnlyExecutionPreflight, simulate_aggregator_execution
from smart_money.kyber import KyberAggregatorClient
from smart_money.models import Signal, address
from smart_money.quotes import LiveQuoter, QuotePolicy, assess_market_quote
from smart_money.registry import USDG, CHAIN_ID
from smart_money.rpc import ReadOnlyRpc
from smart_money.zeroex import ZeroExAggregatorClient


async def run(args):
    load_endpoint_env()
    rpc = ReadOnlyRpc(os.environ['ROBINHOOD_RPC_URL'])
    clients = {'zeroex': ZeroExAggregatorClient(os.environ.get('0X_API_KEY')),
               'kyber': KyberAggregatorClient()}
    quoter = LiveQuoter(rpc, clients)
    policy = QuotePolicy(max_age_seconds=6, max_slippage_bps=300)
    try:
        if int(await rpc.call('eth_chainId'), 16) != CHAIN_ID:
            raise ValueError('wrong public RPC chain')
        for iteration in range(args.rounds):
            built = []
            providers = ['zeroex', 'kyber'] if iteration % 2 == 0 else ['kyber', 'zeroex']
            for provider in providers:
                started = time.monotonic()
                row = dict(round=iteration+1, provider=provider, broadcast_performed=False,
                           private_key_read=False, amount_in_raw=args.amount, status='failed')
                signal = Signal('0x'+'00'*32, args.follower, 'direct', 'BUY', 'readonly-probe',
                    None, '', token_in=USDG, token_out=args.token, protocol=provider,
                    exact_in=True, stage='swap_evidenced', execution_status='success')
                # Synthetic evidenced signal is local to the probe, never stored or
                # handed to an execution engine. Only market guards are evaluated.
                try:
                    with quoter.execution_context(f'probe-{iteration}-{provider}', args.follower, 'readonly', 6):
                        quote, reference, gas = await quoter.quote_with_reference(signal, args.amount)
                        swap = await quoter.build_aggregator_transaction(signal, args.amount, args.follower, 300,
                                                                        int(time.time())+120)
                    quote = replace(quote, amount_out_raw=swap.amount_out_raw, gas_estimate_raw=str(swap.gas_estimate))
                    accepted, reason, risk = assess_market_quote(quote, reference, policy, gas)
                    if not accepted: raise ValueError(reason)
                    plan = build_aggregator_execution_plan(signal, args.follower, 'readonly', 'readonly', quote,
                        risk['minimum_amount_out_raw'], swap, max(600000, swap.gas_estimate*13//10+50000),
                        str((int(gas)*12+9)//10), '0', {'0x', 'kyber'}, {USDG}, ())
                    row.update(status='built', quote_build_ms=round((time.monotonic()-started)*1000, 3),
                               minimum_out_raw=swap.minimum_amount_out_raw, amount_out_raw=swap.amount_out_raw,
                               gas_limit=plan.gas_limit)
                    built.append((row, plan))
                except Exception as exc:
                    row.update(error_type=type(exc).__name__, reason=str(exc)[:160],
                               quote_build_ms=round((time.monotonic()-started)*1000, 3))
                    print(json.dumps(row), flush=True)
            header = await rpc.call('eth_getBlockByNumber', ['latest', False])
            class Pinned:
                async def call(self, method, params=None):
                    params = list(params or [])
                    if params and params[-1] == 'pending': params[-1] = header['number']
                    return await rpc.call(method, params)
            for row, plan in built:
                row.update(simulation_block=header['number'], simulation_block_hash=header['hash'])
                try:
                    preflight = await ReadOnlyExecutionPreflight(Pinned(), frozenset({plan.to}),
                        policy.max_gas_cost_wei, max_quote_age_seconds=6).check(plan)
                    result = await simulate_aggregator_execution(Pinned(), plan)
                    row.update(status='simulation_passed', preflight_ms=preflight['preflight_ms'],
                               simulation_ms=result['simulation_ms'])
                except Exception as exc:
                    row.update(status='simulation_or_preflight_failed', error_type=type(exc).__name__, reason=str(exc)[:160])
                print(json.dumps(row), flush=True)
    finally:
        rpc.close()
        for client in clients.values(): client.close()


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--follower', required=True, type=address)
    parser.add_argument('--token', required=True, type=address)
    parser.add_argument('--amount', default='100000')
    parser.add_argument('--rounds', type=int, default=1, choices=range(1, 6))
    args = parser.parse_args()
    if not args.amount.isdecimal() or not 100 <= int(args.amount) <= 100000:
        parser.error('amount must be 100..100000 raw USDG')
    try:
        asyncio.run(run(args))
    except Exception as exc:
        print(json.dumps(dict(status='stopped', error_type=type(exc).__name__, broadcast_performed=False,
                              private_key_read=False)))
