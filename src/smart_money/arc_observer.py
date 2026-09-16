"""Read-only Arc mainnet Swap-log ingestion.

The WebSocket is only a low-latency hint.  Every candidate is re-read through
the allowlisted HTTPS RPC client, decoded from transaction calldata, and
checked against its canonical receipt before any signal is persisted.
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlsplit

import websockets

from . import registry as R
from .account_state import prestate_implementations
from .decode import Decoder
from .models import Signal, Transaction, address, number
from .native_flows import verify_native_flows
from .pools import verify_signal_pools
from .receipts import SWAPS, enrich
from .rpc import ReadOnlyRpc, RpcError
from .store import Store


ARC_SWAP_TOPIC = next(topic for topic, protocol in SWAPS.items() if protocol == "v4")
MAX_SEEN_TRANSACTIONS = 8192
MAX_CACHED_BLOCKS = 64
ARC_CURSOR = "arc_v4"


class ArcCandidateRejected(ValueError):
    """Permanent candidate-data rejection; safe to advance the scan cursor."""


class ArcCanonicalMismatch(RuntimeError):
    """Saved Arc cursor no longer matches the RPC canonical block."""


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
        self._lock = asyncio.Lock()
        self._processed_order: deque[str] = deque()
        self._processed: set[str] = set()
        self._block_order: deque[tuple[int, str]] = deque()
        self._block_transactions: dict[tuple[int, str], dict[str, dict]] = {}

    def _remember(self, tx_hash: str) -> None:
        if tx_hash in self._processed:
            return
        self._processed.add(tx_hash)
        self._processed_order.append(tx_hash)
        if len(self._processed_order) > MAX_SEEN_TRANSACTIONS:
            self._processed.remove(self._processed_order.popleft())

    async def _decode(self, tx: Transaction) -> list[Signal]:
        """Decode self-calls only from transaction-prestate delegation proof."""
        self.decoder.delegations.pop(tx.sender, None)
        account_state_source = "sender_direct_call_not_delegation_dependent"
        if tx.to == tx.sender:
            account_state_source = "transaction_prestate_trace_unavailable"
            try:
                implementations, _ = await prestate_implementations(
                    self.rpc, tx.hash, [tx.sender])
            except (RpcError, TypeError, ValueError):
                implementations = {}
            else:
                account_state_source = "transaction_prestate_unsupported_or_absent"
            implementation = implementations.get(tx.sender)
            if implementation is not None:
                self.decoder.delegations[tx.sender] = implementation
                account_state_source = "transaction_prestate_trace"
        signals = self.decoder.decode(tx)
        for signal in signals:
            signal.evidence["account_state_source"] = account_state_source
            signal.evidence["observation_source"] = tx.observation_source
        return signals

    async def _transaction_from_block(self, hint: dict) -> dict:
        height = number(hint["blockNumber"])
        hinted_hash = hint["blockHash"].lower()
        key = (height, hinted_hash)
        indexed = self._block_transactions.get(key)
        if indexed is None:
            block = await self.rpc.call(
                "eth_getBlockByNumber", [hex(height), True])
            if block is None:
                raise RpcError("Arc block is not available yet")
            block_hash, _ = _block_identity(block, height)
            if block_hash != hinted_hash:
                raise ArcCanonicalMismatch(
                    "Arc subscription block hash does not match HTTPS RPC")
            transactions = block.get("transactions")
            if not isinstance(transactions, list):
                raise ArcCandidateRejected(
                    "Arc full block transactions unavailable")
            indexed = {
                item.get("hash", "").lower(): item
                for item in transactions if isinstance(item, dict)
            }
            self._block_transactions[key] = indexed
            self._block_order.append(key)
            if len(self._block_order) > MAX_CACHED_BLOCKS:
                self._block_transactions.pop(self._block_order.popleft(), None)
        raw = indexed.get(hint["transactionHash"].lower())
        if raw is None:
            raise ArcCanonicalMismatch(
                "Arc Swap transaction missing from its canonical block")
        return raw

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

    async def observe(self, hint: dict,
                      raw_transaction: dict | None = None) -> list[Signal]:
        hint = validate_arc_swap_log(hint)
        tx_hash = hint["transactionHash"].lower()
        async with self._lock:
            if tx_hash in self._processed:
                return []
            raw = raw_transaction
            if raw is None:
                raw = await self._transaction_from_block(hint)
            if raw is None:
                raise RpcError("Arc transaction is not available yet")
            if not isinstance(raw, dict) or "chainId" not in raw:
                raise ArcCandidateRejected("Arc transaction chain id unavailable")
            try:
                tx = Transaction.from_rpc(raw, observation_source="arc_v4_subscription")
            except (KeyError, TypeError, ValueError) as exc:
                raise ArcCandidateRejected("invalid Arc transaction") from exc
            if tx.hash != tx_hash or tx.chain_id != R.ARC.chain_id:
                raise ArcCandidateRejected("Arc transaction identity mismatch")
            if tx.sender not in self.watchlist:
                self._remember(tx_hash)
                return []

            self.store.put_candidate(tx)
            try:
                receipt = await self.rpc.receipt(tx.hash)
                if receipt is None:
                    raise RpcError("Arc receipt is not available yet")
                if (not isinstance(receipt, dict)
                        or not self._receipt_contains_hint(receipt, hint)):
                    raise ArcCandidateRejected(
                        "Arc subscription log not confirmed by receipt")
                signals = await self._decode(tx)
                pool_checks = await verify_signal_pools(self.rpc, signals, receipt)
                try:
                    native_checks = await verify_native_flows(
                        self.rpc, tx, receipt, signals)
                except (RpcError, ValueError, TypeError):
                    # A provider without the bounded state-diff tracer cannot
                    # promote native-USDC routes; enrich leaves them review-only.
                    native_checks = {}
                final = enrich(
                    tx, signals, receipt, self.watchlist, pool_checks, native_checks)
                for signal in final:
                    if self.store.put(signal) and self.on_signal is not None:
                        self.on_signal(signal)
                self.store.complete_candidate(
                    tx.hash, number(receipt["blockNumber"]), receipt["blockHash"])
                self._remember(tx_hash)
                return final
            except ArcCandidateRejected:
                self.store.fail_candidate(tx.hash, "ArcCandidateRejected")
                self._remember(tx_hash)
                raise
            except Exception as exc:
                self.store.fail_candidate(tx.hash, type(exc).__name__)
                raise


def _block_identity(block: object, expected_number: int) -> tuple[str, str]:
    if (not isinstance(block, dict) or number(block.get("number", -1)) != expected_number
            or not isinstance(block.get("hash"), str)
            or not isinstance(block.get("parentHash"), str)):
        raise ValueError("invalid Arc block header")
    block_hash, parent_hash = block["hash"].lower(), block["parentHash"].lower()
    if (len(block_hash) != 66 or len(parent_hash) != 66
            or not block_hash.startswith("0x") or not parent_hash.startswith("0x")):
        raise ValueError("invalid Arc block hash")
    int(block_hash[2:], 16)
    int(parent_hash[2:], 16)
    return block_hash, parent_hash


async def arc_backfill_once(rpc: ReadOnlyRpc, store: Store, observer: ArcObserver,
                            batch_size: int = 500) -> dict:
    """Scan the next bounded finalized range and advance only after processing."""
    if not 1 <= batch_size <= 2000:
        raise ValueError("invalid Arc backfill batch size")
    latest = number(await rpc.call("eth_blockNumber"))
    cursor = store.chain_cursor(ARC_CURSOR)
    initialized = cursor is None
    if cursor is None:
        # The WSS task is already starting in parallel. Scanning the current
        # finalized block closes the startup race without replaying full history.
        start = end = latest
    else:
        cursor_header = await rpc.call(
            "eth_getBlockByNumber", [hex(cursor[0]), False])
        observed_cursor_hash, _ = _block_identity(cursor_header, cursor[0])
        if observed_cursor_hash != cursor[1].lower():
            raise ArcCanonicalMismatch(
                "Arc cursor hash changed; manual review required")
        if latest <= cursor[0]:
            return {"initialized": False, "from_block": cursor[0],
                    "to_block": cursor[0], "logs": 0, "rejected": 0}
        start = cursor[0] + 1
        end = min(latest, start + batch_size - 1)
    raw_logs = await rpc.call("eth_getLogs", [{
        "fromBlock": hex(start), "toBlock": hex(end),
        "address": R.ARC.v4_manager, "topics": [ARC_SWAP_TOPIC],
    }])
    if not isinstance(raw_logs, list):
        raise ValueError("invalid Arc eth_getLogs result")
    logs = sorted(
        (validate_arc_swap_log(item) for item in raw_logs),
        key=lambda item: (number(item["blockNumber"]), number(item["logIndex"])),
    )
    rejected = 0
    by_block: dict[int, list[dict]] = {}
    for log in logs:
        height = number(log["blockNumber"])
        if height < start or height > end:
            raise ValueError("Arc log is outside requested range")
        by_block.setdefault(height, []).append(log)

    verified_blocks: dict[int, tuple[str, str]] = {}
    for height, block_logs in sorted(by_block.items()):
        block = await rpc.call("eth_getBlockByNumber", [hex(height), True])
        block_hash, parent_hash = _block_identity(block, height)
        if any(item["blockHash"].lower() != block_hash for item in block_logs):
            raise ArcCanonicalMismatch("Arc log block hash changed during backfill")
        transactions = block.get("transactions")
        if not isinstance(transactions, list):
            raise ValueError("Arc full block transactions unavailable")
        indexed = {
            item.get("hash", "").lower(): item
            for item in transactions if isinstance(item, dict)
        }
        for log in block_logs:
            raw = indexed.get(log["transactionHash"].lower())
            if raw is None:
                raise ArcCanonicalMismatch(
                    "Arc Swap transaction missing from its canonical block")
            try:
                await observer.observe(log, raw_transaction=raw)
            except ArcCandidateRejected:
                rejected += 1
        store.record_chain_block(
            height, block_hash, parent_hash, name=ARC_CURSOR)
        verified_blocks[height] = (block_hash, parent_hash)
    if end in verified_blocks:
        end_hash = verified_blocks[end][0]
    else:
        end_header = await rpc.call("eth_getBlockByNumber", [hex(end), False])
        end_hash, _ = _block_identity(end_header, end)
    store.set_chain_cursor(end, end_hash, ARC_CURSOR)
    return {"initialized": initialized, "from_block": start, "to_block": end,
            "logs": len(logs), "rejected": rejected}


async def observe_arc(rpc: ReadOnlyRpc, ws_url: str, store: Store, watchlist: dict,
                      on_signal: Callable[[Signal], None] | None = None,
                      on_status: Callable[[str, dict], None] | None = None,
                      backfill_interval: float = 5.0,
                      backfill_batch: int = 500) -> None:
    if number(await rpc.call("eth_chainId")) != R.ARC.chain_id:
        raise ValueError("Arc RPC is connected to the wrong chain")
    if not 0.5 <= backfill_interval <= 60:
        raise ValueError("invalid Arc backfill interval")
    observer = ArcObserver(rpc, store, watchlist, on_signal)

    def status(event: str, **details) -> None:
        if on_status is not None:
            on_status(event, details)

    async def subscribe_forever() -> None:
        failures = 0
        while True:
            try:
                status("arc_ws_connecting")
                async for log in ArcSwapSubscriber(ws_url).logs():
                    failures = 0
                    try:
                        await observer.observe(log)
                    except ArcCandidateRejected as exc:
                        status("arc_candidate_rejected", error_type=type(exc).__name__)
                raise ConnectionError("Arc WebSocket closed")
            except asyncio.CancelledError:
                raise
            except ArcCanonicalMismatch:
                raise
            except Exception as exc:
                failures += 1
                status("arc_ws_reconnect", error_type=type(exc).__name__,
                       consecutive_failures=failures)
                if failures >= 8:
                    raise RuntimeError("Arc WebSocket repeatedly failed") from exc
                await asyncio.sleep(min(10.0, 2 ** (failures - 1)))

    async def backfill_forever() -> None:
        failures = 0
        while True:
            try:
                progress = await arc_backfill_once(
                    rpc, store, observer, backfill_batch)
                failures = 0
                if progress["initialized"] or progress["logs"]:
                    status("arc_backfill_progress", **progress)
                await asyncio.sleep(backfill_interval)
            except asyncio.CancelledError:
                raise
            except ArcCanonicalMismatch:
                raise
            except Exception as exc:
                failures += 1
                status("arc_backfill_error", error_type=type(exc).__name__,
                       consecutive_failures=failures)
                if failures >= 8:
                    raise RuntimeError("Arc backfill repeatedly failed") from exc
                await asyncio.sleep(min(10.0, 2 ** (failures - 1)))

    tasks = [asyncio.create_task(subscribe_forever()),
             asyncio.create_task(backfill_forever())]
    try:
        done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_EXCEPTION)
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
