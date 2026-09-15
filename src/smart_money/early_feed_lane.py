"""Bounded in-process evidence lane fed by the monitor's ONE Feed connection.

This lane does not reserve funds, invoke an executor or grant live eligibility.
It is the durable ingestion/recognition boundary for the pending live handoff.
"""
from __future__ import annotations

import asyncio
from collections import Counter
from copy import deepcopy
import json
import math
import time

from . import registry as R
from .early_intent import parse_candidates
from .early_replay import evaluate_candidate
from .early_timing import EARLY_FEED_MAX_AGE_SECONDS
from .relay_race import RACE_ADDRESS, RACE_RULE


class EarlyFeedJobStore:
    def create_early_feed_job(self, tx):
        """Candidate must already be durable. Never overwrites a previous result."""
        try:
            changed = self.connection.execute("""INSERT OR IGNORE INTO early_feed_jobs
                (tx_hash,status,received_at,updated_at) SELECT ?,'queued',?,?
                WHERE EXISTS(SELECT 1 FROM candidates WHERE tx_hash=?)""",
                (tx.hash, tx.received_at, time.time(), tx.hash)).rowcount
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise
        return changed == 1

    def finish_early_feed_job(self, tx_hash, status, result):
        if status not in {"done", "expired", "failed", "interrupted"}:
            raise ValueError("invalid early feed job status")
        payload = json.dumps(result, sort_keys=True, separators=(",", ":"))
        if len(payload.encode()) > 16 * 1024 * 1024:
            raise ValueError("early feed result too large")
        self.connection.execute("""UPDATE early_feed_jobs SET status=?,result_payload=?,updated_at=?
            WHERE tx_hash=? AND status='queued'""", (status, payload, time.time(), tx_hash))
        self.connection.commit()

    def interrupt_early_feed_jobs(self):
        """Do not replay pre-crash work into an eventual live handoff."""
        self.connection.execute("""UPDATE early_feed_jobs SET status='interrupted',
            result_payload=?,updated_at=? WHERE status='queued'""",
            ('{"reason":"process_interrupted","copy_eligible":false}', time.time()))
        self.connection.commit()


def fresh_feed(tx, now):
    return (tx.observation_source == "feed" and tx.fresh is True
            and type(tx.received_at) in (int, float) and math.isfinite(tx.received_at)
            and type(tx.timestamp) in (int, float) and math.isfinite(tx.timestamp)
            and tx.timestamp <= tx.received_at <= now
            and now - tx.received_at <= EARLY_FEED_MAX_AGE_SECONDS
            and now - tx.timestamp <= EARLY_FEED_MAX_AGE_SECONDS)


class EarlyEvidenceResolver:
    """Uses existing read-only clients; never creates a Feed subscription."""
    def __init__(self, rpc, relay, wallets, *, deployment_monitor=None):
        self.rpc, self.relay = rpc, relay
        self.wallets = tuple(wallets)
        self.deployment_monitor = deployment_monitor
        self.prefetch = None

    async def _code(self, wallet, deployment=False):
        block = await self.rpc.call("eth_getBlockByNumber", ["latest", False])
        code = await self.rpc.call("eth_getCode", [wallet, {
            "blockHash": block["hash"], "requireCanonical": True}])
        return {"chain_id": R.CHAIN_ID, "code": code, "block_hash": block["hash"],
                "block_number": block["number"],
                **({"contract": wallet, "rule": RACE_RULE} if deployment else {"wallet": wallet})}

    async def __call__(self, tx):
        results, rejections = [], []
        # A byte match widens candidates only. parse_candidates verifies structure
        # and identity; incoming transfers do not become purchase evidence.
        wallets = [w for w in self.wallets if tx.sender == w or bytes.fromhex(w[2:]) in tx.data]
        for wallet in wallets:
            if not fresh_feed(tx, time.time()):
                break
            parsed = parse_candidates(tx, wallet)
            if not parsed.candidates:
                rejections.append({"wallet": wallet, "reasons": list(parsed.reasons)})
            for candidate in parsed.candidates:
                if len(results) >= 16:
                    return {"candidates": results, "copy_eligible": False,
                            "reason": "candidate_limit_strict_fallback"}
                snapshots, errors = {}, {}
                async def capture(name, action):
                    started = time.time()
                    try:
                        payload = await action()
                        snapshots[name] = {"payload": payload, "observed_at": time.time(),
                                           "capture_started_at": started,
                                           "provenance": "monitor_single_feed_readonly_" + name}
                    except Exception as exc:
                        errors[name] = type(exc).__name__
                jobs = []
                initial = evaluate_candidate(candidate, time.time(), {}, enabled=False,
                                             feed_max_age_seconds=EARLY_FEED_MAX_AGE_SECONDS)
                if (not candidate.blockers and fresh_feed(tx, time.time())
                        and initial["checks"]["freshness"]["status"] == "pass"):
                    if candidate.side == "BUY":
                        jobs.append(capture("order", lambda: self.relay.lookup_by_order(
                            candidate.order_id, candidate.metadata.get("request_hint"))))
                        if (self.prefetch is not None
                                and initial["checks"]["semantics"]["status"] == "pass"):
                            # This callback only fetches market data. It cannot grant eligibility.
                            jobs.append(capture("quote_prefetch", lambda: self.prefetch(candidate)))
                        if candidate.route_kind == "relay_wrapper":
                            if self.deployment_monitor is None:
                                jobs.append(capture("deployment", lambda: self._code(RACE_ADDRESS, True)))
                            else:
                                try:
                                    snapshots["deployment"] = self.deployment_monitor.snapshot()
                                except ValueError as exc:
                                    errors["deployment"] = str(exc)
                    else:
                        jobs.append(capture("account", lambda: self._code(wallet)))
                # No cancellation/replacement of blocking HTTP threads on timeout:
                # transport timeouts bound workers; late results cannot qualify.
                await asyncio.gather(*jobs)
                checked_at = time.time()
                evaluation = evaluate_candidate(candidate, checked_at, snapshots, enabled=False,
                                                feed_max_age_seconds=EARLY_FEED_MAX_AGE_SECONDS,
                                                deployment_monitor=self.deployment_monitor)
                required = ["freshness", "semantics", "attribution"]
                if candidate.route_kind == "relay_wrapper":
                    required.append("deployment")
                recognized = all(evaluation["checks"][k]["status"] == "pass" for k in required)
                results.append({"candidate": candidate.to_dict(), "snapshots": snapshots,
                                "recognized_intent": recognized, "checked_at": checked_at,
                                "checks": evaluation["checks"], "errors": errors,
                                "copy_eligible": False, "live_handoff": "not_connected"})
        return {"candidates": results, "parse_rejections": rejections, "copy_eligible": False,
                "feed_max_age_seconds": EARLY_FEED_MAX_AGE_SECONDS,
                "recognized_count": sum(r["recognized_intent"] for r in results)}


class EarlyFeedLane:
    def __init__(self, store, resolver, *, healthy=lambda: True, queue_size=32, workers=2, handoff=None):
        if not 1 <= queue_size <= 256 or not 1 <= workers <= 4:
            raise ValueError("invalid early feed lane limits")
        self.store, self.resolver, self.healthy = store, resolver, healthy
        self.queue = asyncio.Queue(queue_size)
        self.worker_count, self.tasks = workers, []
        self.stats = Counter()
        self.handoff = handoff

    def submit(self, tx):
        if not self.tasks or all(task.done() for task in self.tasks):
            self.stats["not_running"] += 1
            return False
        if not self.healthy() or not fresh_feed(tx, time.time()):
            self.stats["stale_or_unhealthy"] += 1
            return False
        if not self.store.create_early_feed_job(tx):
            self.stats["duplicate_or_not_durable"] += 1
            return False
        try:
            self.queue.put_nowait(deepcopy(tx))
        except asyncio.QueueFull:
            self.store.finish_early_feed_job(tx.hash, "expired", {
                "reason": "queue_full_strict_fallback", "copy_eligible": False})
            self.stats["queue_full"] += 1
            return False
        self.stats["enqueued"] += 1
        return True

    def start(self):
        if self.tasks:
            raise ValueError("early feed lane already started")
        self.store.interrupt_early_feed_jobs()
        self.tasks = [asyncio.create_task(self._worker()) for _ in range(self.worker_count)]

    async def _worker(self):
        while True:
            tx = await self.queue.get()
            try:
                if not self.healthy() or not fresh_feed(tx, time.time()):
                    self.store.finish_early_feed_job(tx.hash, "expired", {
                        "reason": "queue_wait_or_feed_unhealthy", "copy_eligible": False})
                    self.stats["expired"] += 1
                    continue
                result = await self.resolver(tx)
                if not self.healthy() or not fresh_feed(tx, time.time()):
                    result = {**result, "eligible_after_processing": False,
                              "reason": "processing_expired_or_feed_unhealthy"}
                    status = "expired"
                else:
                    status = "done"
                self.store.finish_early_feed_job(tx.hash, status, result)
                self.stats[status] += 1
                if status == "done" and self.handoff is not None:
                    await self.handoff(tx, result)
            except asyncio.CancelledError:
                self.store.finish_early_feed_job(tx.hash, "interrupted", {
                    "reason": "worker_cancelled", "copy_eligible": False})
                raise
            except Exception as exc:
                self.store.finish_early_feed_job(tx.hash, "failed", {
                    "error_type": type(exc).__name__, "copy_eligible": False})
                self.stats["failed"] += 1
            finally:
                self.queue.task_done()

    async def close(self):
        for task in self.tasks:
            task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)
        self.tasks = []
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self.store.interrupt_early_feed_jobs()
