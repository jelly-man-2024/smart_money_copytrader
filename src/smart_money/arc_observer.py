"""Read-only Arc mainnet Swap-log ingestion.

The WebSocket is only a low-latency hint.  Every candidate is re-read through
the allowlisted HTTPS RPC client, decoded from transaction calldata, and
checked against its canonical receipt before any signal is persisted.
"""
from __future__ import annotations

import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlsplit

import websockets

from . import registry as R
from .decode import Decoder
from .models import Signal, Transaction, address, number
from .native_flows import verify_native_flows
from .pools import verify_signal_pools
from .receipts import SWAPS, enrich
from .rpc import ReadOnlyRpc, RpcError
from .store import Store


ARC_SWAP_TOPIC = next(topic for topic, protocol in SWAPS.items() if protocol == "v4")
MAX_SEEN_TRANSACTIONS = 8192


def validate_arc_ws_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme == "wss":
        return url
    if parsed.scheme == "ws" and parsed.hostname in {"localhost", "127.0.0.1"}:
        return url
    raise ValueError("Arc WebSocket must use WSS, except on localhost")


def validate_arc_swap_log(value: object) -> dict:
    """Validate the untrusted subscription payload before using any identity."""
    if not isinstance(value, dict) or value.get("removed") is True:
        if isinstance(value, dict) and value.get("removed") is True:
            raise ValueError("removed Arc log requires canonical rescan")
        raise ValueError("invalid Arc log")
    required = ("address", "transactionHash", "blockHash", "blockNumber", "logIndex", "topics")
    if any(key not in value for key in required):
        raise ValueError("incomplete Arc log")
    if address(value["address"]) != R.ARC.v4_manager:
        raise ValueError("Arc log is not from the configured v4 manager")
    topics = value["topics"]
    if (not isinstance(topics, list) or len(topics) < 2
            or not isinstance(topics[0], str) or topics[0].lower() != ARC_SWAP_TOPIC):
        raise ValueError("Arc log is not a v4 Swap")
    for field in ("transactionHash", "blockHash"):
        item = value[field]
        if (not isinstance(item, str) or len(item) != 66 or not item.startswith("0x")):
            raise ValueError(f"invalid Arc log {field}")
        int(item[2:], 16)
    number(value["blockNumber"])
    number(value["logIndex"])
    return value


class ArcSwapSubscriber:
    """One-purpose WSS subscriber with bounded duplicate suppression."""

    def __init__(self, url: str):
        self.url = validate_arc_ws_url(url)
        self._seen_order: deque[str] = deque()
        self._seen: set[str] = set()

    def _first_transaction_log(self, log: dict) -> bool:
        tx_hash = log["transactionHash"].lower()
        if tx_hash in self._seen:
            return False
        self._seen.add(tx_hash)
        self._seen_order.append(tx_hash)
        if len(self._seen_order) > MAX_SEEN_TRANSACTIONS:
            self._seen.remove(self._seen_order.popleft())
        return True

    async def logs(self) -> AsyncIterator[dict]:
        async with websockets.connect(
                self.url, open_timeout=15, close_timeout=5,
                max_size=1024 * 1024, max_queue=256, ping_interval=20,
                ping_timeout=20) as socket:
            request = {
                "jsonrpc": "2.0", "id": 1, "method": "eth_subscribe",
                "params": ["logs", {
                    "address": R.ARC.v4_manager,
                    "topics": [ARC_SWAP_TOPIC],
                }],
            }
            await socket.send(json.dumps(request, separators=(",", ":")))
            acknowledgement = json.loads(await socket.recv())
            if (not isinstance(acknowledgement, dict)
                    or acknowledgement.get("id") != 1
                    or not isinstance(acknowledgement.get("result"), str)
                    or not acknowledgement["result"]):
                raise ValueError("invalid Arc subscription acknowledgement")
            subscription = acknowledgement["result"]
            async for raw in socket:
                document = json.loads(raw)
                if (not isinstance(document, dict)
                        or document.get("method") != "eth_subscription"):
                    raise ValueError("unexpected Arc WebSocket message")
                params = document.get("params")
                if (not isinstance(params, dict)
                        or params.get("subscription") != subscription):
                    raise ValueError("Arc subscription identity mismatch")
                log = validate_arc_swap_log(params.get("result"))
                if self._first_transaction_log(log):
                    yield log


class ArcObserver:
    """Turn verified Arc log hints into the existing candidate/signal model."""

    def __init__(self, rpc: ReadOnlyRpc, store: Store, watchlist: dict,
                 on_signal: Callable[[Signal], None] | None = None):
        self.rpc = rpc
        self.store = store
        self.watchlist = watchlist
        self.decoder = Decoder(watchlist, chain_id=R.ARC.chain_id)
        self.on_signal = on_signal

    @staticmethod
    def _receipt_contains_hint(receipt: dict, hint: dict) -> bool:
        expected = (
            hint["transactionHash"].lower(), hint["blockHash"].lower(),
            number(hint["logIndex"]), R.ARC.v4_manager, ARC_SWAP_TOPIC,
        )
        for item in receipt.get("logs", []):
            topics = item.get("topics", []) if isinstance(item, dict) else []
            try:
                observed = (
                    item.get("transactionHash", receipt.get("transactionHash", "")).lower(),
                    item["blockHash"].lower(), number(item["logIndex"]),
                    address(item["address"]), topics[0].lower(),
                )
            except (AttributeError, KeyError, TypeError, ValueError, IndexError):
                continue
            if observed == expected and not item.get("removed", False):
                return True
        return False

    async def observe(self, hint: dict) -> list[Signal]:
        hint = validate_arc_swap_log(hint)
        tx_hash = hint["transactionHash"].lower()
        raw = await self.rpc.call("eth_getTransactionByHash", [tx_hash])
        if not isinstance(raw, dict) or "chainId" not in raw:
            raise ValueError("Arc transaction or chain id unavailable")
        tx = Transaction.from_rpc(raw, observation_source="arc_v4_subscription")
        if tx.hash != tx_hash or tx.chain_id != R.ARC.chain_id:
            raise ValueError("Arc transaction identity mismatch")
        if tx.sender not in self.watchlist:
            return []

        self.store.put_candidate(tx)
        try:
            signals = self.decoder.decode(tx)
            receipt = await self.rpc.receipt(tx.hash)
            if not isinstance(receipt, dict) or not self._receipt_contains_hint(receipt, hint):
                raise ValueError("Arc subscription log not confirmed by receipt")
            pool_checks = await verify_signal_pools(self.rpc, signals, receipt)
            try:
                native_checks = await verify_native_flows(self.rpc, tx, receipt, signals)
            except (RpcError, ValueError, TypeError):
                # A provider without the bounded state-diff tracer cannot promote
                # native-USDC routes; enrich() leaves them review-only.
                native_checks = {}
            final = enrich(
                tx, signals, receipt, self.watchlist, pool_checks, native_checks)
            for signal in final:
                if self.store.put(signal) and self.on_signal is not None:
                    self.on_signal(signal)
            self.store.complete_candidate(
                tx.hash, number(receipt["blockNumber"]), receipt["blockHash"])
            return final
        except Exception as exc:
            self.store.fail_candidate(tx.hash, type(exc).__name__)
            raise


async def observe_arc(rpc: ReadOnlyRpc, ws_url: str, store: Store, watchlist: dict,
                      on_signal: Callable[[Signal], None] | None = None) -> None:
    if number(await rpc.call("eth_chainId")) != R.ARC.chain_id:
        raise ValueError("Arc RPC is connected to the wrong chain")
    observer = ArcObserver(rpc, store, watchlist, on_signal)
    async for log in ArcSwapSubscriber(ws_url).logs():
        await observer.observe(log)
