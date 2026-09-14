"""Bounded canonical L2 scanning used to recover RPC-visible feed gaps."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Transaction, number
from .receipts import TRANSFER

MAX_MATCHING_LOGS_PER_BLOCK = 10000
MAX_MATCHING_LOGS_PER_RANGE = 10000
MAX_RANGE_BLOCKS = 5000


class ReorgDetected(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanResult:
    initialized: bool = False
    blocks: int = 0
    candidates: int = 0
    passive_candidates: int = 0


@dataclass(frozen=True)
class ReorgResolution:
    common_ancestor: int
    orphaned_signals: int
    candidates_requeued: int


def relevant(tx: Transaction, watchlist: dict, watched_bytes: list[bytes]) -> bool:
    return tx.sender in watchlist or any(raw in tx.data for raw in watched_bytes)


class BlockScanner:
    """Address-filtered canonical scanning.

    Instead of pulling every full block, one scan asks the RPC for ERC-20
    ``Transfer`` logs sent from or delivered to the watched wallets across a
    range of blocks, then fetches only the transactions that matched. Every
    copyable BUY or SELL moves an ERC-20 balance of the smart wallet, so the
    range query cannot miss a trade; it deliberately ignores approvals and other
    calls that move no token. Parent-hash continuity is verified at the range
    start, at every block that produced a hit and at the range end, so reorg
    detection is per range rather than per block.
    """

    def __init__(self, rpc, store, watchlist: dict, confirmations: int = 2,
                 max_blocks: int = 20, progress=None):
        if confirmations < 0 or not 1 <= max_blocks <= MAX_RANGE_BLOCKS:
            raise ValueError("invalid backfill limits")
        self.rpc = rpc
        self.store = store
        self.watchlist = watchlist
        self.watched_bytes = [bytes.fromhex(a[2:]) for a in watchlist]
        self.recipient_topics = {"0x" + a[2:].rjust(64, "0") for a in watchlist}
        self.confirmations = confirmations
        self.max_blocks = max_blocks
        self.progress = progress

    async def _block(self, height: int, full: bool) -> dict:
        block = await self.rpc.call("eth_getBlockByNumber", [hex(height), full])
        if not isinstance(block, dict) or number(block.get("number", -1)) != height:
            raise ValueError("RPC returned an invalid block")
        if not isinstance(block.get("hash"), str) or not isinstance(block.get("parentHash"), str):
            raise ValueError("RPC block hashes missing")
        return block

    async def scan_once(self) -> ScanResult:
        latest = number(await self.rpc.call("eth_blockNumber"))
        safe_head = max(0, latest - self.confirmations)
        cursor = self.store.chain_cursor()
        if cursor is None:
            block = await self._block(safe_head, False)
            self.store.record_chain_block(safe_head, block["hash"], block["parentHash"])
            return ScanResult(initialized=True)
        if self.store.chain_block_hash(cursor[0]) is None:
            anchor = await self._block(cursor[0], False)
            if anchor["hash"].lower() != cursor[1].lower():
                raise ReorgDetected("stored cursor is no longer canonical")
            self.store.record_chain_block(cursor[0], anchor["hash"], anchor["parentHash"])
        if safe_head <= cursor[0]:
            return ScanResult()

        start, end = cursor[0] + 1, min(safe_head, cursor[0] + self.max_blocks)
        first = await self._block(start, False)
        if first["parentHash"].lower() != cursor[1].lower():
            raise ReorgDetected(
                f"canonical parent mismatch at block {start}; explicit rewind required"
            )
        wallet_topics = sorted(self.recipient_topics)
        hits: dict[str, int] = {}
        for position, topics in (("from", [TRANSFER, wallet_topics, None]),
                                 ("to", [TRANSFER, None, wallet_topics])):
            for log in await self._logs(start, end, topics):
                if not isinstance(log, dict) or log.get("removed", False):
                    continue
                log_topics = log.get("topics", [])
                if (len(log_topics) != 3 or log_topics[0].lower() != TRANSFER
                        or log_topics[1 if position == "from" else 2].lower()
                        not in self.recipient_topics):
                    continue
                tx_hash = str(log.get("transactionHash", "")).lower()
                height = number(log.get("blockNumber", -1))
                if len(tx_hash) != 66 or not start <= height <= end:
                    raise ValueError("Transfer log outside the scanned range")
                hits[tx_hash] = height
        headers = {start: first}
        blocks = candidates = passive_candidates = 0
        by_height: dict[int, list[str]] = {}
        for tx_hash, height in hits.items():
            by_height.setdefault(height, []).append(tx_hash)
        for height in sorted(by_height):
            before_candidates, before_passive = candidates, passive_candidates
            header = headers.get(height) or await self._block(height, False)
            headers[height] = header
            timestamp = number(header.get("timestamp", 0))
            for tx_hash in sorted(by_height[height]):
                raw = await self.rpc.call("eth_getTransactionByHash", [tx_hash])
                if (not isinstance(raw, dict) or str(raw.get("hash", "")).lower() != tx_hash
                        or number(raw.get("blockNumber", -1)) != height
                        or str(raw.get("blockHash", "")).lower() != header["hash"].lower()):
                    raise ValueError("matched transaction is not in the canonical block")
                source = dict(raw)
                source["_timestamp"] = timestamp
                tx = Transaction.from_rpc(source, observation_source="backfill")
                if self.store.put_candidate(tx):
                    candidates += 1
                    if not relevant(tx, self.watchlist, self.watched_bytes):
                        passive_candidates += 1
            self.store.record_chain_block(height, header["hash"], header["parentHash"])
            if self.progress:
                self.progress(candidates - before_candidates,
                              passive_candidates - before_passive)
        if end not in headers:
            headers[end] = await self._block(end, False)
        self.store.record_chain_block(end, headers[end]["hash"], headers[end]["parentHash"])
        blocks = end - cursor[0]
        if self.progress and blocks > len(by_height):
            self.progress(0, 0)
        return ScanResult(blocks=blocks, candidates=candidates,
                          passive_candidates=passive_candidates)

    async def _logs(self, start: int, end: int, topics: list) -> list:
        """Fetch matching logs, halving the range when the RPC or the cap refuses it."""
        try:
            logs = await self.rpc.call("eth_getLogs", [{
                "fromBlock": hex(start), "toBlock": hex(end), "topics": topics,
            }])
            if not isinstance(logs, list):
                raise ValueError("invalid Transfer log response")
            if len(logs) <= MAX_MATCHING_LOGS_PER_RANGE:
                return logs
            if start == end:
                raise ValueError("excessive matching Transfer logs in one block")
        except ValueError:
            raise
        except Exception:
            if start == end:
                raise
        middle = (start + end) // 2
        return (await self._logs(start, middle, topics)
                + await self._logs(middle + 1, end, topics))

    async def reconcile_reorg(self, max_depth: int = 64) -> ReorgResolution:
        cursor = self.store.chain_cursor()
        if cursor is None:
            raise ReorgDetected("cannot reconcile without a chain cursor")
        floor = max(0, cursor[0] - max_depth)
        for height in range(cursor[0], floor - 1, -1):
            stored_hash = self.store.chain_block_hash(height)
            if stored_hash is None:
                continue
            block = await self._block(height, False)
            if block["hash"].lower() == stored_hash.lower():
                signals, candidates = self.store.rewind_chain(height, stored_hash)
                return ReorgResolution(height, signals, candidates)
        raise ReorgDetected(f"no common ancestor within {max_depth} blocks")
