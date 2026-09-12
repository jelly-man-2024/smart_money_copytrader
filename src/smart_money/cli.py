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

from .account_state import prestate_implementations
from .backfill import BlockScanner, ReorgDetected, relevant
from .config import load_endpoint_env
from .decode import Decoder
from .feed import DecodeError, FeedHealth, decode_raw, envelopes
from .execution_receipts import ReadOnlyExecutionTracker
from .key_source import key_record_status
from .ledger_migration import migrate_sqlite_ledger
from .models import Transaction, number
from .mysql_config import import_watchlist_relationships, load_mysql_paper_config
from .mysql_store import MySqlStore
from .native_flows import verify_native_flows
from .paper import PaperEngine, PaperExecutor, PaperValuator, budget_bucket, scope_reason
from .paper_config import load_paper_config
from .pools import verify_signal_pools
from .quotes import LiveQuoter
from .receipts import enrich
from .registry import CHAIN_ID, ENTRYPOINT, delegation, load_watchlist, snapshot_delegations
from .rpc import ReadOnlyRpc, RpcError
from .store import Store


def report(event: str, **details):
    print(json.dumps({"event": event, **details}, ensure_ascii=False), file=sys.stderr, flush=True)


def emit(store, signal):
    if store.put(signal):
        print(json.dumps(signal.to_dict(), ensure_ascii=False), flush=True)


def runtime_paper_config(args):
    if getattr(args, "paper_mysql", False):
        return load_mysql_paper_config()
    path = getattr(args, "paper_config", None) or getattr(args, "config", None)
    return load_paper_config(path) if path else None


def runtime_store(args):
    """Choose exactly one ledger backend; never dual-write."""
    return MySqlStore() if getattr(args, "ledger_mysql", False) else Store(args.db)


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
        "unknown_fraction": round(int(stats.get("receipt_unknown", 0)) / total, 6) if total else None,
    }


def dispatch_pending(store, queue, stats) -> int:
    """Move only durable candidates into the bounded in-memory work queue."""
    available = queue.maxsize - queue.qsize()
    candidates = store.claim_candidates(available)
    queued_at = time.monotonic()
    for tx in candidates:
        queue.put_nowait((tx, queued_at))
        stats["candidates_dispatched"] += 1
    return len(candidates)


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


async def monitor(args):
    load_endpoint_env()
    watchlist = load_watchlist(args.watchlist)
    watched_bytes = [bytes.fromhex(a[2:]) for a in watchlist]
    rpc = ReadOnlyRpc(os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    feed_url = os.environ.get("ROBINHOOD_FEED_URL", "wss://feed.mainnet.chain.robinhood.com")
    if not feed_url.startswith("wss://"):
        raise ValueError("feed must use WSS")
    if number(await rpc.call("eth_chainId")) != CHAIN_ID:
        raise ValueError("RPC is connected to the wrong chain")
    decoder = Decoder(watchlist)
    queue = asyncio.Queue(maxsize=args.queue_size)
    health = FeedHealth()
    stats = Counter()
    store = runtime_store(args)
    paper_config = runtime_paper_config(args)
    paper_engines = {}
    paper_executor = None
    if paper_config:
        if not set(paper_config.wallets) <= set(watchlist):
            raise ValueError("paper config wallets must be present in the observer watchlist")
        if args.paper_cycle_action == "reset":
            store.start_paper_budget_cycle(args.paper_cycle_id, args.paper_cycle_reason)
            for policy in paper_config.relationships:
                for bucket, limit in policy.budget_limits.items():
                    store.configure_paper_budget(policy.ledger_scope, bucket, limit)
        elif args.paper_cycle_action == "reuse":
            if store.active_paper_budget_cycle() is None:
                raise ValueError("no active paper budget cycle to reuse")
            for policy in paper_config.relationships:
                for bucket, limit in policy.budget_limits.items():
                    current = store.paper_budget(policy.ledger_scope, bucket)
                    if current is None or current["limit_raw"] != limit:
                        raise ValueError("active paper budget does not match config")
        else:
            raise ValueError("paper mode requires explicit --paper-cycle-action")
        quoter = LiveQuoter(rpc)
        paper_executor = {}
        for policy in paper_config.relationships:
            paper_executor[policy.ledger_scope] = PaperExecutor(
                store, quoter, policy.quote_policy)
            for mode in (policy.trigger_mode, *policy.shadow_trigger_modes):
                paper_engines[(mode, policy.ledger_scope)] = PaperEngine(
                    store, quoter, policy.quote_policy,
                    policy.strategy_version, mode,
                    policy.allowed_protocols, policy.allowed_assets,
                    policy.allowed_routes, {policy.wallet: policy.label},
                    {policy.wallet: {"follower_wallet": policy.follower_wallet,
                                     "relationship_id": policy.relationship_id,
                                     "ledger_scope": policy.ledger_scope}},
                    policy.snapshot_hash,
                    shadow_only=mode != policy.trigger_mode,
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
        "account_prestate_missing",
        "paper_decisions", "paper_accepted", "paper_rejected", "paper_shadow_accepted",
        "paper_filled", "paper_fill_cancelled",
        "paper_reserved_recovered", "paper_errors",
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
            matching = [policy for policy in paper_config.policies_for(signal.wallet)] \
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
            execution = await paper_executor[matching[0].ledger_scope].execute(
                signal, proposal_id)
            stats["paper_reserved_recovered"] += 1
            stats["paper_filled" if execution.status == "filled"
                  else "paper_fill_cancelled"] += 1
            report("paper_execution_recovered", proposal_id=proposal_id,
                   status=execution.status, reason=execution.reason,
                   fill_id=execution.fill_id, paper_only=True, live_trading=False)

    async def paper_observe(signal):
        if not paper_config or signal.wallet not in paper_config.wallets:
            return
        for policy in paper_config.policies_for(signal.wallet):
            if signal.behavior in {"BUY", "TOKEN_SWAP"}:
                bucket = budget_bucket(signal.token_in) if signal.token_in else None
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
                             "swap_evidenced", "needs_review", "failed"}))
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
                report("paper_decision", decision_id=decision.decision_id,
                       source_event_id=signal.event_id, trigger_mode=mode,
                       relationship_id=policy.relationship_id,
                       follower_wallet=policy.follower_wallet,
                       shadow_only=engine.shadow_only, accepted=decision.accepted,
                       reason=decision.reason, proposal_id=decision.proposal_id,
                       live_trading=False)
                if decision.accepted and decision.proposal_id and not engine.shadow_only:
                    execution_started = time.monotonic()
                    execution = await paper_executor[policy.ledger_scope].execute(
                        signal, decision.proposal_id)
                    timings.observe("paper_execution_requote_ms",
                                    time.monotonic() - execution_started)
                    stats["paper_filled" if execution.status == "filled"
                          else "paper_fill_cancelled"] += 1
                    report("paper_execution", proposal_id=execution.proposal_id,
                           status=execution.status, reason=execution.reason,
                           fill_id=execution.fill_id, paper_only=True, live_trading=False)

    async def safe_paper_observe(signal):
        try:
            await paper_observe(signal)
        except Exception as exc:
            stats["paper_errors"] += 1
            report("paper_error", source_event_id=signal.event_id,
                   error_type=type(exc).__name__, live_trading=False)

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
                fresh = bool(tx.timestamp and health.healthy() and time.time() - tx.timestamp <= health.max_age_seconds)
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
                    for signal in final_signals:
                        signal.fresh = bool(tx.timestamp and health.healthy() and time.time() - tx.timestamp <= health.max_age_seconds)
                        signal.evidence["account_state_source"] = "transaction_prestate_trace"
                        signal.evidence["observation_source"] = tx.observation_source
                        emit(store, signal)
                        await safe_paper_observe(signal)
                        stats["receipt_signals"] += 1
                        if signal.behavior == "UNKNOWN":
                            stats["receipt_unknown"] += 1
                        if signal.stage == "needs_review":
                            stats["receipt_needs_review"] += 1
                        if signal.stage == "swap_evidenced":
                            stats["receipt_swap_evidenced"] += 1
                    stats["receipts"] += 1
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

    async def dispatcher():
        while True:
            wake_dispatcher.clear()
            if not dispatch_pending(store, queue, stats):
                try:
                    await asyncio.wait_for(wake_dispatcher.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(0)

    async def heartbeat():
        while True:
            await asyncio.sleep(5)
            report("health", healthy=health.healthy(), queued=queue.qsize(), counters=dict(stats),
                   candidate_states=store.candidate_counts(), chain_cursor=store.chain_cursor(),
                   latency_ms=timings.summary(), coverage=coverage_summary(stats))

    async def backfill():
        def progress(candidates, passive_candidates):
            stats["backfill_blocks"] += 1
            stats["backfill_candidates"] += candidates
            stats["backfill_passive_candidates"] += passive_candidates
            if candidates:
                wake_dispatcher.set()

        scanner = BlockScanner(rpc, store, watchlist, args.confirmations, args.backfill_batch,
                               progress=progress)
        while True:
            try:
                result = await scanner.scan_once()
                if result.initialized:
                    report("backfill_initialized", chain_cursor=store.chain_cursor(),
                           confirmations=args.confirmations)
            except ReorgDetected as exc:
                stats["reorg_detected"] += 1
                try:
                    resolution = await scanner.reconcile_reorg()
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
    workers = [asyncio.create_task(worker()) for _ in range(args.workers)]
    dispatch_task = asyncio.create_task(dispatcher())
    backfill_task = asyncio.create_task(backfill())
    beat = asyncio.create_task(heartbeat())
    receiver = asyncio.create_task(receive())
    try:
        report("monitor_started", wallets=len(watchlist), seconds=args.seconds, live_trading=False)
        if args.seconds > 0:
            try:
                await asyncio.wait_for(receiver, timeout=args.seconds)
            except asyncio.TimeoutError:
                pass
        else:
            await receiver
        try:
            await asyncio.wait_for(queue.join(), timeout=15)
        except asyncio.TimeoutError:
            report("drain_timeout", unfinished=queue.qsize())
    finally:
        for task in [receiver, dispatch_task, backfill_task, beat, *workers]:
            task.cancel()
        await asyncio.gather(receiver, dispatch_task, backfill_task, beat, *workers,
                             return_exceptions=True)
        candidate_states = store.candidate_counts()
        chain_cursor = store.chain_cursor()
        store.close()
        report("monitor_finished", counters=dict(stats), candidate_states=candidate_states,
               chain_cursor=chain_cursor, latency_ms=timings.summary(),
               coverage=coverage_summary(stats), live_trading=False)


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
            matching = [policy for policy in config.policies_for(source.wallet)] \
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
                    store, quoter, matching[0].quote_policy).mark(lot_id, source)
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
    root = argparse.ArgumentParser(description="Read-only smart-money observer; no signing or broadcasting")
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
    monitor_parser.add_argument("--backfill-batch", type=int, default=20)
    monitor_parser.add_argument("--backfill-interval", type=float, default=1.0)
    monitor_source = monitor_parser.add_mutually_exclusive_group()
    monitor_source.add_argument("--paper-config")
    monitor_source.add_argument("--paper-mysql", action="store_true",
                                help="Load enabled copy relationships from local MySQL")
    monitor_parser.add_argument("--paper-cycle-action", choices=("reuse", "reset"))
    monitor_parser.add_argument("--paper-cycle-id")
    monitor_parser.add_argument("--paper-cycle-reason")
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
        elif args.command == "monitor":
            if (args.seconds < 0 or not 1 <= args.workers <= 8 or not 1 <= args.queue_size <= 10000
                    or args.confirmations < 0 or not 1 <= args.backfill_batch <= 1000
                    or not 0.1 <= args.backfill_interval <= 60):
                raise ValueError("invalid monitor limits")
            if args.paper_config or args.paper_mysql:
                if args.paper_cycle_action == "reset" and (
                        not args.paper_cycle_id or not args.paper_cycle_reason):
                    raise ValueError("paper reset requires cycle id and reason")
                if args.paper_cycle_action == "reuse" and (
                        args.paper_cycle_id or args.paper_cycle_reason):
                    raise ValueError("paper reuse does not accept cycle id or reason")
                if args.paper_cycle_action is None:
                    raise ValueError("paper mode requires explicit cycle action")
            elif any((args.paper_cycle_action, args.paper_cycle_id, args.paper_cycle_reason)):
                raise ValueError("paper cycle options require --paper-config or --paper-mysql")
            asyncio.run(monitor(args))
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
        elif args.command == "relationships-import":
            inserted, skipped = import_watchlist_relationships(
                args.follower_wallet, args.follower_label, args.watchlist, args.template)
            report("relationships_imported", inserted=inserted, skipped_existing=skipped,
                   enabled=False, live_trading=False)
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
