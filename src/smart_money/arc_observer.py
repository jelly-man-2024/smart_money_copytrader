"""Read-only Arc mainnet ingestion, triggered by watched wallets' token moves.

Arc is subscribed by WALLET, not by venue: the ERC-20 Transfer topics carry the
sender and recipient as indexed fields, so the RPC filters server-side for the
watchlist and a whole chain of swaps never reaches us. Measured over 1500 Arc
blocks this is ~18 logs against 13752 for the v3 and v4 swap streams together,
it covers every venue at once including the v3 forks that have no singleton
contract to filter on, and it still sees a cross-chain Relay delivery, which
emits a Transfer but no swap of ours.

The WebSocket is only a low-latency hint. Every candidate is re-read through the
allowlisted HTTPS RPC client, decoded from transaction calldata, and checked
against its canonical receipt before any signal is persisted.
"""
from __future__ import annotations

import asyncio
import time
import json
from collections import deque
from collections.abc import AsyncIterator, Callable
from urllib.parse import urlsplit

import websockets

from . import registry as R
from .registry import native_to_erc20_amount
from .account_state import prestate_implementations
from .decode import Decoder
from .models import Signal, Transaction, address, number
from .native_flows import verify_native_flows
from .pools import verify_signal_pools
from .receipts import TRANSFER, direct_token_transfer_evidence, enrich
from .relay_api import RelayApiError, RelayNotReady
from .rpc import ReadOnlyRpc, RpcError
from .solver import relay_passive_buy
from .store import Store


MAX_SEEN_TRANSACTIONS = 8192
MAX_CACHED_BLOCKS = 64
# The cursor is named for what it now scans; an older "arc_v4" cursor is left
# behind rather than resumed, because it indexed a different filter.
ARC_CURSOR = "arc_wallet_transfers"
# A Relay order is often not settled yet when its destination credit lands, so
# attribution is deferred and retried rather than dropped. "Not settled yet" is
# missing evidence, never evidence of a non-purchase, so an exhausted retry
# leaves the delivery in needs_review and copies nothing.
MAX_RELAY_RETRY_ATTEMPTS = 20
RELAY_RETRY_DEADLINE_SECONDS = 900.0


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


def wallet_topic(wallet: str) -> str:
    """A wallet address as an indexed 32-byte log topic."""
    return "0x" + "0" * 24 + address(wallet)[2:]


def validate_arc_transfer_log(value: object, watchlist: dict) -> tuple[dict, list[str]]:
    """Validate the untrusted payload and name the watched wallets it touches.

    Returns the log and the watchlist wallets appearing in it. A provider that
    ignored our filter, or a token that reuses the Transfer topic with a
    different shape, is rejected rather than trusted.
    """
    if not isinstance(value, dict) or value.get("removed") is True:
        if isinstance(value, dict) and value.get("removed") is True:
            raise ValueError("removed Arc log requires canonical rescan")
        raise ValueError("invalid Arc log")
    required = ("address", "transactionHash", "blockHash", "blockNumber", "logIndex", "topics")
    if any(key not in value for key in required):
        raise ValueError("incomplete Arc log")
    address(value["address"])
    topics = value["topics"]
    # Exactly three topics is the ERC-20 shape (from, to indexed; value is not).
    # An ERC-721 Transfer indexes the token id as a fourth topic and is not a
    # fungible balance change, so it is not a candidate here.
    if (not isinstance(topics, list) or len(topics) != 3
            or not all(isinstance(item, str) for item in topics)
            or topics[0].lower() != TRANSFER):
        raise ValueError("Arc log is not an ERC-20 Transfer")
    wallets = []
    for topic in topics[1:]:
        if len(topic) != 66 or not topic.startswith("0x") or int(topic[2:26], 16) != 0:
            raise ValueError("invalid Arc Transfer party topic")
        party = "0x" + topic[-40:].lower()
        if party in watchlist and party not in wallets:
            wallets.append(party)
    if not wallets:
        raise ValueError("Arc Transfer does not touch a watched wallet")
    for field in ("transactionHash", "blockHash"):
        item = value[field]
        if (not isinstance(item, str) or len(item) != 66 or not item.startswith("0x")):
            raise ValueError(f"invalid Arc log {field}")
        int(item[2:], 16)
    number(value["blockNumber"])
    number(value["logIndex"])
    return value, wallets


class ArcWalletSubscriber:
    """WSS subscriber scoped to the watchlist, with duplicate suppression.

    Two subscriptions are needed because one filter cannot express "the wallet
    is the sender OR the recipient": a topic list constrains one position. A
    swap shows the wallet on both sides and arrives twice, which the per
    transaction suppression collapses.
    """

    def __init__(self, url: str, watchlist: dict):
        self.url = validate_arc_ws_url(url)
        self.watchlist = watchlist
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

    def _filters(self) -> list[dict]:
        parties = [wallet_topic(wallet) for wallet in sorted(self.watchlist)]
        if not parties:
            raise ValueError("Arc subscription requires a watchlist")
        return [{"topics": [TRANSFER, parties, None]},
                {"topics": [TRANSFER, None, parties]}]

    async def logs(self) -> AsyncIterator[dict]:
        async with websockets.connect(
                self.url, open_timeout=15, close_timeout=5,
                max_size=1024 * 1024, max_queue=256, ping_interval=20,
                ping_timeout=20) as socket:
            subscriptions = set()
            for index, log_filter in enumerate(self._filters(), start=1):
                await socket.send(json.dumps({
                    "jsonrpc": "2.0", "id": index, "method": "eth_subscribe",
                    "params": ["logs", log_filter]}, separators=(",", ":")))
                acknowledgement = json.loads(await socket.recv())
                if (not isinstance(acknowledgement, dict)
                        or acknowledgement.get("id") != index
                        or not isinstance(acknowledgement.get("result"), str)
                        or not acknowledgement["result"]
                        or acknowledgement["result"] in subscriptions):
                    raise ValueError("invalid Arc subscription acknowledgement")
                subscriptions.add(acknowledgement["result"])
            async for raw in socket:
                document = json.loads(raw)
                if (not isinstance(document, dict)
                        or document.get("method") != "eth_subscription"):
                    raise ValueError("unexpected Arc WebSocket message")
                params = document.get("params")
                if (not isinstance(params, dict)
                        or params.get("subscription") not in subscriptions):
                    raise ValueError("Arc subscription identity mismatch")
                log, _wallets = validate_arc_transfer_log(
                    params.get("result"), self.watchlist)
                if self._first_transaction_log(log):
                    yield log


class ArcObserver:
    """Turn verified Arc log hints into the existing candidate/signal model."""

    def __init__(self, rpc: ReadOnlyRpc, store: Store, watchlist: dict,
                 on_signal: Callable[[Signal], None] | None = None,
                 relay_client=None,
                 on_status: Callable[[str, dict], None] | None = None):
        self.rpc = rpc
        self.store = store
        self.watchlist = watchlist
        self.relay_client = relay_client
        self.on_status = on_status
        self.decoder = Decoder(watchlist, chain_id=R.ARC.chain_id)
        self.on_signal = on_signal
        self._lock = asyncio.Lock()
        self._processed_order: deque[str] = deque()
        self._processed: set[str] = set()
        self._block_order: deque[tuple[int, str]] = deque()
        self._block_transactions: dict[tuple[int, str], dict[str, dict]] = {}
        # tx_hash -> {"hint": validated log, "attempts": int, "first_seen": float}
        self._relay_pending: dict[str, dict] = {}
        self._relay_pending_now = False

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
            number(hint["logIndex"]), address(hint["address"]),
            hint["topics"][0].lower(),
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

    def _status(self, event: str, **details) -> None:
        if self.on_status is not None:
            self.on_status(event, details)

    def _normalize_native_scale(self, signal: Signal) -> Signal:
        """Restate a native-denominated leg in the ERC-20 scale used downstream.

        Arc's gas asset and its enshrined ERC-20 are one balance counted two
        ways, 18 decimals natively and 6 through the token. enrich reads a
        native leg out of the state diff and an ERC-20 leg out of the Transfer
        logs, so a signal can carry both scales at once and every later
        comparison — budget bucket, amount rule, price deviation — would be off
        by a factor of a trillion. Quoting and execution use the ERC-20 form, so
        that is the scale the amounts are restated in.

        The asset identity is left alone: the pool key and the state diff record
        what the chain actually did, and rewriting the native sentinel would
        break the pool-key checks that depend on it.
        """
        chain = R.chain_for(signal.chain_id)
        divisor = chain.native_to_erc20_divisor
        if divisor == 1 or R.NATIVE not in (signal.token_in, signal.token_out):
            return signal
        native_in = signal.token_in == R.NATIVE
        native_out = signal.token_out == R.NATIVE
        fields = []
        if native_in:
            fields += ["amount_in_raw", "actual_input_debit_raw"]
        if native_out:
            fields += ["amount_out_raw", "actual_output_credit_raw"]
        if signal.amount_limit_raw is not None:
            # The limit bounds the output of an exact-input swap and the input of
            # an exact-output one. With no direction recorded it cannot be tied to
            # a leg, so the signal is held rather than rescaled on a guess.
            if signal.exact_in is None:
                signal.stage = "needs_review"
                signal.reasons.append("native_amount_limit_scale_undetermined")
                return signal
            if (native_out if signal.exact_in else native_in):
                fields.append("amount_limit_raw")
        dust = {}
        for field in fields:
            container = signal.evidence if field.startswith("actual_") else None
            raw = (container.get(field) if container is not None
                   else getattr(signal, field, None))
            if raw is None:
                continue
            try:
                scaled, remainder = native_to_erc20_amount(int(raw), chain)
            except (TypeError, ValueError):
                signal.stage = "needs_review"
                signal.reasons.append("native_amount_not_rescalable")
                return signal
            if container is not None:
                container[field] = str(scaled)
            else:
                setattr(signal, field, str(scaled))
            if remainder:
                dust[field] = str(remainder)
        signal.evidence["native_scale_normalization"] = {
            "rule": "native-to-erc20-scale-v1",
            "divisor": str(divisor),
            "rescaled_fields": fields,
            "dropped_dust_raw": dust,
            "note": ("amounts are in the ERC-20 scale; native_flow_verification "
                     "keeps the chain's own 18-decimal values"),
        }
        return signal

    async def _associate_relay_delivery(self, signal: Signal, tx: Transaction,
                                        receipt: dict) -> Signal:
        """Turn a credit with no local debit into a BUY when Relay orchestrated it.

        A cross-chain fill debits the wallet on the source chain, so on Arc it
        looks like a plain receipt of tokens and enrich holds it for review. The
        Relay order is what binds the two ends, and it is checked against this
        receipt amount for amount in relay_passive_buy.

        The filter is negative on purpose: only a transaction that is itself a
        plain ERC-20 transfer is skipped. Requiring the credit to come straight
        from a Relay contract would be tighter but wrong, because a fill can be
        settled through an intermediary and would then be silently dropped.
        """
        if (self.relay_client is None
                or signal.behavior not in {"EXTERNAL_DELIVERY_CANDIDATE", "INCOMING_TRANSFER"}
                or signal.stage != "needs_review"):
            return signal
        for key in ("relay_lookup_skipped", "relay_lookup_skip_reason",
                    "relay_lookup_skip_evidence"):
            signal.evidence.pop(key, None)
        skip_evidence = direct_token_transfer_evidence(tx, receipt, signal.wallet)
        if skip_evidence is not None:
            signal.evidence.update({
                "relay_lookup_skipped": True,
                "relay_lookup_skip_reason": "direct_token_transfer",
                "relay_lookup_skip_evidence": skip_evidence,
            })
            self._status("arc_relay_lookup_skipped", source_event_id=signal.event_id,
                         reason="direct_token_transfer")
            return signal
        try:
            document = await self.relay_client.lookup_by_destination_hash(signal.tx_hash)
            associated = relay_passive_buy(document, signal)
        except RelayNotReady:
            # The order is not settled in Relay's view yet. Leave the signal for
            # review; backfill re-reads the transaction from canonical state.
            self._relay_pending_now = True
            self._status("arc_relay_lookup_pending", source_event_id=signal.event_id)
            return signal
        except (RelayApiError, RpcError, ValueError) as exc:
            # Failing to obtain an attribution is not evidence that the delivery
            # was not a purchase: a rate-limited lookup, a transport failure or a
            # half-written order all land here. Queue it like a pending order and
            # let the bounded retry decide; exhaustion still leaves it unattributed.
            self._relay_pending_now = True
            self._status("arc_relay_association_deferred",
                         source_event_id=signal.event_id, error_type=type(exc).__name__)
            return signal
        self._status("arc_relay_buy_associated", source_event_id=associated.event_id,
                     relay_order_id=associated.evidence.get("relay_order_id"))
        return associated

    async def observe(self, hint: dict,
                      raw_transaction: dict | None = None) -> list[Signal]:
        hint, _wallets = validate_arc_transfer_log(hint, self.watchlist)
        tx_hash = hint["transactionHash"].lower()
        self._relay_pending_now = False
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
            # No sender gate: the wallet was matched on the Transfer itself, and a
            # cross-chain Relay delivery or a router refund is sent by somebody
            # else entirely. Attribution stays with enrich, which reads each
            # watched wallet's own balance changes out of the receipt.

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
                final = [self._normalize_native_scale(item) for item in final]
                final = [await self._associate_relay_delivery(item, tx, receipt)
                         for item in final]
                for signal in final:
                    if self.store.put(signal) and self.on_signal is not None:
                        self.on_signal(signal)
                if self._relay_pending_now:
                    # Leave the candidate open and unremembered: retry_pending_relay
                    # re-reads it from canonical state once Relay has settled, and
                    # store.put then upgrades needs_review to relay_buy_evidenced.
                    entry = self._relay_pending.setdefault(
                        tx_hash, {"hint": hint, "attempts": 0, "first_seen": time.time()})
                    entry["attempts"] += 1
                    return final
                self._relay_pending.pop(tx_hash, None)
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


    async def retry_pending_relay(self) -> dict:
        """Re-attribute deliveries whose Relay order had not settled yet.

        Called from the backfill loop, because the scan cursor has already moved
        past these blocks and would never revisit them. Each retry re-reads the
        transaction from canonical state, so a settled order upgrades the stored
        signal; giving up leaves it in needs_review and copies nothing.
        """
        retried = resolved = abandoned = 0
        for tx_hash, entry in list(self._relay_pending.items()):
            expired = (time.time() - entry["first_seen"] > RELAY_RETRY_DEADLINE_SECONDS
                       or entry["attempts"] >= MAX_RELAY_RETRY_ATTEMPTS)
            if expired:
                self._relay_pending.pop(tx_hash, None)
                self._remember(tx_hash)
                abandoned += 1
                self._status("arc_relay_retry_exhausted", source_tx_hash=tx_hash,
                             attempts=entry["attempts"])
                continue
            retried += 1
            try:
                await self.observe(entry["hint"])
            except (ArcCandidateRejected, RpcError, ArcCanonicalMismatch) as exc:
                entry["attempts"] += 1
                self._status("arc_relay_retry_error", source_tx_hash=tx_hash,
                             error_type=type(exc).__name__)
                continue
            if tx_hash not in self._relay_pending:
                resolved += 1
                self._status("arc_relay_retry_resolved", source_tx_hash=tx_hash)
        return {"retried": retried, "resolved": resolved, "abandoned": abandoned,
                "pending": len(self._relay_pending)}


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
    cursor = store.chain_cursor(ARC_CURSOR, R.ARC.chain_id)
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
    # Same wallet filter as the subscription, applied server-side: the canonical
    # lane reads a handful of logs per batch instead of every swap on the chain,
    # so a batch cannot outgrow the transport's response limit however busy Arc
    # is, and falling behind never becomes unrecoverable.
    parties = [wallet_topic(wallet) for wallet in sorted(observer.watchlist)]
    if not parties:
        raise ValueError("Arc backfill requires a watchlist")
    raw_logs = []
    for topics in ([TRANSFER, parties, None], [TRANSFER, None, parties]):
        batch = await rpc.call("eth_getLogs", [{
            "fromBlock": hex(start), "toBlock": hex(end), "topics": topics,
        }])
        if not isinstance(batch, list):
            raise ValueError("invalid Arc eth_getLogs result")
        raw_logs.extend(batch)
    seen_positions = set()
    logs = []
    for item in raw_logs:
        validated, _wallets = validate_arc_transfer_log(item, observer.watchlist)
        # A swap puts the wallet on both sides, so the two directions overlap.
        position = (number(validated["blockNumber"]), number(validated["logIndex"]))
        if position in seen_positions:
            continue
        seen_positions.add(position)
        logs.append(validated)
    logs.sort(key=lambda item: (number(item["blockNumber"]), number(item["logIndex"])))
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
        store.record_chain_block(height, block_hash, parent_hash,
                                 name=ARC_CURSOR, chain_id=R.ARC.chain_id)
        verified_blocks[height] = (block_hash, parent_hash)
    if end in verified_blocks:
        end_hash = verified_blocks[end][0]
    else:
        end_header = await rpc.call("eth_getBlockByNumber", [hex(end), False])
        end_hash, _ = _block_identity(end_header, end)
    store.set_chain_cursor(end, end_hash, ARC_CURSOR, R.ARC.chain_id)
    return {"initialized": initialized, "from_block": start, "to_block": end,
            "logs": len(logs), "rejected": rejected}


async def observe_arc(rpc: ReadOnlyRpc, ws_url: str, store: Store, watchlist: dict,
                      on_signal: Callable[[Signal], None] | None = None,
                      on_status: Callable[[str, dict], None] | None = None,
                      backfill_interval: float = 5.0,
                      backfill_batch: int = 500,
                      relay_client=None) -> None:
    if number(await rpc.call("eth_chainId")) != R.ARC.chain_id:
        raise ValueError("Arc RPC is connected to the wrong chain")
    if not 0.5 <= backfill_interval <= 60:
        raise ValueError("invalid Arc backfill interval")
    def status(event: str, **details) -> None:
        if on_status is not None:
            on_status(event, details)

    # ArcObserver reports as on_status(event, details_dict) while `status` above
    # takes keywords, so the two contracts are bridged here. Without this every
    # status the observer itself raises — the Relay attribution branch, which only
    # fires when a watched wallet receives tokens with no outflow — is a TypeError.
    observer = ArcObserver(rpc, store, watchlist, on_signal, relay_client=relay_client,
                           on_status=lambda event, details: status(event, **details))

    async def subscribe_forever() -> None:
        failures = 0
        while True:
            try:
                status("arc_ws_connecting")
                async for log in ArcWalletSubscriber(ws_url, watchlist).logs():
                    failures = 0
                    try:
                        await observer.observe(log)
                    except ArcCandidateRejected as exc:
                        status("arc_candidate_rejected", error_type=type(exc).__name__)
                    except RpcError as exc:
                        # The WSS hint can outrun the HTTPS RPC's view of the newest
                        # block/receipt (not available yet). The hint is optional and
                        # backfill re-covers the swap from canonical state, so defer
                        # this one instead of tearing down the whole subscription.
                        # A real reorg raises ArcCanonicalMismatch (not RpcError) and
                        # still surfaces loudly.
                        status("arc_hint_deferred", error_type=type(exc).__name__)
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
                retries = await observer.retry_pending_relay()
                if retries["retried"] or retries["abandoned"]:
                    status("arc_relay_retry_sweep", **retries)
                await asyncio.sleep(backfill_interval)
            except asyncio.CancelledError:
                raise
            except ArcCanonicalMismatch:
                raise
            except Exception as exc:
                failures += 1
                status("arc_backfill_error", error_type=type(exc).__name__,
                       consecutive_failures=failures)
                if failures >= 30:
                    # Backfill only tops up the real-time WSS lane; tolerate long
                    # runs of transient RPC blips before giving up (the conn pool
                    # now drops idle connections, so these should be rare).
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
