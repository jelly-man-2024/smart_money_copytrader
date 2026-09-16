"""Supervision of the monitor's critical ledger loops (dispatcher/heartbeat).

Regression for 2026-09-16: a MySQL socket loss raised inside the heartbeat and
dispatcher tasks, both died silently, and the process kept running with a stalled
strict channel. The supervisor must retry transient faults, count and report each
failure, and fail closed (stop hook + loud exit) when failures persist.
"""
import asyncio
import unittest
from collections import Counter

from smart_money.cli import supervise_critical_task


class SuperviseCriticalTask(unittest.IsolatedAsyncioTestCase):
    async def test_transient_failures_recover_without_fail_closed(self):
        stats = Counter()
        calls = {"n": 0}
        recovered = asyncio.Event()
        fail_closed_calls = []

        async def iteration():
            calls["n"] += 1
            if calls["n"] <= 2:
                raise ValueError("transient ledger fault")
            recovered.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(supervise_critical_task(
            "dispatcher", iteration, stats, fail_closed_calls.append,
            backoffs=(0, 0, 0)))
        await asyncio.wait_for(recovered.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stats["dispatcher_errors"], 2)
        self.assertEqual(fail_closed_calls, [])

    async def test_persistent_failure_fails_closed_and_raises(self):
        stats = Counter()
        fail_closed_calls = []

        async def iteration():
            raise ValueError("ledger connection is gone")

        with self.assertRaises(ValueError):
            await supervise_critical_task(
                "heartbeat", iteration, stats,
                lambda: fail_closed_calls.append("stopped"), backoffs=(0, 0))
        # Two retries per the schedule, the third consecutive failure is final.
        self.assertEqual(stats["heartbeat_errors"], 3)
        self.assertEqual(fail_closed_calls, ["stopped"])

    async def test_success_resets_the_consecutive_failure_budget(self):
        stats = Counter()
        fail_closed_calls = []
        calls = {"n": 0}
        parked = asyncio.Event()

        async def iteration():
            calls["n"] += 1
            # Alternate one failure / one success longer than the backoff
            # schedule; the reset must prevent fail-closed.
            if calls["n"] % 2 == 1 and calls["n"] < 9:
                raise ValueError("blip")
            if calls["n"] >= 9:
                parked.set()
                await asyncio.sleep(60)

        task = asyncio.create_task(supervise_critical_task(
            "dispatcher", iteration, stats, fail_closed_calls.append,
            backoffs=(0,)))
        await asyncio.wait_for(parked.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stats["dispatcher_errors"], 4)
        self.assertEqual(fail_closed_calls, [])

    async def test_cancellation_passes_through_without_fail_closed(self):
        stats = Counter()
        fail_closed_calls = []
        started = asyncio.Event()

        async def iteration():
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(supervise_critical_task(
            "dispatcher", iteration, stats, fail_closed_calls.append))
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(stats["dispatcher_errors"], 0)
        self.assertEqual(fail_closed_calls, [])


if __name__ == "__main__":
    unittest.main()
