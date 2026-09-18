"""The observer's own status reports must reach the caller, not raise."""
import asyncio
import unittest
from unittest.mock import AsyncMock, patch

from smart_money import registry as R
from smart_money.arc_observer import ArcObserver, observe_arc


class StatusContractTests(unittest.IsolatedAsyncioTestCase):
    async def test_observer_status_reaches_the_caller_through_observe_arc(self):
        """Regression: ArcObserver reports (event, dict) while observe_arc's own
        status helper takes keywords. Mismatched, every status the observer
        raised was a TypeError — and the observer only reports from the Relay
        attribution branch, so it surfaced the first time a watched wallet
        received tokens with no outflow, not in any earlier run."""
        captured, built = [], {}

        class StubObserver:
            def __init__(self, *args, **kwargs):
                built["on_status"] = kwargs["on_status"]

        class StubSubscriber:
            def __init__(self, *args, **kwargs):
                pass

            def logs(self):
                raise asyncio.CancelledError

        rpc = AsyncMock()
        rpc.call.return_value = hex(R.ARC.chain_id)
        async def stop(*args, **kwargs):
            raise asyncio.CancelledError

        with patch("smart_money.arc_observer.ArcObserver", StubObserver), \
             patch("smart_money.arc_observer.ArcWalletSubscriber", StubSubscriber), \
             patch("smart_money.arc_observer.arc_backfill_once", stop):
            with self.assertRaises(asyncio.CancelledError):
                await observe_arc(rpc, "wss://arc.invalid/ws", object(), {},
                                  None, lambda event, details: captured.append((event, details)),
                                  backfill_interval=5.0)
        # The observer hands its callback a plain dict; it must survive that.
        built["on_status"]("arc_relay_buy_associated", {"source_event_id": "e", "relay_order_id": "o"})
        self.assertEqual(captured[-1],
                         ("arc_relay_buy_associated",
                          {"source_event_id": "e", "relay_order_id": "o"}))

    def test_observer_calls_its_callback_with_a_dict(self):
        seen = []
        observer = ArcObserver.__new__(ArcObserver)
        observer.on_status = lambda event, details: seen.append((event, details))
        observer._status("arc_relay_lookup_pending", source_event_id="e")
        self.assertEqual(seen, [("arc_relay_lookup_pending", {"source_event_id": "e"})])


if __name__ == "__main__":
    unittest.main()
