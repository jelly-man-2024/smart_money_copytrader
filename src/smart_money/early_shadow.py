"""Opt-in independent Feed evidence collector. Never imported by the live monitor.

No Store, signer, executor, reservations, transaction building or broadcasting.
All evidence is timestamped on receipt; later truth is written separately.
"""
from __future__ import annotations

import asyncio
from collections import Counter, OrderedDict
from dataclasses import asdict
import json
import threading
import time

from . import registry as R
from .early_intent import fingerprint, parse_candidates
from .early_replay import evaluate_candidate, reconcile
from .feed import DecodeError, FeedHealth, decode_raw, envelopes
from .relay_race import RACE_ADDRESS, RACE_RULE


def transaction_record(tx):
    return {**asdict(tx), "data": "0x" + tx.data.hex(), "value": str(tx.value)}


def snapshot(payload, provenance, observed_at=None):
    return {"payload": payload, "provenance": provenance,
            "observed_at": time.time() if observed_at is None else observed_at}


class JsonlSink:
    """Exclusive output, bounded size, fail closed on I/O failure. No overwrites."""
    def __init__(self, path, max_bytes=64 * 1024 * 1024):
        self.stream = open(path, "x", encoding="utf-8")
        self.max_bytes, self.bytes_written = max_bytes, 0
        self.lock = threading.Lock()

    def write(self, record):
        line = json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        size = len(line.encode("utf-8"))
        with self.lock:
            if size + self.bytes_written > self.max_bytes:
                raise ValueError("shadow_output_limit")
            self.stream.write(line)
            self.stream.flush()
            self.bytes_written += size

    def close(self):
        with self.lock:
            self.stream.close()


class ShadowCollector:
    def __init__(self, rpc, relay, business, sink, *, queue_size=32, workers=2,
                 evidence_timeout=3.0, reconcile_seconds=30.0):
        if not 1 <= workers <= 4 or not 1 <= queue_size <= 256:
            raise ValueError("invalid_shadow_concurrency")
        if not 0 < evidence_timeout <= 10 or not 0 <= reconcile_seconds <= 60:
            raise ValueError("invalid_shadow_timeout")
        self.rpc, self.relay, self.business, self.sink = rpc, relay, business, sink
        self.queue = asyncio.Queue(queue_size)
        self.workers, self.evidence_timeout = workers, evidence_timeout
        self.reconcile_seconds = reconcile_seconds
        self.stats = Counter()
        self.seen = OrderedDict()
        self.run_id = fingerprint([time.time_ns(), "early-shadow-v1"])

    async def emit(self, kind, **fields):
        # Only bounded worker calls are in flight; disk latency cannot block the
        # Feed receiver's event loop. The CLI gives this method a serialized sink.
        async with self.write_lock:
            await asyncio.to_thread(self.sink.write, {
                "record_type": kind, "run_id": self.run_id, "recorded_at": time.time(),
                "live_enabled": False, "copy_eligible": False, **fields})

    def enqueue(self, tx, wallets):
        if tx.hash in self.seen:
            self.stats["duplicate_transactions"] += 1
            return
        self.seen[tx.hash] = None
        if len(self.seen) > 4096:
            self.seen.popitem(last=False)
        try:
            self.queue.put_nowait((tx, wallets))
            self.stats["enqueued_transactions"] += 1
        except asyncio.QueueFull:
            self.stats["queue_dropped_transactions"] += 1

    async def observe(self, tx, wallet):
        parsed = parse_candidates(tx, wallet)
        if not parsed.candidates:
            self.stats["parse_rejected_wallet_transactions"] += 1
            await self.emit("parse_rejected", transaction=transaction_record(tx),
                            wallet=wallet, reasons=list(parsed.reasons))
            return
        for candidate in parsed.candidates:
            started, snapshots, errors, timings = time.time(), {}, {}, {}
            self.stats["capture_started"] += 1
            await self.emit("candidate_started", tx_hash=tx.hash, wallet=wallet,
                            candidate=candidate.to_dict())

            async def capture(name, action):
                begin = time.time()
                try:
                    # Clients also have transport timeouts: cancelling to_thread
                    # alone does not stop its socket or release its thread.
                    payload = await action()
                    snapshots[name] = {**snapshot(payload, "early_shadow:" + name),
                                       "capture_started_at": begin}
                except Exception as exc:
                    errors[name] = type(exc).__name__  # no endpoint/body/credentials
                finally:
                    timings[name] = {"started_at": begin, "finished_at": time.time()}

            async def account():
                block = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
                # EIP-1898 binds code to this block, not a later 'latest'.
                code = await self.rpc.call("eth_getCode", [wallet, {
                    "blockHash": block["hash"], "requireCanonical": True}])
                return {"wallet": wallet, "chain_id": R.CHAIN_ID,
                        "code": code, "block_hash": block["hash"],
                        "block_number": block["number"]}

            async def deployment():
                block = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
                code = await self.rpc.call("eth_getCode", [RACE_ADDRESS, {
                    "blockHash": block["hash"], "requireCanonical": True}])
                return {"contract": RACE_ADDRESS, "rule": RACE_RULE, "chain_id": R.CHAIN_ID,
                        "code": code, "block_hash": block["hash"], "block_number": block["number"]}

            jobs = [capture("business", lambda: asyncio.to_thread(
                self.business.capture, candidate))]
            if candidate.side == "BUY":
                jobs.append(capture("order", lambda: self.relay.lookup_by_order(
                    candidate.order_id, candidate.metadata.get("request_hint"))))
                if candidate.route_kind == "relay_wrapper":
                    jobs.append(capture("deployment", deployment))
            else:
                jobs.append(capture("account", account))
            # Do not cancel blocking I/O threads and spawn replacements: each
            # worker waits for its bounded transport request to actually finish.
            await asyncio.gather(*jobs)
            shared = {k: v for k, v in snapshots.items() if k != "business"}
            business = snapshots.get("business", {}).get("payload", {})
            relationships = business.get("relationships", [])
            if not relationships:
                relationships = [{"relationship_id": None, "snapshots": {}}]
            for rel in relationships:
                evidence = {**shared, **rel.get("snapshots", {})}
                decision_at = time.time()
                result = evaluate_candidate(candidate, decision_at, evidence, enabled=True)
                case_id = fingerprint([self.run_id, candidate.operation_key,
                                       rel["relationship_id"], tx.hash, candidate.path])
                case = {"record_id": case_id, "transaction": transaction_record(tx),
                        "wallet": wallet, "relationship_id": rel["relationship_id"],
                        "decision_at": decision_at, "snapshots": evidence,
                        "provenance": "early_shadow:" + self.run_id}
                await self.emit("early_case", case=case, result=result,
                                capture_errors=errors, request_timings=timings,
                                started_at=started,
                                feed_to_evaluation_ms=(decision_at - tx.received_at) * 1000,
                                capture_deadline_exceeded=decision_at - started > self.evidence_timeout)
                self.stats["candidate_relationships"] += 1
                for name, check in result["checks"].items():
                    self.stats[name + ":" + check["status"]] += 1
                # Reconciliation uses a separate bounded queue and never feeds
                # data back into the already frozen early result.
                try:
                    self.truth_queue.put_nowait((case_id, candidate,
                                                time.monotonic() + self.reconcile_seconds))
                except asyncio.QueueFull:
                    self.stats["reconciliation_dropped"] += 1
            self.stats["capture_completed"] += 1

    async def truth_worker(self):
        while True:
            case_id, candidate, deadline = await self.truth_queue.get()
            try:
                signals = await asyncio.to_thread(self.business.truth, candidate.tx_hash,
                                                  candidate.wallet)
                # Bundled users and other operations in the same tx are not
                # interchangeable. Only exactly matching order/path is used.
                relevant = [s for s in signals if s.get("evidence", {}).get(
                    "relay_order_id", s.get("evidence", {}).get("relay_deposit_order_id"))
                    == candidate.order_id or s.get("path") == candidate.path]
                if len(relevant) > 1:
                    label, truth = "ambiguous_strict_evidence", None
                else:
                    truth = relevant[0] if relevant else None
                    label = reconcile(candidate, truth)
                await self.emit("reconciliation", record_id=case_id, label=label,
                                truth=truth, observed_at=time.time())
                if time.monotonic() < deadline:
                    # Keep polling even after matched: a later observed reorg
                    # must not disappear merely because a first match occurred.
                    await asyncio.sleep(1)
                    try:
                        self.truth_queue.put_nowait((case_id, candidate, deadline))
                    except asyncio.QueueFull:
                        self.stats["reconciliation_dropped"] += 1
            except Exception as exc:
                await self.emit("reconciliation_error", record_id=case_id,
                                error_type=type(exc).__name__)
            finally:
                self.truth_queue.task_done()

    async def run(self, frames, wallets, seconds=60):
        if not 0 < seconds <= 3600:
            raise ValueError("shadow_duration_must_be_1_to_3600")
        self.write_lock = asyncio.Lock()
        self.truth_queue = asyncio.Queue(256)
        health = FeedHealth()
        needles = {w: bytes.fromhex(w[2:]) for w in wallets}

        async def receive():
            async for frame in frames:
                self.stats["frames"] += 1
                try:
                    for raw, metadata in envelopes(frame, health):
                        tx = decode_raw(raw, observation_source="feed", **metadata)
                        matched = [w for w, n in needles.items() if tx.sender == w or n in tx.data]
                        if matched:
                            self.stats["relevant_transactions"] += 1
                            if not tx.fresh:
                                self.stats["stale_or_gap_transactions"] += 1
                            self.enqueue(tx, matched)
                except DecodeError:
                    self.stats["invalid_frames"] += 1
                if health.gap:
                    # A sequence gap requires an explicit new capture; never
                    # silently reset health and reinterpret replay as fresh.
                    self.stats["feed_gap_stop"] += 1
                    return

        async def worker():
            while True:
                tx, matches = await self.queue.get()
                try:
                    for wallet in matches:
                        await self.observe(tx, wallet)
                finally:
                    self.queue.task_done()

        await self.emit("run_started", wallets=wallets, seconds=seconds,
                        preparation_supported=False, market_capture_supported=False)
        workers = [asyncio.create_task(worker()) for _ in range(self.workers)]
        truth = asyncio.create_task(self.truth_worker())
        receiver = asyncio.create_task(receive())
        timer = asyncio.create_task(asyncio.sleep(seconds))
        tasks = [receiver, timer, truth, *workers]
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()  # writer/worker failure stops the capture
        finally:
            # Shutdown is bounded; queued/during-capture omissions stay visible.
            self.stats["queued_at_stop"] = self.queue.qsize()
            self.stats["reconciliation_queued_at_stop"] = self.truth_queue.qsize()
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await self.emit("run_finished", counters=dict(self.stats),
                            pending_is_not_failure=True)
        return dict(self.stats)
