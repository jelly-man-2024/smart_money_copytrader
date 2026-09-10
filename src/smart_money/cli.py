from __future__ import annotations

import argparse
import asyncio
from collections import Counter, deque
import json
import os
from pathlib import Path
import sys
import time

import websockets

from .decode import Decoder
from .feed import DecodeError, FeedHealth, decode_raw, envelopes
from .models import Transaction, number
from .receipts import enrich
from .registry import CHAIN_ID, ENTRYPOINT, delegation, load_watchlist, snapshot_delegations
from .rpc import ReadOnlyRpc, RpcError
from .store import Store


def report(event: str, **details):
    print(json.dumps({"event": event, **details}, ensure_ascii=False), file=sys.stderr, flush=True)


def emit(store, signal):
    if store.put(signal):
        print(json.dumps(signal.to_dict(), ensure_ascii=False), flush=True)


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
    store = Store(args.db)
    counts = Counter()
    try:
        for tx, receipt in examples:
            for signal in enrich(tx, decoder.decode(tx), receipt, watchlist):
                emit(store, signal)
                counts[signal.behavior] += 1
    finally:
        store.close()
    report("replay_finished", transactions=len(examples), behaviors=dict(counts), live_trading=False)


async def monitor(args):
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
    seen = set()
    recent = deque()
    store = Store(args.db)

    async def account_impl(wallet):
        # Read latest implementation for relevant accounts; historical replay uses
        # its own snapshot. Receipt-block state reconciliation is a later milestone.
        try:
            code = await rpc.call("eth_getCode", [wallet, "latest"])
            impl = delegation(code)
            decoder.delegations.pop(wallet, None)
            if impl:
                decoder.delegations[wallet] = impl
        except RpcError:
            decoder.delegations.pop(wallet, None)
            stats["account_code_errors"] += 1

    async def worker():
        while True:
            tx = await queue.get()
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
                    emit(store, signal)
                receipt = await rpc.receipt(tx.hash)
                if receipt is None:
                    stats["receipt_unavailable"] += 1
                    report("receipt_unavailable", tx_hash=tx.hash)
                else:
                    for signal in enrich(tx, signals, receipt, watchlist):
                        signal.fresh = bool(tx.timestamp and health.healthy() and time.time() - tx.timestamp <= health.max_age_seconds)
                        signal.evidence["account_state_source"] = "latest_not_historical"
                        emit(store, signal)
                    stats["receipts"] += 1
            except Exception as exc:
                stats["worker_errors"] += 1
                report("candidate_error", tx_hash=tx.hash, error_type=type(exc).__name__)
            finally:
                queue.task_done()

    async def heartbeat():
        while True:
            await asyncio.sleep(5)
            report("health", healthy=health.healthy(), queued=queue.qsize(), counters=dict(stats))

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
                                    tx = decode_raw(raw, **metadata)
                                except DecodeError:
                                    stats["unsupported_or_invalid_transactions"] += 1
                                    continue
                                stats["decoded_transactions"] += 1
                                # Raw address matching only widens the candidate set.
                                # Direct senders are always recovered from signatures.
                                relevant = tx.sender in watchlist or any(a in tx.data for a in watched_bytes)
                                if not relevant or tx.hash in seen:
                                    continue
                                seen.add(tx.hash)
                                recent.append(tx.hash)
                                if len(recent) > 20000:
                                    seen.remove(recent.popleft())
                                if queue.full():
                                    stats["queue_drops"] += 1
                                    report("queue_overflow", tx_hash=tx.hash)
                                    continue
                                queue.put_nowait(tx)
                                stats["candidates"] += 1
                        except DecodeError:
                            stats["frame_errors"] += 1
                            raise
            except (websockets.WebSocketException, OSError, DecodeError):
                stats["reconnections"] += 1
                health.reset()
                report("feed_reconnecting", reason="connection_or_frame_error")
                await asyncio.sleep(1)

    workers = [asyncio.create_task(worker()) for _ in range(args.workers)]
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
        for task in [receiver, beat, *workers]:
            task.cancel()
        await asyncio.gather(receiver, beat, *workers, return_exceptions=True)
        store.close()
        report("monitor_finished", counters=dict(stats), live_trading=False)


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
    export_parser = commands.add_parser("export", help="Export deduplicated SQLite signals as JSONL")
    export_parser.add_argument("--db", default="var/observer.sqlite3")
    return root


def main():
    args = parser().parse_args()
    try:
        if args.command == "replay":
            replay(args)
        elif args.command == "monitor":
            if args.seconds < 0 or not 1 <= args.workers <= 8 or not 1 <= args.queue_size <= 10000:
                raise ValueError("invalid monitor limits")
            asyncio.run(monitor(args))
        else:
            if not Path(args.db).is_file():
                raise ValueError("database does not exist")
            store = Store(args.db)
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
