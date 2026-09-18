"""A Relay order that has not settled yet is retried, never dropped."""
import time
import unittest
from unittest.mock import AsyncMock

from smart_money import registry as R
from smart_money.arc_observer import (
    MAX_RELAY_RETRY_ATTEMPTS, RELAY_RETRY_DEADLINE_SECONDS, ArcObserver)

TX = "0x" + "3b" * 32
HINT = {"transactionHash": TX}


def observer():
    obs = ArcObserver.__new__(ArcObserver)
    obs.on_status = lambda event, details: obs.status_events.append((event, details))
    obs.status_events = []
    obs._relay_pending = {}
    obs._relay_pending_now = False
    obs._processed = set()
    obs._processed_order = __import__("collections").deque()
    return obs


class RelayRetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_pending_delivery_is_retried_until_it_resolves(self):
        obs = observer()
        obs._relay_pending[TX] = {"hint": HINT, "attempts": 1, "first_seen": time.time()}

        async def settle(hint):
            obs._relay_pending.pop(TX, None)      # Relay settled on this pass
        obs.observe = settle

        result = await obs.retry_pending_relay()
        self.assertEqual(result, {"retried": 1, "resolved": 1, "abandoned": 0, "pending": 0})
        self.assertIn("arc_relay_retry_resolved", [e for e, _ in obs.status_events])
        # Resolved deliveries are not retried again.
        self.assertEqual(await obs.retry_pending_relay(),
                         {"retried": 0, "resolved": 0, "abandoned": 0, "pending": 0})

    async def test_a_still_pending_delivery_stays_queued(self):
        obs = observer()
        obs._relay_pending[TX] = {"hint": HINT, "attempts": 1, "first_seen": time.time()}
        obs.observe = AsyncMock()                  # still pending: entry untouched
        result = await obs.retry_pending_relay()
        self.assertEqual(result["retried"], 1)
        self.assertEqual(result["pending"], 1)
        self.assertEqual(result["resolved"], 0)

    async def test_retries_are_bounded_by_attempts_and_by_time(self):
        for entry in ({"hint": HINT, "attempts": MAX_RELAY_RETRY_ATTEMPTS,
                       "first_seen": time.time()},
                      {"hint": HINT, "attempts": 1,
                       "first_seen": time.time() - RELAY_RETRY_DEADLINE_SECONDS - 1}):
            with self.subTest(entry=entry):
                obs = observer()
                obs._relay_pending[TX] = dict(entry)
                obs.observe = AsyncMock(side_effect=AssertionError("must not retry"))
                result = await obs.retry_pending_relay()
                self.assertEqual(result["abandoned"], 1)
                self.assertEqual(result["pending"], 0)
                # Giving up means the delivery stays unattributed, never a buy.
                self.assertIn("arc_relay_retry_exhausted",
                              [e for e, _ in obs.status_events])
                self.assertIn(TX, obs._processed)

    async def test_a_transient_read_failure_counts_as_an_attempt_and_keeps_the_entry(self):
        from smart_money.rpc import RpcError
        obs = observer()
        obs._relay_pending[TX] = {"hint": HINT, "attempts": 0, "first_seen": time.time()}
        obs.observe = AsyncMock(side_effect=RpcError("not available yet"))
        result = await obs.retry_pending_relay()
        self.assertEqual(result["pending"], 1)
        self.assertEqual(obs._relay_pending[TX]["attempts"], 1)
        self.assertIn("arc_relay_retry_error", [e for e, _ in obs.status_events])


if __name__ == "__main__":
    unittest.main()
