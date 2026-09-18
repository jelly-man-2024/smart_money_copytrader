from __future__ import annotations

import argparse
import asyncio
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import sys
import time

import websockets

from dataclasses import replace

from .account_state import prestate_implementations
from .arc_observer import observe_arc
from .arc_pool_safety import ArcPoolSafetyPolicy, verify_arc_exit_via_aggregator
from .approval import (
    approve_relationship_token, approve_relationship_usdg,
    confirm_relationship_token_approval,
)
from .backfill import MAX_RANGE_BLOCKS, BlockScanner, ReorgDetected, relevant

# Tracking polls once a second, so this waits about a minute before deciding a
# broadcast the node accepted is never going to be mined.
DROPPED_BROADCAST_ATTEMPTS = 60

# How much of an attributable position to approve for its eventual exit. A
# bounded multiple keeps the standing allowance in proportion to what the
# relationship actually holds while letting several buys share one approval.
EXIT_ALLOWANCE_MULTIPLE = 10
from .broadcast import MainnetBroadcaster
from .zeroex import ZeroExAggregatorClient, ZeroExApiError
from .config import load_endpoint_env
from .decode import Decoder
from .feed import DEFAULT_FEED_MAX_AGE_SECONDS, DecodeError, FeedHealth, decode_raw, envelopes
from .execution_receipts import ReadOnlyExecutionTracker
from .execution_controls import require_mainnet_broadcast_enabled
from .execution_pipeline import (
    ExecutionPreparer, LiveExecutionSigner, LivePreBroadcastReviewer,
)
from .key_source import key_record_status, live_key_record_status
from .live_settlement import settle_confirmed_execution
from .ledger_migration import migrate_sqlite_ledger
from .models import Transaction, number
from .mysql_config import (
    MySqlRelationshipGate, import_watchlist_relationships,
    load_enabled_mainnet_acceptance, load_enabled_relationship_policy,
    load_mysql_paper_config,
)
from .mysql_store import MySqlStore
from .native_flows import verify_native_flows
from .kyber import KyberAggregatorClient
from .paper import (
    AGGREGATOR_PROVIDERS, AGGREGATOR_ROUTERS, PaperEngine, PaperExecutor,
    aggregator_routers,
    PaperValuator, budget_bucket, execution_quote_signal, scope_reason,
)
from .paper_config import load_paper_config
from .pools import discover_v3_execution_route, verify_signal_pools
from .quotes import LiveQuoter
from .receipts import direct_token_transfer_evidence, enrich
from .registry import (
    ARC, CHAIN_ID, ENTRYPOINT, NATIVE, USDG, V2_ROUTER, V3_ROUTER, chain_for,
    delegation, load_watchlist, snapshot_delegations,
)
from .rpc import ReadOnlyRpc, RpcError, CancelledBeforeSigningRpcError
from .relay_api import RelayApiError, RelayNotReady, RelayPublicClient
from .solver import relay_confirmed_sell, relay_passive_buy
from .store import Store
from .early_feed_lane import EarlyFeedLane, EarlyEvidenceResolver
from .early_runtime import EarlyRuntime
from .deployment_monitor import DeploymentMonitor
from .early_timing import EARLY_FEED_MAX_AGE_SECONDS
from .execution_pipeline import check_early_execution_source
from .runtime_safety import runtime_instance_lock, trip_execution_stop, execution_health
from .simulation_diagnostics import AggregatorSimulationError


def report(event: str, **details):
    print(json.dumps({"event": event, "observed_at": time.time(), **details}, ensure_ascii=False), file=sys.stderr, flush=True)


def emit(store, signal):
    if store.put(signal):
        print(json.dumps(signal.to_dict(), ensure_ascii=False), flush=True)
        if signal.stage in {"swap_evidenced", "relay_buy_evidenced", "relay_sell_evidenced", "failed"}:
            report("source_evidence_available", source_event_id=signal.event_id,
                   source_tx_hash=signal.tx_hash, smart_wallet=signal.wallet,
                   stage=signal.stage, order_id=signal.evidence.get("relay_order_id",
                       signal.evidence.get("relay_deposit_order_id")))


async def arc_monitor(args):
    """Run the independent Arc observer; no paper/live execution is wired here."""
    load_endpoint_env()
    rpc_url = os.environ.get("ARC_RPC_URL")
    ws_url = os.environ.get("ARC_WS_URL")
    if not rpc_url or not ws_url:
        raise ValueError("ARC_RPC_URL and ARC_WS_URL are required")
    rpc = ReadOnlyRpc(rpc_url)
    store = Store(args.db)
    watchlist = load_watchlist(args.watchlist)
    # Relay is deployed on Arc with the same code as Robinhood Chain and its API
    # serves chain 5042, so a cross-chain fill can be attributed here too. Without
    # this the delivery reads as a plain receipt of tokens and is never a buy.
    relay_client = None if args.no_relay else RelayPublicClient()

    def output(signal):
        print(json.dumps(signal.to_dict(), ensure_ascii=False), flush=True)
        report("arc_signal_observed", source_event_id=signal.event_id,
               source_tx_hash=signal.tx_hash, smart_wallet=signal.wallet,
               stage=signal.stage, chain_id=signal.chain_id,
               read_only=True, live_trading=False)

    def status(event, details):
        report(event, **details, chain_id=ARC.chain_id,
               read_only=True, live_trading=False)

    report("arc_observer_started", chain_id=ARC.chain_id,
           smart_wallets=len(watchlist), read_only=True, live_trading=False)
    try:
        task = observe_arc(
            rpc, ws_url, store, watchlist, output, status,
            backfill_interval=args.backfill_interval,
            backfill_batch=args.backfill_batch,
            relay_client=relay_client)
        if args.seconds:
            try:
                await asyncio.wait_for(task, timeout=args.seconds)
            except asyncio.TimeoutError:
                report("arc_observer_duration_complete", seconds=args.seconds)
        else:
            await task
    finally:
        rpc.close()
        store.close()


async def arc_paper(args):
    """Arc (5042) paper lane: observe -> exit gate -> decide -> paper fill.

    This lane has no signing or broadcasting surface. It builds a PaperExecutor
    and never a live pipeline, and it refuses to start while any Arc
    relationship is not in paper mode, so a configuration slip cannot quietly
    turn it into a live one. Policies are selected by wallet AND chain, because
    the same smart wallet is also copied live on Robinhood Chain.
    """
    load_endpoint_env()
    rpc_url = os.environ.get("ARC_RPC_URL")
    ws_url = os.environ.get("ARC_WS_URL")
    if not rpc_url or not ws_url:
        raise ValueError("ARC_RPC_URL and ARC_WS_URL are required")
    api_key = os.environ.get("0X_API_KEY")
    if not api_key:
        raise ValueError("0X_API_KEY is required: Arc executes through 0x")
    paper_config = load_mysql_paper_config()
    policies = tuple(policy for policy in paper_config.relationships
                     if policy.chain_id == ARC.chain_id)
    if not policies:
        raise ValueError("no enabled relationship copies Arc")
    not_paper = sorted(policy.relationship_id for policy in policies
                       if policy.run_mode != "paper")
    if not_paper:
        raise ValueError(
            f"Arc lane cannot execute; relationships {not_paper} are not paper mode")
    arc_config = replace(paper_config, relationships=policies)
    safety = ArcPoolSafetyPolicy(
        max_tax_bps=args.max_tax_bps,
        max_round_trip_loss_bps=args.max_round_trip_loss_bps,
        probe_amount_raw=args.probe_amount_raw)

    store = MySqlStore()
    rpc = ReadOnlyRpc(rpc_url)
    zeroex = ZeroExAggregatorClient(api_key)
    quoter = LiveQuoter(rpc, {"zeroex": zeroex})
    relay_client = None if args.no_relay else RelayPublicClient()
    stats = Counter()
    engines, executors = {}, {}
    cycle_id, cycle_created = prepare_runtime_budget_cycle(
        store, arc_config, args.paper_cycle_action,
        args.paper_cycle_id, args.paper_cycle_reason)
    for policy in policies:
        executors[policy.ledger_scope] = PaperExecutor(
            store, quoter, policy.quote_policy, policy.route_definitions)
        for mode in (policy.trigger_mode, *policy.shadow_trigger_modes):
            engines[(mode, policy.ledger_scope)] = PaperEngine(
                store, quoter, policy.quote_policy, policy.strategy_version, mode,
                policy.allowed_protocols, policy.allowed_assets, policy.allowed_routes,
                {policy.wallet: policy.label},
                {policy.wallet: {"follower_wallet": policy.follower_wallet,
                                 "relationship_id": policy.relationship_id,
                                 "operation_claims": False,
                                 "ledger_scope": policy.ledger_scope}},
                policy.snapshot_hash,
                execution_routes=policy.route_definitions,
                shadow_only=mode != policy.trigger_mode,
                execution_providers=policy.execution_providers)
    watchlist = monitoring_watchlist(args.watchlist, arc_config)

    async def exit_gate(signal):
        """Refuse a token 0x will not quote a sell for: bought in, cannot get out."""
        verdict = await verify_arc_exit_via_aggregator(
            zeroex, signal.token_out, chain_id=ARC.chain_id, policy=safety)
        stats["arc_exit_gate_accepted" if verdict["accepted"]
              else "arc_exit_gate_rejected"] += 1
        report("arc_exit_gate", source_event_id=signal.event_id,
               smart_wallet=signal.wallet, **verdict, live_trading=False)
        return verdict["accepted"]

    async def decide(signal):
        selected = arc_config.policies_for(signal.wallet, signal.chain_id)
        if not selected:
            return
        if signal.behavior in {"BUY", "TOKEN_SWAP"} and not await exit_gate(signal):
            return
        for policy in selected:
            if signal.behavior in {"BUY", "TOKEN_SWAP"}:
                bucket = (budget_bucket(signal.token_in, signal.chain_id)
                          if signal.token_in else None)
                rule = policy.buy_rules.get(bucket)
                if rule is None:
                    continue
                method = "buy"
            elif signal.behavior == "SELL":
                rule, method = policy.sell_rule, "sell"
            else:
                return
            # 0x builds an executable quote only inside a bound operation
            # context, which carries the follower, the config snapshot and the
            # quote's age and slippage. Without it every Arc buy was rejected as
            # quote_unavailable before any price was ever fetched.
            with quoter.execution_context(
                    signal.event_id, policy.follower_wallet, policy.snapshot_hash,
                    policy.quote_policy.max_age_seconds,
                    policy.quote_policy.max_slippage_bps) as context:
                for mode in (policy.trigger_mode, *policy.shadow_trigger_modes):
                    engine = engines[(mode, policy.ledger_scope)]
                    ready = ((mode == "receipt_success" and signal.execution_status == "success")
                             or (mode == "feed_intent" and signal.stage == "intent")
                             or (mode == "swap_evidenced" and signal.stage in {
                                 "swap_evidenced", "needs_review", "failed"})
                             or (mode in {"relay_sell_evidenced", "relay_buy_evidenced"}
                                 and signal.stage in {mode, "needs_review", "failed"})
                             or (mode == "evidenced" and signal.stage in {
                                 "swap_evidenced", "relay_sell_evidenced",
                                 "relay_buy_evidenced", "needs_review", "failed"}))
                    if not ready:
                        continue
                    decision = (await engine.propose_buy(signal, rule) if method == "buy"
                                else await engine.propose_sell(signal, rule))
                    stats["paper_decisions"] += 1
                    stats["paper_accepted" if decision.accepted else "paper_rejected"] += 1
                    report("paper_decision", decision_id=decision.decision_id,
                           source_event_id=signal.event_id, trigger_mode=mode,
                           relationship_id=policy.relationship_id, chain_id=signal.chain_id,
                           shadow_only=engine.shadow_only, accepted=decision.accepted,
                           reason=decision.reason, proposal_id=decision.proposal_id,
                           live_trading=False)
                    if decision.accepted and decision.proposal_id and not engine.shadow_only:
                        execution = await executors[policy.ledger_scope].execute(
                            signal, decision.proposal_id)
                        stats["paper_filled" if execution.status == "filled"
                              else "paper_fill_cancelled"] += 1
                        report("paper_execution", proposal_id=execution.proposal_id,
                               status=execution.status, reason=execution.reason,
                               fill_id=execution.fill_id, chain_id=signal.chain_id,
                               paper_only=True, live_trading=False)
                report("execution_quote_requests", source_event_id=signal.event_id,
                       relationship_id=policy.relationship_id,
                       route_requests=context["route_requests"],
                       build_requests=context["build_requests"],
                       quote_reuses=context["quote_reuses"], live_trading=False)

    # Decisions are serialized through a bounded queue: the observer stays
    # responsive, and one slow decision can never interleave with another.
    queue = asyncio.Queue(maxsize=256)

    def on_signal(signal):
        try:
            queue.put_nowait(signal)
        except asyncio.QueueFull:
            stats["decision_queue_drops"] += 1
            report("arc_decision_queue_full", source_event_id=signal.event_id,
                   live_trading=False)

    async def decide_forever():
        while True:
            signal = await queue.get()
            try:
                await decide(signal)
            except Exception as exc:
                stats["decision_errors"] += 1
                report("arc_decision_error", source_event_id=signal.event_id,
                       error_type=type(exc).__name__, live_trading=False)
            finally:
                queue.task_done()

    def status(event, details):
        report(event, **details, chain_id=ARC.chain_id, live_trading=False)

    report("arc_paper_started", chain_id=ARC.chain_id,
           relationships=[policy.relationship_id for policy in policies],
           smart_wallets=len(watchlist), cycle_id=cycle_id,
           cycle_created=cycle_created, max_tax_bps=safety.max_tax_bps,
           max_round_trip_loss_bps=safety.max_round_trip_loss_bps,
           probe_amount_raw=str(safety.probe_amount_raw),
           paper_only=True, live_trading=False)
    consumer = asyncio.create_task(decide_forever())
    try:
        task = observe_arc(
            rpc, ws_url, store, watchlist, on_signal, status,
            backfill_interval=args.backfill_interval,
            backfill_batch=args.backfill_batch,
            relay_client=relay_client)
        if args.seconds:
            try:
                await asyncio.wait_for(task, timeout=args.seconds)
            except asyncio.TimeoutError:
                await queue.join()
                report("arc_paper_duration_complete", seconds=args.seconds,
                       counters=dict(stats), live_trading=False)
        else:
            await task
    finally:
        consumer.cancel()
        rpc.close()
        zeroex.close()
        store.close()


def runtime_paper_config(args):
    if getattr(args, "paper_mysql", False):
        return load_mysql_paper_config()
    path = getattr(args, "paper_config", None) or getattr(args, "config", None)
    return load_paper_config(path) if path else None


def runtime_store(args):
    """Choose exactly one ledger backend; never dual-write."""
    return MySqlStore() if getattr(args, "ledger_mysql", False) else Store(args.db)


def monitoring_watchlist(path, paper_config=None):
    """Use the CSV evidence set plus every enabled database relationship wallet."""
    watchlist = load_watchlist(path)
    if paper_config is not None:
        for policy in paper_config.relationships:
            watchlist.setdefault(policy.wallet, {
                "real_evm": policy.wallet,
                "handle": policy.label,
                "source": "copy_relationships",
            })
    return watchlist


def prepare_runtime_budget_cycle(store, config, action, cycle_id=None, reason=None):
    """Reuse budgets on restart, initializing a first cycle and new scopes safely."""
    active = store.active_paper_budget_cycle()
    created = False
    if action == "reset":
        store.start_paper_budget_cycle(cycle_id, reason)
        active, created = cycle_id, True
    elif action == "reuse":
        if active is None:
            raise ValueError("no active paper budget cycle to reuse")
    elif action == "auto":
        if active is None:
            active = f"mainnet-auto-{time.time_ns()}"
            store.start_paper_budget_cycle(active, "automatic_initial_cycle")
            created = True
    else:
        raise ValueError("paper mode requires a budget cycle action")
    budgets = [(policy.ledger_scope, bucket, limit)
               for policy in config.relationships
               for bucket, limit in policy.budget_limits.items()]
    for scope, bucket, limit in budgets:
        current = store.paper_budget(scope, bucket)
        if (current is not None
                and int(current["reserved_raw"]) + int(current["invested_raw"])
                > int(limit)):
            raise ValueError("new limit is below occupied budget")
    for scope, bucket, limit in budgets:
        # Existing invested/reserved amounts are preserved. The store rejects
        # a reduced limit below occupied budget and initializes only new scopes.
        store.configure_paper_budget(scope, bucket, limit)
    return active, created


def validate_live_relationships(live_policies):
    """Fail startup unless every enabled live relationship and follower key is ready."""
    ready = []
    for policy in live_policies:
        require_mainnet_broadcast_enabled(
            policy.follower_wallet, policy.relationship_id, policy.snapshot_hash)
        key_status = live_key_record_status(
            policy.follower_wallet, policy.relationship_id, policy.snapshot_hash)
        if not key_status["found"] or not key_status["enabled"]:
            raise ValueError(
                f"enabled follower signing key is unavailable for relationship "
                f"{policy.relationship_id}")
        ready.append(policy)
    return tuple(ready)


def live_wallet_execution_locks(live_policies):
    """Serialize approval/nonce/broadcast for one follower, not across followers."""
    return {policy.follower_wallet: asyncio.Lock() for policy in live_policies}


def require_existing_sqlite(args):
    if not getattr(args, "ledger_mysql", False) and not Path(args.db).is_file():
        raise ValueError("database does not exist")


class LatencySamples:
    """Bounded in-process timings; values from different processes are not mixed."""

    def __init__(self, limit: int = 10000):
        self.limit = limit
        self.values = defaultdict(list)

    def observe(self, name: str, seconds: float) -> None:
        values = self.values[name]
        if len(values) < self.limit:
            values.append(max(0.0, seconds * 1000))

    def summary(self) -> dict:
        result = {}
        for name, values in self.values.items():
            ordered = sorted(values)

            def percentile(p):
                return round(ordered[max(0, (len(ordered) * p + 99) // 100 - 1)], 3)

            result[name] = {
                "count": len(ordered), "p50": percentile(50),
                "p95": percentile(95), "p99": percentile(99),
            }
        return result


def coverage_summary(stats) -> dict:
    total = int(stats.get("receipt_signals", 0))
    return {
        "receipt_signals": total,
        "unknown": int(stats.get("receipt_unknown", 0)),
        "needs_review": int(stats.get("receipt_needs_review", 0)),
        "swap_evidenced": int(stats.get("receipt_swap_evidenced", 0)),
        "relay_sell_evidenced": int(stats.get("receipt_relay_sell_evidenced", 0)),
        "relay_buy_evidenced": int(stats.get("receipt_relay_buy_evidenced", 0)),
        "unknown_fraction": round(int(stats.get("receipt_unknown", 0)) / total, 6) if total else None,
    }


def dispatch_pending(store, queue, stats) -> int:
    """Move only durable candidates into the bounded in-memory work queue."""
    available = queue.maxsize - queue.qsize()
    candidates = store.claim_candidates(available, chain_id=CHAIN_ID)
    queued_at = time.monotonic()
    for tx in candidates:
        queue.put_nowait((tx, queued_at))
        stats["candidates_dispatched"] += 1
    return len(candidates)


CRITICAL_TASK_BACKOFF_SECONDS = (1.0, 2.0, 5.0, 10.0, 30.0)


async def supervise_critical_task(name, iteration, stats, fail_closed,
                                  backoffs=CRITICAL_TASK_BACKOFF_SECONDS):
    """Retry a critical monitor loop through transient faults; fail closed when stuck.

    On 2026-09-16 a ledger socket loss killed the heartbeat and dispatcher tasks
    silently while the process kept running. Every failure now emits a structured
    event carrying only the exception type (no raw error text, URLs or params).
    Consecutive failures beyond the backoff schedule call ``fail_closed`` once and
    re-raise so the monitor exits loudly instead of degrading unnoticed.
    """
    failures = 0
    while True:
        try:
            await iteration()
            failures = 0
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            failures += 1
            stats[f"{name}_errors"] += 1
            exhausted = failures > len(backoffs)
            report("critical_task_error", task=name, error_type=type(exc).__name__,
                   consecutive_failures=failures, will_fail_closed=exhausted)
            if exhausted:
                fail_closed()
                raise
            await asyncio.sleep(backoffs[failures - 1])


def replay(args):
    watchlist = load_watchlist(args.watchlist)
    decoder = Decoder(watchlist, snapshot_delegations(args.accounts))
    source = json.loads(Path(args.fixtures).read_text())
    examples = [(Transaction.from_rpc(row["transaction"]), row["receipt"]) for row in source["examples"]]
    if "live_example" in source:
        live = source["live_example"]
        examples.append((decode_raw(bytes.fromhex(live["capture"]["raw"][2:])), live["receipt"]))
    if args.extra_fixture:
        for path in args.extra_fixture:
            example = json.loads(Path(path).read_text())
            examples.append((Transaction.from_rpc(example["transaction"]), example["receipt"]))
    store = runtime_store(args)
    counts = Counter()
    try:
        for tx, receipt in examples:
            for signal in enrich(tx, decoder.decode(tx), receipt, watchlist):
                emit(store, signal)
                counts[signal.behavior] += 1
    finally:
        store.close()
    report("replay_finished", transactions=len(examples), behaviors=dict(counts), live_trading=False)


def relay_associate(args):
    """Offline/manual association; the command never calls Relay or an RPC endpoint."""
    require_existing_sqlite(args)
    document = json.loads(Path(args.document).read_text())
    store = runtime_store(args)
    try:
        candidate = store.signal(args.event_id)
        if candidate is None:
            raise ValueError("signal event id not found")
        associated = relay_passive_buy(document, candidate)
        emit(store, associated)
        report("relay_passive_buy_associated", event_id=associated.event_id,
               stage=associated.stage, copy_eligible=False, live_trading=False)
    finally:
        store.close()


def relay_requeue_pending(args):
    """Re-queue the strict-channel candidates behind open early lots still pending.

    Until 2026-09-18 a failed Relay lookup was terminal in the strict channel, so a
    delivery whose lookup failed once stayed ``needs_review`` and the early lot it
    funded stayed ``pending`` forever, freezing every sell of that token for the
    relationship. This sends only those candidates back through the normal worker,
    which re-runs the Relay attribution and the early-lot reconciliation exactly as
    a live delivery would. Without ``--confirm`` it only reports what it would do.
    This command copies nothing and calls no network endpoint itself.
    """
    require_existing_sqlite(args)
    store = runtime_store(args)
    try:
        hashes = store.pending_early_source_tx_hashes()
        statuses = store.candidate_statuses(hashes)
        eligible = [h for h in hashes if statuses.get(h) in {"complete", "failed"}]
        requeued = (store.requeue_candidates(eligible, "operator_requeue_pending_early_lot")
                    if args.confirm else 0)
        report("relay_requeue_pending", pending_source_transactions=len(hashes),
               eligible=len(eligible), requeued=requeued, confirmed=bool(args.confirm),
               skipped_states={h: statuses.get(h, "missing") for h in hashes if h not in eligible},
               live_trading=False)
    finally:
        store.close()


def paper_cycle(args):
    config = runtime_paper_config(args)
    store = runtime_store(args)
    try:
        active = store.active_paper_budget_cycle()
        if args.action == "reuse":
            if active is None:
                raise ValueError("no active paper budget cycle to reuse")
            report("paper_cycle_reused", cycle_id=active, live_trading=False)
            return
        store.start_paper_budget_cycle(args.cycle_id, args.reason)
        for policy in config.relationships:
            for bucket, limit in policy.budget_limits.items():
                store.configure_paper_budget(policy.ledger_scope, bucket, limit)
        report("paper_cycle_reset", cycle_id=args.cycle_id,
               wallets=len(config.relationships), live_trading=False)
    finally:
        store.close()


def relationship_status(args):
    """Print one enabled relationship and its snapshot without key material."""
    acceptance = load_enabled_mainnet_acceptance(args.relationship_id)
    policy = acceptance["policy"]
    accepted_at = acceptance["accepted_at"]
    updated_at = acceptance["updated_at"]
    acceptance_current = (
        accepted_at is not None
        and updated_at is not None
        and accepted_at >= updated_at
    )
    print(json.dumps({
        "relationship_id": policy.relationship_id,
        "run_mode": policy.run_mode,
        "follower_wallet": policy.follower_wallet,
        "smart_wallet": policy.wallet,
        "smart_wallet_label": policy.label,
        "strategy_version": policy.strategy_version,
        "trigger_mode": policy.trigger_mode,
        "allowed_protocols": sorted(policy.allowed_protocols),
        "trusted_assets": sorted(policy.allowed_assets),
        "route_definitions": policy.route_definitions,
        "execution_providers": list(policy.execution_providers),
        "budget_limits": policy.budget_limits,
        "config_snapshot_hash": policy.snapshot_hash,
        "live_risk_accepted_at": (
            accepted_at.isoformat(timespec="microseconds") if accepted_at else None),
        "relationship_updated_at": (
            updated_at.isoformat(timespec="microseconds") if updated_at else None),
        "live_risk_acceptance_current": acceptance_current,
        "private_key_read": False,
    }, ensure_ascii=False, sort_keys=True))


async def mainnet_approve_usdg(args):
    """Explicitly broadcast one exact-budget USDG approval after all live gates."""
    load_endpoint_env()
    policy = load_enabled_relationship_policy(args.relationship_id)
    rpc = ReadOnlyRpc(os.environ.get(
        "ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    if number(await rpc.call("eth_chainId")) != CHAIN_ID:
        raise ValueError("RPC is connected to the wrong chain")
    result = await approve_relationship_usdg(
        policy, rpc, MySqlRelationshipGate(), MainnetBroadcaster())
    print(json.dumps({
        "relationship_id": policy.relationship_id,
        "follower_wallet": policy.follower_wallet,
        "asset": "USDG", "spender": "v3_router",
        "amount_raw": result.amount_raw,
        "previous_allowance_raw": result.previous_allowance_raw,
        "tx_hash": result.tx_hash, "submitted": result.submitted,
        "live_trading": True,
    }, ensure_ascii=False, sort_keys=True))


async def monitor(args):
    load_endpoint_env()
    paper_config = runtime_paper_config(args)
    watchlist = monitoring_watchlist(args.watchlist, paper_config)
    watched_bytes = [bytes.fromhex(a[2:]) for a in watchlist]
    # Backfill only needs the wallets whose trades we may copy; the CSV
    # observation set keeps flowing through the live feed. Fewer topics also
    # means fewer passive candidates and Relay lookups per range.
    backfill_watchlist = (
        {wallet: watchlist[wallet] for wallet in paper_config.wallets if wallet in watchlist}
        if paper_config and paper_config.wallets else watchlist)
    rpc = ReadOnlyRpc(os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    relay_client = RelayPublicClient() if args.relay_auto_associate else None
    feed_url = os.environ.get("ROBINHOOD_FEED_URL", "wss://feed.mainnet.chain.robinhood.com")
    if not feed_url.startswith("wss://"):
        raise ValueError("feed must use WSS")
    if number(await rpc.call("eth_chainId")) != CHAIN_ID:
        raise ValueError("RPC is connected to the wrong chain")
    decoder = Decoder(watchlist)
    queue = asyncio.Queue(maxsize=args.queue_size)
    early_trial_id = getattr(args, "early_trial_id", None)
    early_enabled = bool(getattr(args, "early_feed_evidence", False) or early_trial_id)
    health = FeedHealth(max_age_seconds=EARLY_FEED_MAX_AGE_SECONDS) if early_enabled else FeedHealth()
    stats = Counter()
    store = runtime_store(args)
    paper_engines = {}
    # Trial scope remains enrolled in dedup even when a later restart omits the
    # early flag, or the window is stopped/expired. Missing pre-upgrade schema is
    # the only tolerated database error here.
    try:
        trial_scopes = store.connection.execute(
            "SELECT follower_wallet,relationships_payload FROM early_trials").fetchall()
    except Exception as exc:
        if getattr(exc, "args", (None,))[0] != 1146:
            raise
        trial_scopes = []
    operation_scopes = {(follower, relation) for follower, payload in trial_scopes
                        for relation in json.loads(payload)}
    early_lane = None
    deployment_monitor = None
    early_policies = ()
    if early_trial_id:
        trial = store.early_trial_status(early_trial_id)
        if trial is None or paper_config is None:
            raise ValueError("operator-started early trial and MySQL policies required")
        early_policies = tuple(p for p in paper_config.relationships
                               if p.relationship_id in trial["relationships"])
        if (len(early_policies) != len(trial["relationships"])
                or any(p.follower_wallet != trial["follower_wallet"]
                       or p.run_mode != "mainnet_live" or p.execution_providers not in
                       (("kyber",), ("zeroex",), ("zeroex", "kyber"))
                       or p.trigger_mode != "evidenced" for p in early_policies)):
            raise ValueError("early trial scope must match enabled aggregator-only live relationships")
        for table in ("copy_operation_claims", "early_trial_operations", "early_feed_jobs"):
            store.connection.execute(f"SELECT * FROM {table} LIMIT 0")
        audit = store.execution_audit()
        if (not audit["healthy"] or audit["prepared"]
                or any(audit["attempt_statuses"].get(k, 0) for k in ("signed", "observed_pending", "orphaned"))):
            raise ValueError("early startup requires operator review of unresolved executions")
        for (payload,) in store.connection.execute(
                "SELECT attribution_payload FROM paper_proposals WHERE status='reserved'").fetchall():
            if "early_trial_id" in json.loads(payload):
                raise ValueError("early restart has reserved work; operator reconciliation required")
    if getattr(args, "early_feed_evidence", False) or early_trial_id:
        # Fail startup before any recovery/execution if the optional schema is
        # absent. One shared Feed receiver; this lane has no execution callback.
        store.connection.execute("SELECT tx_hash FROM early_feed_jobs LIMIT 0")
        deployment_monitor = DeploymentMonitor(rpc, report)
        early_lane = EarlyFeedLane(
            store, EarlyEvidenceResolver(rpc, relay_client or RelayPublicClient(),
                paper_config.wallets if paper_config else watchlist,
                deployment_monitor=deployment_monitor),
            healthy=health.healthy)
    paper_executor = None
    live_pipelines = {}
    # Per-chain RPC readers and broadcasters, filled in once the
    # relationships are known; the Robinhood reader is the one above.
    chain_rpcs, broadcasters = {CHAIN_ID: rpc}, {}
    live_tracking_tasks = set()
    live_wallet_locks = {}
    if paper_config:
        live_policies = [policy for policy in paper_config.relationships
                         if policy.run_mode == "mainnet_live"]
        if live_policies:
            if not args.paper_mysql or not args.ledger_mysql:
                raise ValueError(
                    "mainnet_live requires the MySQL configuration and ledger")
            validate_live_relationships(live_policies)
            live_wallet_locks = live_wallet_execution_locks(live_policies)
            for policy in live_policies:
                report("live_key_ready", relationship_id=policy.relationship_id,
                       follower_wallet=policy.follower_wallet,
                       private_key_read=False, live_trading=True)
            report("live_relationships_ready", relationships=len(live_policies),
                   follower_wallets=len(live_wallet_locks), live_trading=True)
        cycle_id, cycle_created = prepare_runtime_budget_cycle(
            store, paper_config, args.paper_cycle_action,
            args.paper_cycle_id, args.paper_cycle_reason)
        report("paper_cycle_initialized" if cycle_created else "paper_cycle_reused",
               cycle_id=cycle_id, wallets=len(paper_config.relationships),
               automatic=args.paper_cycle_action == "auto", live_trading=False)
        # One RPC and one broadcaster per chain. A relationship's chain decides
        # which it gets, so a plan built on one chain can never be preflighted,
        # signed against, or broadcast to another chain's node.
        for policy in paper_config.relationships:
            if policy.chain_id not in chain_rpcs:
                endpoint = os.environ.get(chain_for(policy.chain_id).rpc_env)
                if not endpoint:
                    raise ValueError(
                        f"chain {policy.chain_id} has no configured RPC endpoint")
                chain_rpcs[policy.chain_id] = ReadOnlyRpc(endpoint)
            if policy.run_mode != "paper" and policy.chain_id not in broadcasters:
                broadcasters[policy.chain_id] = MainnetBroadcaster(chain_id=policy.chain_id)
        aggregators = {}
        for policy in paper_config.relationships:
            for provider in policy.execution_providers:
                if provider == "kyber" and provider not in aggregators:
                    aggregators[provider] = KyberAggregatorClient()
                elif provider == "zeroex" and provider not in aggregators:
                    aggregators[provider] = ZeroExAggregatorClient(os.environ.get("0X_API_KEY"))
        # The quoter reads headers, gas prices and contracts per chain: a fee
        # built from another chain's gas price is off by orders of magnitude.
        quoter = LiveQuoter(rpc, aggregators, chain_rpcs=chain_rpcs)
        if aggregators:
            report("execution_providers_ready", providers=sorted(aggregators),
                   relationships=sum(1 for policy in paper_config.relationships
                                     if any(p in aggregators
                                            for p in policy.execution_providers)),
                   live_trading=False)
        paper_executor = {}
        relationship_gate = MySqlRelationshipGate() if live_policies else None
        for policy in paper_config.relationships:
            if policy.run_mode == "paper":
                paper_executor[policy.ledger_scope] = PaperExecutor(
                    store, quoter, policy.quote_policy, policy.route_definitions)
            else:
                chain_rpc = chain_rpcs[policy.chain_id]
                preparer = ExecutionPreparer(
                    store, quoter, chain_rpc, policy.quote_policy,
                    policy.allowed_protocols, policy.allowed_assets,
                    policy.allowed_routes, policy.snapshot_hash,
                    single_preflight=not getattr(args, "legacy_preflight", False))
                signer = LiveExecutionSigner(
                    store, quoter, chain_rpc, policy.quote_policy, policy.snapshot_hash,
                    relationship_gate=relationship_gate)
                reviewer = LivePreBroadcastReviewer(
                    store, quoter, chain_rpc, policy.quote_policy, relationship_gate)
                live_pipelines[policy.ledger_scope] = (
                    preparer, signer, reviewer, broadcasters[policy.chain_id],
                    ReadOnlyExecutionTracker(store, chain_rpc),
                )
            for mode in (policy.trigger_mode, *policy.shadow_trigger_modes):
                paper_engines[(mode, policy.ledger_scope)] = PaperEngine(
                    store, quoter, policy.quote_policy,
                    policy.strategy_version, mode,
                    policy.allowed_protocols, policy.allowed_assets,
                    policy.allowed_routes, {policy.wallet: policy.label},
                    {policy.wallet: {"follower_wallet": policy.follower_wallet,
                                     "relationship_id": policy.relationship_id,
                                     "operation_claims": (policy.follower_wallet, policy.relationship_id) in operation_scopes,
                                     "ledger_scope": policy.ledger_scope}},
                    policy.snapshot_hash,
                    execution_routes=policy.route_definitions,
                    shadow_only=mode != policy.trigger_mode,
                    execution_providers=policy.execution_providers,
                )
    timings = LatencySamples()
    wake_dispatcher = asyncio.Event()
    for name in (
        "candidates", "candidate_duplicates", "candidates_dispatched", "receipts",
        "receipt_unavailable", "candidate_retries", "candidate_retry_exhausted",
        "queue_drops", "worker_errors", "account_code_errors", "reconnections", "frame_errors",
        "backfill_blocks", "backfill_candidates", "backfill_errors", "reorg_detected",
        "reorg_orphaned_signals", "reorg_candidates_requeued",
        "backfill_passive_candidates",
        "intent_signals", "receipt_signals", "receipt_unknown",
        "receipt_needs_review", "receipt_swap_evidenced",
        "receipt_relay_sell_evidenced", "receipt_relay_buy_evidenced",
        "account_prestate_missing",
        "paper_decisions", "paper_accepted", "paper_rejected", "paper_shadow_accepted",
        "paper_filled", "paper_fill_cancelled",
        "paper_reserved_recovered", "paper_errors",
        "live_prepared", "live_signed", "live_broadcast", "live_pending",
        "live_confirmed", "live_reverted", "live_orphaned", "live_errors",
        "live_settled", "live_approval_broadcast", "live_approval_confirmed",
        "relay_lookup_pending", "relay_lookup_errors", "relay_lookup_skipped", "relay_buy_associated",
        "relay_sell_confirmed",
        "local_v3_routes_verified",
    ):
        stats[name] = 0
    stats["recovered_inflight"] = store.recover_inflight()
    start_counts = store.candidate_counts()
    stats["pending_candidates_at_start"] = start_counts["pending"] + start_counts["retry"]

    async def recover_paper_reservations():
        if not paper_config:
            return
        ids = []
        for policy in paper_config.relationships:
            ids.extend(store.reserved_paper_proposal_ids(
                policy.strategy_version, policy.trigger_mode))
        ids = sorted(set(ids))
        for proposal_id in ids:
            proposal = store.paper_proposal(proposal_id)
            signal = store.signal(proposal["source_event_id"])
            attribution = proposal.get("attribution", {})
            matching = [policy for policy in paper_config.policies_for(signal.wallet, signal.chain_id)] \
                if signal is not None else []
            matching = [policy for policy in matching
                        if policy.relationship_id == attribution.get("relationship_id")
                        and policy.follower_wallet == attribution.get("follower_wallet")]
            if (signal is None or len(matching) != 1
                    or (signal is not None and scope_reason(
                        signal, matching[0].allowed_protocols,
                        matching[0].allowed_assets,
                        matching[0].allowed_routes) is not None)):
                store.cancel_paper_proposal(proposal_id, "source_signal_unavailable_on_recovery")
                stats["paper_fill_cancelled"] += 1
                continue
            if matching[0].run_mode == "mainnet_live":
                stats["live_errors"] += 1
                report("live_recovery_requires_operator_review", proposal_id=proposal_id,
                       relationship_id=matching[0].relationship_id,
                       live_trading=True)
                continue
            execution = await paper_executor[matching[0].ledger_scope].execute(
                signal, proposal_id)
            stats["paper_reserved_recovered"] += 1
            stats["paper_filled" if execution.status == "filled"
                  else "paper_fill_cancelled"] += 1
            report("paper_execution_recovered", proposal_id=proposal_id,
                   status=execution.status, reason=execution.reason,
                   fill_id=execution.fill_id, paper_only=True, live_trading=False)

    async def ensure_exit_allowance(policy, proposal_id):
        """Approve the token we just bought, while nothing is waiting on it.

        A sell cannot even be quoted through an aggregator until that token is
        approved, and doing it at exit time costs an extra confirmation exactly
        when the price is moving away. Granting it right after a buy settles
        moves that cost off the exit path entirely; a later sell then quotes in
        one round trip. The allowance is a bounded multiple of the position this
        relationship can actually attribute, never unlimited, and a failure here
        is reported and left alone: the buy already settled, and the executor
        still approves at sell time if this never happened.
        """
        if policy.run_mode == "paper" or relationship_gate is None:
            return
        proposal = store.paper_proposal(proposal_id)
        token = (proposal or {}).get("output_asset")
        spender = aggregator_routers(policy.chain_id).get(
            next((name for name in policy.execution_providers
                  if name in AGGREGATOR_PROVIDERS), ""))
        if not token or spender is None:
            return
        position = store.paper_open_position_amount(policy.ledger_scope, token)
        if int(position) <= 0:
            return
        target = str(int(position) * EXIT_ALLOWANCE_MULTIPLE)
        try:
            approval = await approve_relationship_token(
                policy, chain_rpcs[policy.chain_id], relationship_gate,
                broadcasters[policy.chain_id], token, target, spender,
                minimum_required_raw=position)
            if approval.submitted:
                stats["exit_allowance_broadcast"] += 1
                confirmation = await confirm_relationship_token_approval(
                    chain_rpcs[policy.chain_id], approval, policy.follower_wallet)
                stats["exit_allowance_confirmed"] += 1
                # The confirmation is nested rather than splatted: it carries
                # its own asset/spender keys, and merging them collided with
                # the ones named here.
                report("exit_allowance_confirmed", proposal_id=proposal_id,
                       asset=token, spender=spender, amount_raw=target,
                       previous_allowance_raw=approval.previous_allowance_raw,
                       relationship_id=policy.relationship_id,
                       confirmation=confirmation, live_trading=True)
        except Exception as exc:
            stats["exit_allowance_errors"] += 1
            report("exit_allowance_failed", proposal_id=proposal_id, asset=token,
                   spender=spender, error_type=type(exc).__name__,
                   error=str(exc)[:200], relationship_id=policy.relationship_id,
                   live_trading=True)

    async def track_live(policy, proposal_id):
        tracker = live_pipelines[policy.ledger_scope][4]
        previous = None
        attempts, errors = 0, 0
        while early_trial_id or attempts < 180:
            attempts += 1
            try:
                observation = await tracker.observe(proposal_id)
                errors = 0
                if observation.status != previous:
                    report("live_execution_observed", proposal_id=proposal_id,
                           tx_hash=observation.tx_hash, status=observation.status,
                           block_number=observation.block_number,
                           block_hash=observation.block_hash, live_trading=True)
                    previous = observation.status
                if observation.status in {"confirmed", "reverted", "orphaned"}:
                    stats[f"live_{observation.status}"] += 1
                    if observation.status == "confirmed":
                        try:
                            # Read the receipt from the chain the copy ran on:
                            # another chain's node simply does not have it, and
                            # settlement then fails on a trade that did land.
                            settlement = await settle_confirmed_execution(
                                store, chain_rpcs[policy.chain_id], proposal_id,
                                observation.tx_hash)
                            stats["live_settled"] += 1
                            report("live_execution_settled", **settlement,
                                   live_trading=True)
                            if settlement.get("side") == "BUY":
                                await ensure_exit_allowance(policy, proposal_id)
                        except Exception as exc:
                            stats["live_errors"] += 1
                            if early_trial_id:
                                trip_execution_stop()
                            report("live_settlement_error", proposal_id=proposal_id,
                                   tx_hash=observation.tx_hash,
                                   error_type=type(exc).__name__, live_trading=True)
                    elif observation.status == "reverted":
                        # The nonce was consumed and gas paid, but no assets moved;
                        # the proposal cannot fill any more, so its budget goes back.
                        released = store.cancel_paper_proposal(
                            proposal_id, "live_execution_reverted")
                        report("live_execution_reverted_released", proposal_id=proposal_id,
                               tx_hash=observation.tx_hash, proposal_released=released,
                               live_trading=True)
                    elif early_trial_id:
                        trip_execution_stop()
                        await asyncio.sleep(1)
                        continue
                    return
                stats["live_pending"] += observation.status == "observed_pending"
                if (observation.status == "not_observed"
                        and attempts >= DROPPED_BROADCAST_ATTEMPTS):
                    # A node can accept a broadcast and still drop the
                    # transaction before it is mined, so tracking would wait
                    # forever on something that will never land while its nonce
                    # stays reserved. The evidence required to release it is the
                    # same as after a failed send — absent from chain AND the
                    # nonce not consumed — and it is never re-sent.
                    outcome = await verify_broadcast_outcome(
                        proposal_id, observation.tx_hash, policy.follower_wallet,
                        chain_rpcs[policy.chain_id])
                    if outcome == "not_broadcast":
                        released = store.reconcile_unbroadcast_after_send_failure(
                            proposal_id,
                            "broadcast_accepted_but_transaction_absent_chain_verified")
                        stats["live_dropped_released"] += 1
                        report("live_execution_dropped_released",
                               proposal_id=proposal_id, tx_hash=observation.tx_hash,
                               released=released, attempts=attempts, live_trading=True)
                        if released:
                            return
            except Exception as exc:
                stats["live_errors"] += 1
                errors += 1
                if early_trial_id and errors >= 3:
                    trip_execution_stop()
                report("live_tracking_error", proposal_id=proposal_id,
                       error_type=type(exc).__name__, live_trading=True)
            await asyncio.sleep(1)
        report("live_tracking_timeout", proposal_id=proposal_id, live_trading=True)

    async def verify_broadcast_outcome(proposal_id, signed_hash, follower_wallet,
                                       chain_rpc=None):
        """After a broadcast send error, chain-check whether the signed tx reached
        the network. Returns 'broadcast', 'not_broadcast', or 'uncertain'. Read-only
        — it NEVER re-sends. 'not_broadcast' requires BOTH the tx absent from chain
        AND the follower pending nonce not advanced past the reserved nonce; any
        query failure or contradiction returns 'uncertain' (caller fault-latches)."""
        try:
            reader = chain_rpc if chain_rpc is not None else rpc
            if await reader.call("eth_getTransactionByHash", [signed_hash]) is not None:
                return "broadcast"
            pending = number(await reader.call(
                "eth_getTransactionCount", [follower_wallet, "pending"]))
            reservation = store.execution_nonce_reservation(proposal_id)
        except Exception:
            return "uncertain"
        if reservation is None or reservation["status"] != "signed":
            return "uncertain"
        return "not_broadcast" if pending <= int(reservation["nonce"]) else "uncertain"

    async def execute_live_serialized(policy, signal, proposal_id, *, early_intent=None):
        from .execution_controls import _stop_controls
        _stop_controls()
        proposal = store.paper_proposal(proposal_id)
        check_early_execution_source(store, early_intent, signal, proposal)
        if "early_trial_id" in proposal["attribution"]:
            store.check_early_trial_proposal(proposal_id)
        quote_signal = signal if early_intent is not None else execution_quote_signal(
            signal, policy.route_definitions, proposal["output_asset"])
        if quote_signal.protocol in AGGREGATOR_PROVIDERS:
            if quote_signal.protocol not in policy.execution_providers:
                raise ValueError("aggregator execution is not enabled for this relationship")
            report("route_provider_selected", proposal_id=proposal_id,
                   provider=quote_signal.protocol,
                   router=aggregator_routers(policy.chain_id)[quote_signal.protocol],
                   relationship_id=policy.relationship_id, live_trading=True)
        elif quote_signal.protocol not in {"v2", "v3", "v4"}:
            raise ValueError("live execution route is not a verified V2/V3/V4 path")
        preparer, signer, reviewer, broadcaster, _ = live_pipelines[policy.ledger_scope]
        chain_rpc = chain_rpcs[policy.chain_id]
        chain = chain_for(policy.chain_id)
        early_options = {"early_intent": early_intent} if early_intent is not None else {}
        if quote_signal.token_in != NATIVE and early_intent is None:
            spender = {"v2": chain.v2_router, "v3": chain.v3_router,
                       **aggregator_routers(policy.chain_id)}.get(quote_signal.protocol)
            if spender is None:
                raise ValueError("live token input route has no verified approval spender")
            if proposal["attribution"].get("source_behavior") == "SELL":
                approval_amount = store.paper_open_position_amount(
                    policy.ledger_scope, quote_signal.token_in)
                if int(approval_amount) < int(proposal["amount_in_raw"]):
                    raise ValueError("attributed approval bound is below proposal input")
                approval = await approve_relationship_token(
                    policy, chain_rpc, relationship_gate, broadcaster,
                    quote_signal.token_in, approval_amount, spender,
                    minimum_required_raw=proposal["amount_in_raw"])
            elif quote_signal.token_in == USDG and spender in {
                    V3_ROUTER, *AGGREGATOR_ROUTERS.values()}:
                approval = await approve_relationship_usdg(
                    policy, chain_rpc, relationship_gate, broadcaster,
                    minimum_required_raw=proposal["amount_in_raw"], spender=spender)
            else:
                approval = await approve_relationship_token(
                    policy, chain_rpc, relationship_gate, broadcaster,
                    quote_signal.token_in, proposal["amount_in_raw"], spender)
            if approval is not None and approval.submitted:
                stats["live_approval_broadcast"] += 1
                report("live_approval_broadcast", proposal_id=proposal_id,
                       tx_hash=approval.tx_hash, asset=approval.asset,
                       spender=approval.spender, amount_raw=approval.amount_raw,
                       previous_allowance_raw=approval.previous_allowance_raw,
                       relationship_id=policy.relationship_id, live_trading=True)
                confirmation = await confirm_relationship_token_approval(
                    chain_rpc, approval, policy.follower_wallet)
                stats["live_approval_confirmed"] += 1
                report("live_approval_confirmed", proposal_id=proposal_id,
                       relationship_id=policy.relationship_id,
                       live_trading=True, **confirmation)
        # Warm the broadcast connection BEFORE preflight. The ticket that gates
        # the send lives for two seconds from preflight, and a cold TLS handshake
        # to a chain's RPC can eat most of that on its own.
        if hasattr(broadcaster, "warm"):
            await asyncio.to_thread(broadcaster.warm)
        stage = "prepare"
        try:
            try:
                prepared = await preparer.prepare(quote_signal, proposal_id, **early_options)
            except (ValueError, RpcError) as exc:
                route_failure = isinstance(exc, ZeroExApiError) or (
                    isinstance(exc, AggregatorSimulationError)
                    and (exc.diagnostic.get("rpc_error") or {}).get("message_category")
                    in {"execution_reverted", "out_of_gas"}) or str(exc) == "insufficient token allowance"
                if (quote_signal.protocol != "zeroex" or policy.execution_providers != ("zeroex", "kyber")
                        or not route_failure or store.execution_plan(proposal_id) is not None):
                    raise
                context = quoter._context.get()
                swap = context["swaps"].get(quoter._quote_key(quote_signal, proposal["amount_in_raw"])) if context else None
                previous_quote = proposal["quote"].get("quote", proposal["quote"])
                floor = swap.minimum_amount_out_raw if swap else str(
                    int(previous_quote["amount_out_raw"])*(10000-policy.quote_policy.max_slippage_bps)//10000)
                deadline = swap.deadline if swap else int(time.time())+120
                if early_intent is not None:
                    quote_signal = early_intent.quote_signal(time.time(), provider="kyber")
                else:
                    from .paper import aggregator_route_definition
                    signal.evidence["local_execution_route"] = aggregator_route_definition(
                        quote_signal.token_in, quote_signal.token_out, "kyber",
                        signal.chain_id)
                    store.put(signal)
                    quote_signal = execution_quote_signal(signal, policy.route_definitions, proposal["output_asset"])
                report("execution_provider_fallback", proposal_id=proposal_id, previous="zeroex",
                       provider="kyber", failure_type=type(exc).__name__)
                prepared = await preparer.prepare(quote_signal, proposal_id, **early_options,
                                                  minimum_floor=floor, original_deadline=deadline)
            stats["live_prepared"] += 1
            report("live_execution_prepared", proposal_id=proposal_id,
                   gas_retry=prepared.preflight.get("gas_retry"),
                   preflight_ms=prepared.preflight.get("preflight_ms"),
                   preflight_rpc_timings=prepared.preflight.get("rpc_timings"),
                   simulation_ms=prepared.preflight.get("simulation_ms"),
                   single_preflight=prepared.ticket is not None)
            stage = "sign"
            signed = await signer.sign(quote_signal, proposal_id, ticket=prepared.ticket, **early_options)
            stats["live_signed"] += 1
            report("live_execution_signed", proposal_id=proposal_id, signing_ms=signed.preflight.get("signing_ms"))
            stage = "review"
            reviewed = await reviewer.review(
                quote_signal, proposal_id, signed.raw_transaction, ticket=signed.ticket, **early_options)
            report("live_execution_reviewed", proposal_id=proposal_id)
        except (ValueError, RpcError) as exc:
            # A rejected requote, gate or simulation before any broadcast must not
            # leave a reserved nonce behind, or every later live plan for this
            # follower would be built one nonce ahead of the network and fail its
            # pre-broadcast nonce check. Signed cleanup is limited to the existing
            # same-process review rejection; it never grants the RPC exemption.
            try:
                plan = store.execution_plan(proposal_id)
                plan_released = False
                if plan is not None and plan["status"] == "prepared":
                    plan_released = store.cancel_prepared_execution_plan(
                        proposal_id, f"live_{stage}_rejected: {str(exc)[:200]}")
                elif (plan is not None and plan["status"] == "signed"
                        and stage == "review"):
                    # Preserve existing same-process, never-broadcast cleanup.
                    # It does NOT qualify for the pre-sign RPC exemption below.
                    plan_released = store.cancel_unbroadcast_signed_execution_plan(
                        proposal_id, f"live_{stage}_rejected: {str(exc)[:200]}")
                proposal_released = False
                if plan is None or plan_released:
                    proposal_released = store.cancel_paper_proposal(
                        proposal_id, f"live_{stage}_rejected")
            except Exception as cleanup_error:
                if early_trial_id:
                    trip_execution_stop()
                report("live_execution_cleanup_failed", proposal_id=proposal_id, stage=stage,
                       error_type=type(cleanup_error).__name__, operator_review_required=True,
                       rpc_diagnostic=exc.diagnostic if isinstance(exc, RpcError) else None)
                # A ValueError from the ledger is NOT an ordinary trade rejection.
                raise RuntimeError("execution cleanup failed; operator review required") from None
            cleanup_complete = (plan is None or plan_released) and proposal_released
            safely_cancelled = (isinstance(exc, RpcError) and stage == "prepare"
                                and cleanup_complete
                                and (plan is None or plan["status"] == "prepared"))
            simulation_details = {}
            if isinstance(exc, AggregatorSimulationError):
                simulation_details = {
                    "simulation_failure": exc.diagnostic,
                    "source_event_id": proposal["attribution"].get("source_event_id", signal.event_id),
                    "source_tx_hash": signal.tx_hash,
                    "early_trial_id": proposal["attribution"].get("early_trial_id"),
                }
            report("live_execution_abandoned", proposal_id=proposal_id, stage=stage,
                   error_type=type(exc).__name__, error=str(exc)[:300],
                   plan_released=plan_released, proposal_released=proposal_released,
                   operator_review_required=not cleanup_complete,
                   safely_cancelled_before_signing=safely_cancelled,
                   rpc_diagnostic=exc.diagnostic if isinstance(exc, RpcError) else None,
                   relationship_id=policy.relationship_id, live_trading=True,
                   **simulation_details)
            if not cleanup_complete:
                if early_trial_id:
                    trip_execution_stop()
                raise RuntimeError("execution cleanup incomplete; operator review required") from None
            if safely_cancelled:
                raise CancelledBeforeSigningRpcError(proposal_id, exc) from None
            raise
        if "copy_operation_order_id" in proposal["attribution"]:
            try:
                store.mark_copy_operation_broadcast_attempted(proposal_id)
            except ValueError:
                # The fence rolled back and the broadcaster has not received the
                # bytes. Release this process's discarded signature/nonce so a
                # trial expiring here cannot block later strict-evidence orders.
                claim = store.connection.execute(
                    "SELECT status FROM copy_operation_claims WHERE proposal_id=?", (proposal_id,)).fetchone()
                if claim != ("held",):
                    if early_trial_id:
                        trip_execution_stop()
                    raise
                released = store.cancel_unbroadcast_signed_execution_plan(
                    proposal_id, "send_fence_rejected_before_network")
                if released:
                    store.cancel_paper_proposal(proposal_id, "send_fence_rejected_before_network")
                raise
        def early_send_check():
            if not health.healthy():
                raise ValueError("Feed unhealthy before early broadcast")
            # Slot has already been consumed. Recheck intent/source here without
            # requiring another available trial slot (the 100th remains usable).
            early_intent.revalidate(time.time())
            for (payload,) in store.connection.execute(
                    "SELECT payload FROM signals WHERE tx_hash=?", (signal.tx_hash,)).fetchall():
                s = json.loads(payload)
                if s.get("wallet") == signal.wallet and (
                        s.get("canonical_status") == "orphaned" or s.get("stage") == "failed"):
                    raise ValueError("early source failed or orphaned before send")
            checked = store.check_early_trial_send_fence(proposal_id)
            store.execution_budget_evidence(proposal_id)
            early_intent.revalidate(time.time())
            return checked
        trial_send_options = ({"early_trial_check": early_send_check}
                              if "early_trial_id" in proposal["attribution"] else {})
        try:
            report("live_execution_send_started", proposal_id=proposal_id)
            result = await broadcaster.broadcast(
                reviewed, signed.raw_transaction,
                follower_wallet=policy.follower_wallet,
                relationship_id=policy.relationship_id,
                config_snapshot_hash=policy.snapshot_hash, **trial_send_options)
        except Exception as exc:
            # Layer 2: a send that failed with an uncertain transport error must not
            # blindly fault-latch. Chain-verify whether the signed tx actually
            # reached the network; NEVER re-send (double-broadcast guard).
            outcome = await verify_broadcast_outcome(
                proposal_id, reviewed.signed_tx_hash, policy.follower_wallet,
                chain_rpcs[policy.chain_id])
            if outcome == "broadcast":
                # It landed despite the send error — record and track like success.
                stats["live_broadcast"] += 1
                report("live_execution_send_recovered", proposal_id=proposal_id,
                       outcome="broadcast_confirmed_onchain", tx_hash=reviewed.signed_tx_hash,
                       error_type=type(exc).__name__, error=str(exc)[:200],
                       relationship_id=policy.relationship_id, live_trading=True)
                task = asyncio.create_task(track_live(policy, proposal_id))
                live_tracking_tasks.add(task)
                task.add_done_callback(live_tracking_tasks.discard)
                return
            if outcome == "not_broadcast":
                # Chain-verified never broadcast and the nonce is unused: release
                # everything (no re-send) so the follower's nonce line stays intact.
                released = store.reconcile_unbroadcast_after_send_failure(
                    proposal_id, f"send_failed_chain_verified_unbroadcast: {type(exc).__name__}")
                # The type alone left a send failure undiagnosable; the message
                # names which gate refused, and carries no secret.
                report("live_execution_send_recovered", proposal_id=proposal_id,
                       outcome="released_unbroadcast" if released else "release_returned_false",
                       tx_hash=reviewed.signed_tx_hash, error_type=type(exc).__name__,
                       error=str(exc)[:200], relationship_id=policy.relationship_id,
                       live_trading=True)
                if released:
                    return
            # uncertain, or release unexpectedly returned False -> conservative latch.
            if early_trial_id:
                trip_execution_stop()
            report("live_execution_send_uncertain", proposal_id=proposal_id,
                   tx_hash=reviewed.signed_tx_hash, error_type=type(exc).__name__,
                   operator_review_required=True, relationship_id=policy.relationship_id,
                   live_trading=True)
            # Signed attempt/fence remains durable; do not retry with another nonce.
            raise
        stats["live_broadcast"] += 1
        report("live_execution_broadcast", proposal_id=proposal_id,
               plan_id=prepared.plan_id, tx_hash=result.tx_hash,
               relationship_id=policy.relationship_id,
               follower_wallet=policy.follower_wallet, live_trading=True)
        task = asyncio.create_task(track_live(policy, proposal_id))
        live_tracking_tasks.add(task)
        task.add_done_callback(live_tracking_tasks.discard)

    async def execute_live(policy, signal, proposal_id, *, early_intent=None):
        # Approval transactions and copy transactions share the follower's
        # account nonce. Keep that entire sequence atomic per follower while
        # allowing distinct follower wallets to proceed concurrently.
        async with live_wallet_locks[policy.follower_wallet]:
            await execute_live_serialized(policy, signal, proposal_id, early_intent=early_intent)

    if early_trial_id:
        quoter.shared_routes_enabled = True
        early_lane.handoff = EarlyRuntime(store, quoter, relationship_gate, early_policies,
            early_trial_id, execute_live, health.healthy, report,
            deployment_monitor=deployment_monitor)
        early_lane.resolver.prefetch = getattr(early_lane.handoff, "prefetch", None)

    async def paper_observe(signal, policies=None):
        if not paper_config or signal.wallet not in paper_config.wallets:
            return
        selected = (paper_config.policies_for(signal.wallet, signal.chain_id)
                    if policies is None else policies)
        for policy in selected:
            if signal.behavior in {"BUY", "TOKEN_SWAP"}:
                bucket = budget_bucket(signal.token_in, signal.chain_id) if signal.token_in else None
                rule = policy.buy_rules.get(bucket)
                if rule is None:
                    continue
                method = "buy"
            elif signal.behavior == "SELL":
                rule, method = policy.sell_rule, "sell"
            else:
                return
            for mode in (policy.trigger_mode, *policy.shadow_trigger_modes):
                engine = paper_engines[(mode, policy.ledger_scope)]
                ready = ((mode == "feed_intent" and signal.stage == "intent")
                         or (mode == "receipt_success" and signal.execution_status == "success")
                         or (mode == "swap_evidenced" and signal.stage in {
                             "swap_evidenced", "needs_review", "failed"})
                         or (mode in {"relay_sell_evidenced", "relay_buy_evidenced"}
                             and signal.stage in {mode, "needs_review", "failed"})
                         or (mode == "evidenced" and signal.stage in {
                             "swap_evidenced", "relay_sell_evidenced",
                             "relay_buy_evidenced", "needs_review", "failed"}))
                if not ready:
                    continue
                decision_started = time.monotonic()
                decision = (await engine.propose_buy(signal, rule) if method == "buy"
                            else await engine.propose_sell(signal, rule))
                timings.observe(f"paper_{mode}_decision_ms",
                                time.monotonic() - decision_started)
                stats["paper_decisions"] += 1
                stats["paper_accepted" if decision.accepted else "paper_rejected"] += 1
                if decision.accepted and engine.shadow_only:
                    stats["paper_shadow_accepted"] += 1
                selected_route = signal.evidence.get("local_execution_route")
                report("paper_decision", decision_id=decision.decision_id,
                       source_event_id=signal.event_id, trigger_mode=mode,
                       relationship_id=policy.relationship_id,
                       follower_wallet=policy.follower_wallet,
                       shadow_only=engine.shadow_only, accepted=decision.accepted,
                       reason=decision.reason, proposal_id=decision.proposal_id,
                       execution_provider=(
                           selected_route.get("provider", "local")
                           if isinstance(selected_route, dict) else "local"),
                       live_trading=policy.run_mode == "mainnet_live")
                if decision.accepted and decision.proposal_id and not engine.shadow_only:
                    execution_started = time.monotonic()
                    if policy.run_mode == "paper":
                        execution = await paper_executor[policy.ledger_scope].execute(
                            signal, decision.proposal_id)
                        timings.observe("paper_execution_requote_ms",
                                        time.monotonic() - execution_started)
                        stats["paper_filled" if execution.status == "filled"
                              else "paper_fill_cancelled"] += 1
                        report("paper_execution", proposal_id=execution.proposal_id,
                               status=execution.status, reason=execution.reason,
                               fill_id=execution.fill_id, paper_only=True,
                               live_trading=False)
                    else:
                        await execute_live(policy, signal, decision.proposal_id)
                        timings.observe("live_execution_ms",
                                        time.monotonic() - execution_started)

    async def observe_policy(signal, policy):
        if early_trial_id and policy.run_mode == "mainnet_live":
            from .execution_controls import _stop_controls
            try:
                _stop_controls()
            except PermissionError:
                report("live_new_decision_stopped", relationship_id=policy.relationship_id)
                return
        # Aggregator-only policies opt into operation-scoped quote reuse. Mixed
        # policies containing 0x also need a bound context when local discovery
        # falls through to its executable quote. Each
        # relationship owns a task-local cache, including while waiting on its
        # wallet lock. Expired quotes are refreshed without resetting their age.
        if ("zeroex" not in policy.execution_providers
                and not set(policy.execution_providers) <= AGGREGATOR_PROVIDERS):
            return await paper_observe(signal, (policy,))
        with quoter.execution_context(signal.event_id, policy.follower_wallet,
                                      policy.snapshot_hash,
                                      policy.quote_policy.max_age_seconds,
                                      policy.quote_policy.max_slippage_bps) as context:
            try:
                return await paper_observe(signal, (policy,))
            finally:
                report("execution_quote_requests", source_event_id=signal.event_id,
                       relationship_id=policy.relationship_id,
                       follower_wallet=policy.follower_wallet,
                       route_requests=context["route_requests"],
                       build_requests=context["build_requests"],
                       quote_reuses=context["quote_reuses"],
                       refreshes=context["refreshes"],
                       shared_route_reuses=context.get("shared_route_reuses", 0),
                       route_retry=context.get("route_retry"))

    async def safe_paper_observe(signal):
        if not paper_config or signal.wallet not in paper_config.wallets:
            return
        policies = paper_config.policies_for(signal.wallet, signal.chain_id)
        results = await asyncio.gather(
            *(observe_policy(signal, policy) for policy in policies),
            return_exceptions=True,
        )
        for policy, result in zip(policies, results):
            if isinstance(result, asyncio.CancelledError):
                raise result
            if isinstance(result, Exception):
                live = policy.run_mode == "mainnet_live"
                stats["live_errors" if live else "paper_errors"] += 1
                report("copy_execution_error", source_event_id=signal.event_id,
                       relationship_id=policy.relationship_id,
                       follower_wallet=policy.follower_wallet,
                       error_type=type(result).__name__,
                       error=str(result)[:300], live_trading=live)

    async def account_impl(wallet, block="latest", strict=False):
        try:
            code = await rpc.call("eth_getCode", [wallet, block])
            impl = delegation(code)
            decoder.delegations.pop(wallet, None)
            if impl:
                decoder.delegations[wallet] = impl
        except RpcError:
            decoder.delegations.pop(wallet, None)
            stats["account_code_errors"] += 1
            if strict:
                raise

    async def worker():
        while True:
            tx, queued_at = await queue.get()
            started = time.monotonic()
            timings.observe("queue_wait_ms", started - queued_at)
            try:
                if tx.to == ENTRYPOINT:
                    candidates = [a for a in watchlist if bytes.fromhex(a[2:]) in tx.data]
                else:
                    candidates = [tx.sender] if tx.sender in watchlist else []
                await asyncio.gather(*(account_impl(a) for a in candidates))
                signals = decoder.decode(tx)
                # Freshness is checked again after queue and RPC waiting.
                fresh = bool(tx.timestamp and health.healthy() and time.time() - tx.timestamp <= DEFAULT_FEED_MAX_AGE_SECONDS)
                for signal in signals:
                    signal.fresh = fresh
                    signal.evidence["account_state_source"] = "latest_not_historical"
                    signal.evidence["observation_source"] = tx.observation_source
                    emit(store, signal)
                    await safe_paper_observe(signal)
                    stats["intent_signals"] += 1
                rpc_started = time.monotonic()
                receipt = await rpc.receipt(tx.hash)
                timings.observe("receipt_rpc_ms", time.monotonic() - rpc_started)
                if receipt is None:
                    stats["receipt_unavailable"] += 1
                    attempts, delay = store.retry_candidate(tx.hash, "receipt_unavailable")
                    if delay is None:
                        stats["candidate_retry_exhausted"] += 1
                    else:
                        stats["candidate_retries"] += 1
                    report("receipt_unavailable", tx_hash=tx.hash, attempts=attempts,
                           retry_in_seconds=delay, retry_exhausted=delay is None)
                else:
                    prestate_started = time.monotonic()
                    implementations, missing = await prestate_implementations(rpc, tx.hash, candidates)
                    timings.observe("account_prestate_rpc_ms", time.monotonic() - prestate_started)
                    stats["account_prestate_missing"] += len(missing)
                    for wallet in candidates:
                        decoder.delegations.pop(wallet, None)
                        if wallet in implementations:
                            decoder.delegations[wallet] = implementations[wallet]
                    signals = decoder.decode(tx)
                    pool_started = time.monotonic()
                    pool_checks = await verify_signal_pools(rpc, signals, receipt)
                    timings.observe("pool_verification_rpc_ms", time.monotonic() - pool_started)
                    native_started = time.monotonic()
                    native_checks = await verify_native_flows(rpc, tx, receipt, signals)
                    if native_checks:
                        timings.observe("native_trace_rpc_ms", time.monotonic() - native_started)
                    final_signals = enrich(tx, signals, receipt, watchlist, pool_checks, native_checks)
                    defer_completion = False
                    defer_reason = "relay_request_not_ready"
                    for signal in final_signals:
                        signal.fresh = bool(tx.timestamp and health.healthy() and time.time() - tx.timestamp <= DEFAULT_FEED_MAX_AGE_SECONDS)
                        signal.evidence["account_state_source"] = "transaction_prestate_trace"
                        signal.evidence["observation_source"] = tx.observation_source
                        if signal.stage == "relay_sell_evidenced":
                            try:
                                signal.evidence["local_execution_route"] = \
                                    await discover_v3_execution_route(
                                        rpc, receipt, signal.token_in, signal.token_out,
                                        signal.evidence.get("actual_output_credit_raw"))
                                stats["local_v3_routes_verified"] += 1
                            except (RpcError, ValueError) as exc:
                                report("local_execution_route_rejected",
                                       source_event_id=signal.event_id,
                                       error_type=type(exc).__name__, live_trading=False)
                        passive_lookup = (relay_client is not None
                                and signal.behavior in {
                                    "EXTERNAL_DELIVERY_CANDIDATE", "INCOMING_TRANSFER"}
                                and signal.stage == "needs_review")
                        # Compute from this receipt, never trust a persisted skip flag.
                        if passive_lookup:
                            for key in ("relay_lookup_skipped", "relay_lookup_skip_reason",
                                        "relay_lookup_skip_evidence"):
                                signal.evidence.pop(key, None)
                        skip_evidence = (direct_token_transfer_evidence(tx, receipt, signal.wallet)
                                         if passive_lookup else None)
                        if skip_evidence is not None:
                            signal.evidence.update({
                                "relay_lookup_skipped": True,
                                "relay_lookup_skip_reason": "direct_token_transfer",
                                "relay_lookup_skip_evidence": skip_evidence,
                            })
                        emit(store, signal)
                        observed = signal
                        if skip_evidence is not None:
                            stats["relay_lookup_skipped"] += 1
                            report("relay_lookup_skipped", source_event_id=signal.event_id,
                                   tx_hash=tx.hash, reason="direct_token_transfer",
                                   evidence=skip_evidence, live_trading=False)
                        if passive_lookup and skip_evidence is None:
                            try:
                                document = await relay_client.lookup_by_destination_hash(
                                    signal.tx_hash)
                                associated = relay_passive_buy(document, signal)
                                try:
                                    associated.evidence["local_execution_route"] = \
                                        await discover_v3_execution_route(
                                            rpc, receipt, associated.token_in,
                                            associated.token_out,
                                            associated.evidence.get(
                                                "actual_output_credit_raw"))
                                    stats["local_v3_routes_verified"] += 1
                                except (RpcError, ValueError) as exc:
                                    report("local_execution_route_not_in_receipt",
                                           source_event_id=associated.event_id,
                                           error_type=type(exc).__name__,
                                           active_discovery_deferred=True,
                                           live_trading=False)
                                emit(store, associated)
                                observed = associated
                                stats["relay_buy_associated"] += 1
                                stats["receipt_relay_buy_evidenced"] += 1
                                report("relay_buy_auto_associated",
                                       source_event_id=associated.event_id,
                                       relay_order_id=associated.evidence.get("relay_order_id"),
                                       live_trading=False)
                            except RelayNotReady:
                                stats["relay_lookup_pending"] += 1
                                defer_completion = True
                                continue
                            except (RelayApiError, RpcError, ValueError) as exc:
                                # Failing to obtain an attribution is not evidence
                                # that the delivery was not a purchase: a rate-limited
                                # or failed lookup and a half-written order all land
                                # here. Until 2026-09-18 this was terminal, so early
                                # lots whose delivery hit one stayed pending for good
                                # and froze every sell of that token. Queue the
                                # candidate for the same bounded retry as an
                                # unpublished order; exhaustion still leaves the
                                # delivery unattributed and copies nothing.
                                stats["relay_lookup_errors"] += 1
                                defer_completion = True
                                defer_reason = "relay_lookup_failed"
                                report("relay_buy_auto_association_deferred",
                                       source_event_id=signal.event_id,
                                       error_type=type(exc).__name__, live_trading=False)
                                continue
                        if (relay_client is not None
                                and signal.behavior == "SELL"
                                and signal.stage == "needs_review"
                                and signal.protocol in {"0x", "kyber"}
                                and signal.evidence.get("source_orchestrator") == "relay"
                                and "relay_sell_evidence_not_uniquely_closed"
                                in signal.reasons):
                            try:
                                document = await relay_client.lookup_requests_by_hash(
                                    signal.tx_hash)
                                confirmed = relay_confirmed_sell(document, signal)
                                try:
                                    confirmed.evidence["local_execution_route"] = \
                                        await discover_v3_execution_route(
                                            rpc, receipt, confirmed.token_in,
                                            confirmed.token_out,
                                            confirmed.evidence.get(
                                                "actual_output_credit_raw"))
                                    stats["local_v3_routes_verified"] += 1
                                except (RpcError, ValueError) as exc:
                                    report("local_execution_route_not_in_receipt",
                                           source_event_id=confirmed.event_id,
                                           error_type=type(exc).__name__,
                                           active_discovery_deferred=True,
                                           live_trading=False)
                                emit(store, confirmed)
                                observed = confirmed
                                stats["relay_sell_confirmed"] += 1
                                stats["receipt_relay_sell_evidenced"] += 1
                                report("relay_sell_order_confirmed",
                                       source_event_id=confirmed.event_id,
                                       relay_order_id=confirmed.evidence.get("relay_order_id"),
                                       swap_event_count_in_scope=confirmed.evidence.get(
                                           "swap_event_count_in_scope"),
                                       live_trading=False)
                            except RelayNotReady:
                                stats["relay_lookup_pending"] += 1
                                defer_completion = True
                                continue
                            except (RelayApiError, RpcError, ValueError) as exc:
                                # Same bounded retry as the passive BUY lookup above.
                                stats["relay_lookup_errors"] += 1
                                defer_completion = True
                                defer_reason = "relay_lookup_failed"
                                report("relay_sell_confirmation_deferred",
                                       source_event_id=signal.event_id,
                                       error_type=type(exc).__name__, live_trading=False)
                                continue
                        route_before = observed.evidence.get("local_execution_route")
                        await safe_paper_observe(observed)
                        route_after = observed.evidence.get("local_execution_route")
                        if (not isinstance(route_before, dict)
                                and isinstance(route_after, dict)
                                and route_after.get("route_discovery")
                                == "v3_factory_bounded_best_quote"):
                            stats["local_v3_routes_verified"] += 1
                            report(
                                "local_v3_route_discovered",
                                source_event_id=observed.event_id,
                                verified_pool=route_after.get("verified_pool"),
                                fee=(route_after.get("fees") or [None])[0],
                                block_number=route_after.get(
                                    "verified_block_number"),
                                amount_in_raw=route_after.get(
                                    "route_discovery_amount_in_raw"),
                                live_trading=False)
                        stats["receipt_signals"] += 1
                        if signal.behavior == "UNKNOWN":
                            stats["receipt_unknown"] += 1
                        if signal.stage == "needs_review":
                            stats["receipt_needs_review"] += 1
                        if signal.stage == "swap_evidenced":
                            stats["receipt_swap_evidenced"] += 1
                        if signal.stage == "relay_sell_evidenced":
                            stats["receipt_relay_sell_evidenced"] += 1
                        if signal.stage == "relay_buy_evidenced":
                            stats["receipt_relay_buy_evidenced"] += 1
                    stats["receipts"] += 1
                    if defer_completion:
                        attempts, delay = store.retry_candidate(tx.hash, defer_reason)
                        if delay is None:
                            stats["candidate_retry_exhausted"] += 1
                            report("relay_lookup_retry_exhausted", tx_hash=tx.hash,
                                   reason=defer_reason, attempts=attempts, live_trading=False)
                        else:
                            stats["candidate_retries"] += 1
                    else:
                        store.complete_candidate(tx.hash, number(receipt.get("blockNumber", 0)),
                                                 receipt.get("blockHash"))
            except RpcError:
                stats["worker_errors"] += 1
                attempts, delay = store.retry_candidate(tx.hash, "rpc_error")
                if delay is None:
                    stats["candidate_retry_exhausted"] += 1
                else:
                    stats["candidate_retries"] += 1
                report("candidate_error", tx_hash=tx.hash, error_type="RpcError",
                       attempts=attempts, retry_in_seconds=delay, retry_exhausted=delay is None)
            except Exception as exc:
                stats["worker_errors"] += 1
                store.fail_candidate(tx.hash, type(exc).__name__)
                report("candidate_error", tx_hash=tx.hash, error_type=type(exc).__name__)
            finally:
                timings.observe("candidate_processing_ms", time.monotonic() - started)
                queue.task_done()

    def critical_fail_closed():
        """A stuck ledger loop must stop live signing before the loud exit."""
        if live_pipelines or early_trial_id:
            trip_execution_stop()

    async def dispatcher_iteration():
        wake_dispatcher.clear()
        if not dispatch_pending(store, queue, stats):
            try:
                await asyncio.wait_for(wake_dispatcher.wait(), timeout=0.25)
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(0)

    async def heartbeat_iteration():
        await asyncio.sleep(5)
        stats["ledger_reconnections"] = getattr(store.connection, "reconnections", 0)
        feed_healthy = health.healthy()
        report("health", healthy=feed_healthy, feed_healthy=feed_healthy,
               execution=execution_health(bool(live_pipelines)),
               queued=queue.qsize(), counters=dict(stats),
               relay_http_timings=list(getattr(relay_client, "timings", ()))[-8:],
               rpc_http_timings=list(getattr(getattr(rpc, "transport", None), "timings", ()))[-8:],
               rpc_call_timings=list(getattr(rpc, "timings", ()))[-8:],
               aggregator_http_timings={name: list(client.transport.timings)[-8:]
                   for name, client in (quoter.aggregators.items() if paper_config else [])},
               early_feed_counters=dict(early_lane.stats) if early_lane else {},
               deployment_verification=deployment_monitor.status() if deployment_monitor else None,
               candidate_states=store.candidate_counts(), chain_cursor=store.chain_cursor(),
               latency_ms=timings.summary(), coverage=coverage_summary(stats))

    async def backfill():
        def progress(candidates, passive_candidates):
            stats["backfill_blocks"] += 1
            stats["backfill_candidates"] += candidates
            stats["backfill_passive_candidates"] += passive_candidates
            if candidates:
                wake_dispatcher.set()

        scanner = BlockScanner(rpc, store, backfill_watchlist, args.confirmations,
                               args.backfill_batch, progress=progress)
        while True:
            try:
                result = await scanner.scan_once()
                if result.initialized:
                    report("backfill_initialized", chain_cursor=store.chain_cursor(),
                           confirmations=args.confirmations)
            except ReorgDetected as exc:
                stats["reorg_detected"] += 1
                if early_trial_id:
                    trip_execution_stop()
                try:
                    # Range scanning stores headers only at range boundaries and
                    # hit blocks, so the automatic rewind must be allowed to reach
                    # back one full range to find a stored common ancestor.
                    resolution = await scanner.reconcile_reorg(
                        max_depth=max(64, args.backfill_batch + 1))
                    stats["reorg_orphaned_signals"] += resolution.orphaned_signals
                    stats["reorg_candidates_requeued"] += resolution.candidates_requeued
                    report("reorg_reconciled", common_ancestor=resolution.common_ancestor,
                           orphaned_signals=resolution.orphaned_signals,
                           candidates_requeued=resolution.candidates_requeued)
                    if resolution.candidates_requeued:
                        wake_dispatcher.set()
                except ReorgDetected as deep:
                    report("backfill_halted", reason="reorg_depth_exceeded", detail=str(deep))
                    return
            except (RpcError, ValueError) as exc:
                stats["backfill_errors"] += 1
                report("backfill_error", error_type=type(exc).__name__)
            await asyncio.sleep(args.backfill_interval)

    async def receive():
        while True:
            health.reset()
            try:
                async with websockets.connect(feed_url, open_timeout=15, max_size=16 * 1024 * 1024,
                                              max_queue=16, ping_interval=20, proxy=None) as ws:
                    stats["connections"] += 1
                    report("feed_connected", live_trading=False)
                    async for frame in ws:
                        stats["frames"] += 1
                        try:
                            for raw, metadata in envelopes(frame, health):
                                if health.gap:
                                    raise DecodeError("feed sequence gap")
                                if not metadata["fresh"]:
                                    stats["stale_transactions_skipped"] += 1
                                    continue
                                try:
                                    tx = decode_raw(raw, observation_source="feed", **metadata)
                                except DecodeError:
                                    stats["unsupported_or_invalid_transactions"] += 1
                                    continue
                                stats["decoded_transactions"] += 1
                                # Raw address matching only widens the candidate set.
                                # Direct senders are always recovered from signatures.
                                if not relevant(tx, watchlist, watched_bytes):
                                    continue
                                if store.put_candidate(tx):
                                    stats["candidates"] += 1
                                    wake_dispatcher.set()
                                    if early_lane:
                                        try:
                                            early_lane.submit(tx)
                                        except Exception as exc:
                                            stats["early_feed_enqueue_errors"] += 1
                                            report("early_feed_enqueue_failed", tx_hash=tx.hash,
                                                   error_type=type(exc).__name__,
                                                   strict_fallback_preserved=True)
                                else:
                                    stats["candidate_duplicates"] += 1
                        except DecodeError:
                            stats["frame_errors"] += 1
                            raise
            except (websockets.WebSocketException, OSError, DecodeError):
                stats["reconnections"] += 1
                health.reset()
                report("feed_reconnecting", reason="connection_or_frame_error")
                await asyncio.sleep(1)

    await recover_paper_reservations()
    if early_lane:
        await deployment_monitor.start()
        try:
            early_lane.start()
        except Exception:
            await deployment_monitor.close()
            raise
    # Arc ingestion runs inside this same process rather than beside it: the
    # decision path already selects policies by wallet AND chain, and execution
    # already resolves its RPC, targets and broadcaster per chain, so a second
    # chain needs a second source, not a second copytrader.
    arc_tasks = []
    if getattr(args, "arc", False):
        arc_policies = [policy for policy in (paper_config.relationships if paper_config else ())
                        if policy.chain_id == ARC.chain_id]
        if not arc_policies:
            raise ValueError("--arc requires an enabled relationship on chain 5042")
        arc_ws_url = os.environ.get("ARC_WS_URL")
        if not arc_ws_url:
            raise ValueError("ARC_WS_URL is required for Arc ingestion")
        arc_queue = asyncio.Queue(maxsize=256)

        def arc_on_signal(signal):
            try:
                arc_queue.put_nowait(signal)
            except asyncio.QueueFull:
                stats["arc_queue_drops"] += 1
                report("arc_decision_queue_full", source_event_id=signal.event_id)

        def arc_status(event, details):
            report(event, **details, chain_id=ARC.chain_id)

        async def arc_decider():
            # Serialized: one Arc decision at a time, never interleaved.
            while True:
                signal = await arc_queue.get()
                try:
                    await safe_paper_observe(signal)
                except Exception as exc:
                    stats["arc_decision_errors"] += 1
                    report("arc_decision_error", source_event_id=signal.event_id,
                           error_type=type(exc).__name__)
                finally:
                    arc_queue.task_done()

        report("arc_ingestion_started", chain_id=ARC.chain_id,
               relationships=[policy.relationship_id for policy in arc_policies],
               live_trading=any(policy.run_mode != "paper" for policy in arc_policies))
        arc_tasks = [
            asyncio.create_task(observe_arc(
                chain_rpcs[ARC.chain_id], arc_ws_url, store, watchlist,
                arc_on_signal, arc_status,
                backfill_interval=max(args.backfill_interval, 1.0),
                backfill_batch=500, relay_client=RelayPublicClient())),
            asyncio.create_task(arc_decider()),
        ]
    workers = [asyncio.create_task(worker()) for _ in range(args.workers)]
    dispatch_task = asyncio.create_task(supervise_critical_task(
        "dispatcher", dispatcher_iteration, stats, critical_fail_closed))
    backfill_task = asyncio.create_task(backfill())
    beat = asyncio.create_task(supervise_critical_task(
        "heartbeat", heartbeat_iteration, stats, critical_fail_closed))
    receiver = asyncio.create_task(receive())
    try:
        report("monitor_started", wallets=len(watchlist),
               early_feed_max_age_seconds=EARLY_FEED_MAX_AGE_SECONDS if early_lane else None,
               deployment_verification=deployment_monitor.status() if deployment_monitor else None,
               backfill_wallets=len(backfill_watchlist),
               backfill_range_blocks=args.backfill_batch, seconds=args.seconds,
               live_trading=bool(live_pipelines))
        # Waiting on every core task (not only the receiver) turns the silent
        # death of the dispatcher, heartbeat or a worker into a loud exit.
        core_tasks = [receiver, dispatch_task, beat, *workers, *arc_tasks]
        done, _ = await asyncio.wait(
            core_tasks, timeout=args.seconds if args.seconds > 0 else None,
            return_when=asyncio.FIRST_COMPLETED)
        for finished in done:
            if finished.exception() is not None:
                raise finished.exception()
        try:
            await asyncio.wait_for(queue.join(), timeout=15)
        except asyncio.TimeoutError:
            report("drain_timeout", unfinished=queue.qsize())
    finally:
        for task in [receiver, dispatch_task, backfill_task, beat, *workers, *arc_tasks]:
            task.cancel()
        for task in live_tracking_tasks:
            task.cancel()
        await asyncio.gather(receiver, dispatch_task, backfill_task, beat, *workers,
                             return_exceptions=True)
        if live_tracking_tasks:
            await asyncio.gather(*live_tracking_tasks, return_exceptions=True)
        if early_lane:
            try:
                await early_lane.close()
            finally:
                await deployment_monitor.close()
        candidate_states = store.candidate_counts()
        chain_cursor = store.chain_cursor()
        if relay_client is not None:
            relay_client.close()
        if hasattr(rpc, "close"):
            rpc.close()
        for chain_id, chain_rpc in list(chain_rpcs.items()):
            if chain_id != CHAIN_ID and hasattr(chain_rpc, "close"):
                chain_rpc.close()
        if paper_config:
            for client in quoter.aggregators.values():
                if hasattr(client, "close"):
                    client.close()
            for sender in broadcasters.values():
                if hasattr(sender, "close"):
                    sender.close()
        store.close()
        report("monitor_finished", counters=dict(stats), candidate_states=candidate_states,
               early_feed_counters=dict(early_lane.stats) if early_lane else {},
               chain_cursor=chain_cursor, latency_ms=timings.summary(),
               coverage=coverage_summary(stats), live_trading=bool(live_pipelines))


async def reconcile_reorg(args):
    """Explicitly recover a canonical cursor when the automatic 64-block search halted."""
    load_endpoint_env()
    rpc = ReadOnlyRpc(os.environ.get(
        "ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    if number(await rpc.call("eth_chainId")) != CHAIN_ID:
        raise ValueError("RPC is connected to the wrong chain")
    store = runtime_store(args)
    try:
        scanner = BlockScanner(rpc, store, {}, confirmations=0)
        resolution = await scanner.reconcile_reorg(max_depth=args.max_depth)
        report("reorg_reconciled_manual", common_ancestor=resolution.common_ancestor,
               orphaned_signals=resolution.orphaned_signals,
               candidates_requeued=resolution.candidates_requeued,
               searched_max_depth=args.max_depth, live_trading=False)
    finally:
        store.close()


async def paper_mark(args):
    """Create read-only, block-pinned valuation snapshots for all supported open lots."""
    load_endpoint_env()
    config = runtime_paper_config(args)
    rpc = ReadOnlyRpc(os.environ.get(
        "ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    if number(await rpc.call("eth_chainId")) != CHAIN_ID:
        raise ValueError("RPC is connected to the wrong chain")
    store = runtime_store(args)
    marked = rejected = 0
    try:
        quoter = LiveQuoter(rpc)
        for lot_id in store.open_paper_position_ids():
            position = store.paper_position(lot_id)
            source = store.signal(position["source_event_id"])
            attribution = position.get("attribution", {})
            matching = [policy for policy in config.policies_for(source.wallet, source.chain_id)] \
                if source is not None else []
            matching = [policy for policy in matching
                        if policy.relationship_id == attribution.get("relationship_id")
                        and policy.follower_wallet == attribution.get("follower_wallet")]
            if (source is None or len(matching) != 1
                    or (source is not None and scope_reason(
                        source, matching[0].allowed_protocols,
                        matching[0].allowed_assets,
                        matching[0].allowed_routes) is not None)):
                rejected += 1
                report("paper_mark_rejected", lot_id=lot_id,
                       reason="source_signal_not_in_allowed_scope", live_trading=False)
                continue
            try:
                mark = await PaperValuator(
                    store, quoter, matching[0].quote_policy,
                    matching[0].route_definitions).mark(lot_id, source)
                marked += 1
                report("paper_marked", lot_id=lot_id, mark_id=mark.mark_id,
                       block_number=mark.block_number,
                       gross_value_raw=mark.gross_value_raw,
                       unrealized_pnl_raw=mark.unrealized_pnl_raw,
                       gas_cost_wei=mark.gas_cost_wei, live_trading=False)
            except (RpcError, ValueError) as exc:
                rejected += 1
                report("paper_mark_rejected", lot_id=lot_id,
                       reason=type(exc).__name__, live_trading=False)
        report("paper_mark_finished", marked=marked, rejected=rejected,
               live_trading=False)
    finally:
        store.close()


async def execution_track(args):
    """Observe one externally supplied execution hash; never submit a transaction."""
    load_endpoint_env()
    rpc = ReadOnlyRpc(os.environ.get(
        "ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    if number(await rpc.call("eth_chainId")) != CHAIN_ID:
        raise ValueError("RPC is connected to the wrong chain")
    store = runtime_store(args)
    try:
        observation = await ReadOnlyExecutionTracker(store, rpc).observe(
            args.proposal_id, args.tx_hash, args.replaces_tx_hash)
        print(json.dumps({
            "proposal_id": args.proposal_id,
            "tx_hash": observation.tx_hash,
            "status": observation.status,
            "block_number": observation.block_number,
            "block_hash": observation.block_hash,
            "read_only": True,
            "broadcast_performed": False,
            "copy_eligible": False,
            "live_trading": False,
        }, ensure_ascii=False, sort_keys=True))
    finally:
        store.close()


def parser():
    root = argparse.ArgumentParser(
        description="Smart-money observer with separately gated mainnet execution tools")
    commands = root.add_subparsers(dest="command", required=True)
    replay_parser = commands.add_parser("replay", help="Replay captured transactions without network access")
    replay_parser.add_argument("--fixtures", default="data/transaction_examples.json")
    replay_parser.add_argument("--accounts", default="data/account_codes.json")
    replay_parser.add_argument("--extra-fixture", action="append")
    replay_parser.add_argument("--watchlist", default="data/fomo_watchlist.csv")
    replay_parser.add_argument("--db", default="var/replay.sqlite3")
    monitor_parser = commands.add_parser("monitor", help="Observe live feed and query receipts; read-only")
    monitor_parser.add_argument("--watchlist", default="data/fomo_watchlist.csv")
    monitor_parser.add_argument("--db", default="var/observer.sqlite3")
    monitor_parser.add_argument("--seconds", type=float, default=60, help="Duration; 0 runs until interrupted")
    monitor_parser.add_argument("--workers", type=int, default=2)
    monitor_parser.add_argument("--queue-size", type=int, default=256)
    monitor_parser.add_argument("--confirmations", type=int, default=2)
    monitor_parser.add_argument("--backfill-batch", type=int, default=2000,
                                help="Blocks per address-filtered log range scan")
    monitor_parser.add_argument("--backfill-interval", type=float, default=1.0)
    monitor_parser.add_argument(
        "--relay-auto-associate", action="store_true",
        help="Read Relay public order evidence for passive delivery candidates")
    monitor_parser.add_argument(
        "--early-feed-evidence", action="store_true",
        help="Collect early intent evidence using the SAME Feed connection; does not enable early trading")
    arc_parser = commands.add_parser(
        "arc-monitor", help="Observe Arc v4 Swap logs over WSS; read-only")
    arc_parser.add_argument("--watchlist", default="data/fomo_watchlist.csv")
    arc_parser.add_argument("--db", default="var/arc-observer.sqlite3")
    arc_parser.add_argument(
        "--no-relay", action="store_true",
        help="skip Relay order lookups for cross-chain deliveries (read-only either way)")
    arc_parser.add_argument(
        "--seconds", type=float, default=60,
        help="Duration; 0 runs until interrupted")
    arc_parser.add_argument(
        "--backfill-interval", type=float, default=5.0,
        help="Seconds between bounded Arc log catch-up scans")
    arc_parser.add_argument(
        "--backfill-batch", type=int, default=500,
        help="Maximum Arc blocks per catch-up scan")
    arc_paper_parser = commands.add_parser(
        "arc-paper",
        help="Copy Arc smart money in PAPER mode; no signing or broadcasting exists here")
    arc_paper_parser.add_argument("--watchlist", default="data/fomo_watchlist.csv")
    arc_paper_parser.add_argument(
        "--no-relay", action="store_true",
        help="skip Relay order lookups for cross-chain deliveries")
    arc_paper_parser.add_argument(
        "--seconds", type=float, default=0, help="Duration; 0 runs until interrupted")
    arc_paper_parser.add_argument("--backfill-interval", type=float, default=15.0)
    arc_paper_parser.add_argument("--backfill-batch", type=int, default=500)
    arc_paper_parser.add_argument(
        "--paper-cycle-action", choices=("reuse", "reset", "auto"), default="reuse")
    arc_paper_parser.add_argument("--paper-cycle-id")
    arc_paper_parser.add_argument("--paper-cycle-reason")
    # Exit-gate thresholds. Arc meme tokens commonly carry a transaction tax and
    # a sell side that will not route at all, so both are operator-tunable.
    arc_paper_parser.add_argument("--max-tax-bps", type=int, default=500)
    arc_paper_parser.add_argument("--max-round-trip-loss-bps", type=int, default=1500)
    arc_paper_parser.add_argument("--probe-amount-raw", type=int, default=1_000_000,
                                  help="Exit-gate probe size in Arc USDC raw units")
    monitor_source = monitor_parser.add_mutually_exclusive_group()
    monitor_source.add_argument("--paper-config")
    monitor_source.add_argument("--paper-mysql", action="store_true",
                                help="Load enabled copy relationships from local MySQL")
    monitor_parser.add_argument("--paper-cycle-action", choices=("reuse", "reset"))
    monitor_parser.add_argument("--paper-cycle-id")
    monitor_parser.add_argument("--paper-cycle-reason")
    run_parser = commands.add_parser(
        "run",
        help="Run enabled MySQL copy relationships with persistent MySQL ledger")
    run_parser.add_argument(
        "--seconds", type=float, default=0,
        help="Duration; 0 runs until interrupted")
    run_parser.add_argument(
        "--arc", action="store_true",
        help="Also ingest Arc (chain 5042) in this process for its relationships")
    run_parser.add_argument(
        "--early-feed-evidence", action="store_true",
        help="Enable in-process evidence lane only, not early execution")
    run_parser.add_argument("--early-trial-id",
        help="Select an explicitly operator-started bounded trial; never creates or renews one")
    run_parser.add_argument("--legacy-preflight", action="store_true",
        help="Disable process-local preflight reuse; retain independent sign/review RPC checks")
    run_parser.add_argument("--backfill-interval", type=float, default=1.0,
        help="Seconds between backfill catch-up scans; higher means fewer RPC calls "
             "(backfill only tops up the real-time Feed, so a larger value is cheap)")
    run_parser.set_defaults(
        watchlist="data/fomo_watchlist.csv", db="var/observer.sqlite3",
        workers=2, queue_size=256, confirmations=2, backfill_batch=2000,
        relay_auto_associate=True,
        paper_config=None, paper_mysql=True,
        paper_cycle_action="auto", paper_cycle_id=None,
        paper_cycle_reason=None, ledger_mysql=True,
    )
    reconcile_parser = commands.add_parser(
        "reconcile-reorg",
        help="Explicitly search deeper canonical history after automatic reorg recovery halts",
    )
    reconcile_parser.add_argument("--db", default="var/observer.sqlite3")
    reconcile_parser.add_argument("--max-depth", type=int, required=True,
                                  help="Maximum stored blocks to compare (65..100000)")
    cycle_parser = commands.add_parser(
        "paper-cycle", help="Explicitly reuse or reset the manual paper budget cycle")
    cycle_parser.add_argument("action", choices=("reuse", "reset"))
    cycle_source = cycle_parser.add_mutually_exclusive_group(required=True)
    cycle_source.add_argument("--config")
    cycle_source.add_argument("--paper-mysql", action="store_true")
    cycle_parser.add_argument("--db", default="var/observer.sqlite3")
    cycle_parser.add_argument("--cycle-id")
    cycle_parser.add_argument("--reason")
    mark_parser = commands.add_parser(
        "paper-mark", help="Record block-pinned read-only marks for open paper positions")
    mark_source = mark_parser.add_mutually_exclusive_group(required=True)
    mark_source.add_argument("--config")
    mark_source.add_argument("--paper-mysql", action="store_true")
    mark_parser.add_argument("--db", default="var/observer.sqlite3")
    relationship_parser = commands.add_parser(
        "relationships-import",
        help="Import CSV smart wallets as disabled MySQL copy relationships")
    relationship_parser.add_argument("--follower-wallet", required=True)
    relationship_parser.add_argument("--follower-label", required=True)
    relationship_parser.add_argument("--watchlist", default="data/fomo_watchlist.csv")
    relationship_parser.add_argument("--template", default="config/paper.example.json")
    relationship_status_parser = commands.add_parser(
        "relationship-status",
        help="Show one enabled relationship and its live-binding snapshot without secrets")
    relationship_status_parser.add_argument("--relationship-id", required=True)
    approval_parser = commands.add_parser(
        "mainnet-approve-usdg",
        help="Broadcast one bounded 200x-budget USDG approval for an enabled live relationship")
    approval_parser.add_argument("--relationship-id", required=True)
    paper_export_parser = commands.add_parser(
        "paper-export", help="Export attributed paper fills as JSONL")
    paper_export_parser.add_argument("--db", default="var/observer.sqlite3")
    export_parser = commands.add_parser("export", help="Export deduplicated SQLite signals as JSONL")
    export_parser.add_argument("--db", default="var/observer.sqlite3")
    execution_audit_parser = commands.add_parser(
        "execution-audit", help="Read-only audit of execution/nonce/attempt state")
    execution_audit_parser.add_argument("--db", default="var/observer.sqlite3")
    execution_track_parser = commands.add_parser(
        "execution-track", help="Read-only RPC tracking for an externally supplied hash")
    execution_track_parser.add_argument("--db", default="var/observer.sqlite3")
    execution_track_parser.add_argument("--proposal-id", required=True)
    execution_track_parser.add_argument("--tx-hash")
    execution_track_parser.add_argument("--replaces-tx-hash")
    key_status_parser = commands.add_parser(
        "key-status", help="Read public metadata for one offline key record")
    key_status_parser.add_argument("--wallet", required=True)
    ledger_migrate_parser = commands.add_parser(
        "ledger-migrate", help="Migrate a stopped SQLite ledger to business MySQL")
    ledger_migrate_parser.add_argument("--sqlite", required=True)
    ledger_migrate_parser.add_argument("--confirm-source-sha256", required=True)
    relay_associate_parser = commands.add_parser(
        "relay-associate", help="Offline association of a saved Relay response with a passive signal")
    relay_associate_parser.add_argument("--event-id", required=True)
    relay_associate_parser.add_argument("--document", required=True)
    relay_associate_parser.add_argument("--db", default="var/observer.sqlite3")
    relay_associate_parser.add_argument("--ledger-mysql", action="store_true")
    relay_requeue_parser = commands.add_parser(
        "relay-requeue-pending",
        help="Re-queue strict-channel candidates behind pending early lots; dry run without --confirm")
    relay_requeue_parser.add_argument("--confirm", action="store_true")
    relay_requeue_parser.add_argument("--db", default="var/observer.sqlite3")
    relay_requeue_parser.add_argument("--ledger-mysql", action="store_true")
    trial_start_parser = commands.add_parser("early-trial-start",
        help="Operator-only: initialize a bounded window while the emergency stop remains active")
    trial_start_parser.add_argument("--trial-id", required=True)
    trial_start_parser.add_argument("--follower", required=True)
    trial_start_parser.add_argument("--relationships", nargs="+", required=True)
    trial_start_parser.add_argument("--confirm-risk-checklist", action="store_true")
    trial_start_parser.set_defaults(ledger_mysql=True)
    for command_parser in (
            replay_parser, monitor_parser, reconcile_parser, cycle_parser,
            mark_parser, paper_export_parser, export_parser,
            execution_audit_parser, execution_track_parser):
        command_parser.add_argument(
            "--ledger-mysql", action="store_true",
            help="Use the business MySQL runtime ledger instead of SQLite")
    return root


def main():
    args = parser().parse_args()
    try:
        if args.command == "replay":
            replay(args)
        elif args.command == "arc-monitor":
            if (args.seconds < 0 or not 0.5 <= args.backfill_interval <= 60
                    or not 1 <= args.backfill_batch <= 2000):
                raise ValueError("invalid Arc monitor limits")
            with runtime_instance_lock(
                    "var/sm-copy-arc.instance.lock", "var/sm-copy-arc.pid"):
                asyncio.run(arc_monitor(args))
        elif args.command == "arc-paper":
            if (args.seconds < 0 or not 0.5 <= args.backfill_interval <= 60
                    or not 1 <= args.backfill_batch <= 2000):
                raise ValueError("invalid Arc paper limits")
            if args.paper_cycle_action == "reset" and (
                    not args.paper_cycle_id or not args.paper_cycle_reason):
                raise ValueError("paper reset requires cycle id and reason")
            if args.paper_cycle_action != "reset" and (
                    args.paper_cycle_id or args.paper_cycle_reason):
                raise ValueError("only a paper reset accepts cycle id and reason")
            # Its own lock: this lane writes the shared ledger under Arc-only
            # scopes and must not be started twice, but it is not the Robinhood
            # feed and does not contend with it.
            with runtime_instance_lock(
                    "var/sm-copy-arc-paper.instance.lock", "var/sm-copy-arc-paper.pid"):
                asyncio.run(arc_paper(args))
        elif args.command in {"monitor", "run"}:
            if (args.seconds < 0 or not 1 <= args.workers <= 8 or not 1 <= args.queue_size <= 10000
                    or args.confirmations < 0 or not 1 <= args.backfill_batch <= MAX_RANGE_BLOCKS
                    or not 0.1 <= args.backfill_interval <= 60):
                raise ValueError("invalid monitor limits")
            if args.paper_config or args.paper_mysql:
                if args.paper_cycle_action == "reset" and (
                        not args.paper_cycle_id or not args.paper_cycle_reason):
                    raise ValueError("paper reset requires cycle id and reason")
                if args.paper_cycle_action == "reuse" and (
                        args.paper_cycle_id or args.paper_cycle_reason):
                    raise ValueError("paper reuse does not accept cycle id or reason")
                if args.paper_cycle_action == "auto" and (
                        args.paper_cycle_id or args.paper_cycle_reason):
                    raise ValueError("automatic cycle does not accept cycle id or reason")
                if args.paper_cycle_action is None:
                    raise ValueError("paper mode requires explicit cycle action")
            elif any((args.paper_cycle_action, args.paper_cycle_id, args.paper_cycle_reason)):
                raise ValueError("paper cycle options require --paper-config or --paper-mysql")
            with runtime_instance_lock():
                asyncio.run(monitor(args))
        elif args.command == "early-trial-start":
            if not args.confirm_risk_checklist:
                raise ValueError("operator risk-checklist confirmation required")
            from pathlib import Path
            from .execution_controls import _risk_acceptance
            if not Path(os.environ.get("SMART_MONEY_EMERGENCY_STOP_FILE", "var/EXECUTION_STOP")).exists():
                raise ValueError("stop file must remain active while initializing a trial")
            config = load_mysql_paper_config()
            selected = [p for p in config.relationships if p.relationship_id in args.relationships]
            if (len(selected) != len(set(args.relationships)) or len(set(args.relationships)) != len(args.relationships)
                    or any(p.follower_wallet != args.follower or p.execution_providers not in
                           (("kyber",), ("zeroex",), ("zeroex", "kyber"))
                           or p.trigger_mode != "evidenced" or p.run_mode != "mainnet_live" for p in selected)):
                raise ValueError("operator trial scope must match current enabled aggregator-only live policies")
            for p in selected:
                _risk_acceptance(p.follower_wallet, p.relationship_id, p.snapshot_hash)
            store = runtime_store(args)
            try:
                result = store.start_early_trial(args.trial_id, args.follower, args.relationships)
                print(json.dumps({**result, "broadcast_performed": False,
                                  "stop_file_cleared": False, "budget_reset": False}))
            finally:
                store.close()
        elif args.command == "reconcile-reorg":
            require_existing_sqlite(args)
            if not 65 <= args.max_depth <= 100000:
                raise ValueError("max-depth must be between 65 and 100000")
            asyncio.run(reconcile_reorg(args))
        elif args.command == "paper-cycle":
            if args.action == "reset" and (not args.cycle_id or not args.reason):
                raise ValueError("reset requires --cycle-id and --reason")
            if args.action == "reuse" and (args.cycle_id or args.reason):
                raise ValueError("reuse does not accept --cycle-id or --reason")
            paper_cycle(args)
        elif args.command == "paper-mark":
            require_existing_sqlite(args)
            asyncio.run(paper_mark(args))
        elif args.command == "paper-export":
            require_existing_sqlite(args)
            store = runtime_store(args)
            try:
                for row in store.paper_trades():
                    print(json.dumps(row, ensure_ascii=False, sort_keys=True))
            finally:
                store.close()
        elif args.command == "execution-audit":
            require_existing_sqlite(args)
            store = runtime_store(args)
            try:
                print(json.dumps(store.execution_audit(), ensure_ascii=False, sort_keys=True))
            finally:
                store.close()
        elif args.command == "execution-track":
            require_existing_sqlite(args)
            if args.replaces_tx_hash and not args.tx_hash:
                raise ValueError("replacement tracking requires --tx-hash")
            asyncio.run(execution_track(args))
        elif args.command == "key-status":
            print(json.dumps(key_record_status(args.wallet), ensure_ascii=False,
                             sort_keys=True))
        elif args.command == "ledger-migrate":
            print(json.dumps(migrate_sqlite_ledger(
                args.sqlite, args.confirm_source_sha256), ensure_ascii=False,
                sort_keys=True))
        elif args.command == "relay-associate":
            relay_associate(args)
        elif args.command == "relay-requeue-pending":
            relay_requeue_pending(args)
        elif args.command == "relationships-import":
            inserted, skipped = import_watchlist_relationships(
                args.follower_wallet, args.follower_label, args.watchlist, args.template)
            report("relationships_imported", inserted=inserted, skipped_existing=skipped,
                   enabled=False, live_trading=False)
        elif args.command == "relationship-status":
            relationship_status(args)
        elif args.command == "mainnet-approve-usdg":
            asyncio.run(mainnet_approve_usdg(args))
        else:
            require_existing_sqlite(args)
            store = runtime_store(args)
            try:
                for row in store.rows():
                    print(json.dumps(row, ensure_ascii=False))
            finally:
                store.close()
    except KeyboardInterrupt:
        return
    except (OSError, ValueError, RpcError) as exc:
        report("error", error_type=type(exc).__name__, detail=str(exc))
        raise SystemExit(1)
