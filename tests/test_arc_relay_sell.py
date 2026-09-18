"""Arc closes a cross-chain sell, or defers it — never assumes one."""
import unittest
from unittest.mock import AsyncMock, patch

from smart_money import registry as R
from smart_money.arc_observer import ArcObserver
from smart_money.models import Signal
from smart_money.relay_api import RelayApiError, RelayNotReady

WALLET = "0x89909912c58e2182d92b1a8638d6ff8d965e173b"
TOKEN = "0x1b10319b6b535042ef6d2428be915527eabc5957"


def sell_signal(**overrides):
    fields = dict(
        behavior="SELL", stage="needs_review", protocol="0x",
        token_in=TOKEN, token_out=R.ARC.usdc_erc20, chain_id=R.ARC.chain_id,
        reasons=["relay_sell_evidence_not_uniquely_closed"],
        evidence={"source_orchestrator": "relay"})
    fields.update(overrides)
    reasons = fields.pop("reasons")
    evidence = fields.pop("evidence")
    return Signal("0x" + "cc" * 32, WALLET, "third_party", fields.pop("behavior"),
                  "outgoing", TOKEN, "0x0a2b8f36", reasons=reasons,
                  evidence=evidence, **fields)


def observer(client):
    obs = ArcObserver.__new__(ArcObserver)
    obs.relay_client = client
    obs.status_events = []
    obs.on_status = lambda event, details: obs.status_events.append((event, details))
    obs._relay_pending_now = False
    return obs


class ArcRelaySellTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_matching_order_closes_the_sell(self):
        closed = sell_signal(stage="relay_sell_evidenced")
        obs = observer(AsyncMock())
        with patch("smart_money.arc_observer.relay_confirmed_sell", return_value=closed):
            result = await obs._close_relay_sell(sell_signal())
        self.assertEqual(result.stage, "relay_sell_evidenced")
        self.assertIn("arc_relay_sell_confirmed", [e for e, _ in obs.status_events])

    async def test_an_unready_or_failed_lookup_defers_instead_of_rejecting(self):
        for error, event in ((RelayNotReady("not settled"), "arc_relay_sell_pending"),
                             (RelayApiError("rate limited"), "arc_relay_sell_deferred")):
            with self.subTest(error=type(error).__name__):
                client = AsyncMock()
                client.lookup_requests_by_hash.side_effect = error
                obs = observer(client)
                result = await obs._close_relay_sell(sell_signal())
                # Never promoted, and queued for the bounded retry.
                self.assertEqual(result.stage, "needs_review")
                self.assertTrue(obs._relay_pending_now)
                self.assertIn(event, [e for e, _ in obs.status_events])

    async def test_a_signal_of_another_shape_is_left_untouched(self):
        client = AsyncMock(side_effect=AssertionError("must not look up"))
        for overrides in ({"behavior": "BUY"}, {"stage": "swap_evidenced"},
                          {"protocol": "v4"}, {"evidence": {}},
                          {"reasons": ["something_else"]}):
            with self.subTest(**overrides):
                obs = observer(client)
                signal = sell_signal(**overrides)
                self.assertIs(await obs._close_relay_sell(signal), signal)
                self.assertEqual(obs.status_events, [])

    async def test_without_a_relay_client_nothing_is_closed(self):
        obs = observer(None)
        signal = sell_signal()
        self.assertIs(await obs._close_relay_sell(signal), signal)


if __name__ == "__main__":
    unittest.main()
