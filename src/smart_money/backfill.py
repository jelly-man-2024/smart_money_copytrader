"""Bounded canonical L2 scanning used to recover RPC-visible feed gaps."""
from __future__ import annotations

from dataclasses import dataclass

from .models import Transaction, number
from .receipts import TRANSFER

MAX_MATCHING_LOGS_PER_BLOCK = 10000


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
    def __init__(self, rpc, store, watchlist: dict, confirmations: int = 2,
                 max_blocks: int = 20, progress=None):
        if confirmations < 0 or not 1 <= max_blocks <= 1000:
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

        end = min(safe_head, cursor[0] + self.max_blocks)
        previous_hash = cursor[1].lower()
        blocks = candidates = passive_candidates = 0
        for height in range(cursor[0] + 1, end + 1):
            before_candidates = candidates
            before_passive = passive_candidates
            block = await self._block(height, True)
            if block["parentHash"].lower() != previous_hash:
                raise ReorgDetected(
                    f"canonical parent mismatch at block {height}; explicit rewind required"
                )
            timestamp = number(block.get("timestamp", 0))
            transactions = block.get("transactions", [])
            if not isinstance(transactions, list):
                raise ValueError("RPC block transactions missing")
            by_hash = {}
            for row in transactions:
                if not isinstance(row, dict):
                    raise ValueError("full block transaction missing")
                source = dict(row)
                source["_timestamp"] = timestamp
                tx = Transaction.from_rpc(source, observation_source="backfill")
                by_hash[tx.hash] = tx
                if relevant(tx, self.watchlist, self.watched_bytes) and self.store.put_candidate(tx):
                    candidates += 1
            logs = await self.rpc.call("eth_getLogs", [{
                "fromBlock": hex(height), "toBlock": hex(height),
                "topics": [TRANSFER, None, sorted(self.recipient_topics)],
            }])
            if not isinstance(logs, list) or len(logs) > MAX_MATCHING_LOGS_PER_BLOCK:
                raise ValueError("invalid or excessive matching Transfer logs")
            passive_hashes = set()
            for log in logs:
                if not isinstance(log, dict):
                    continue
                topics = log.get("topics", [])
                if (log.get("removed", False) or len(topics) != 3
                        or topics[0].lower() != TRANSFER
                        or topics[2].lower() not in self.recipient_topics):
                    continue
                tx_hash = log.get("transactionHash", "").lower()
                if tx_hash not in by_hash:
                    raise ValueError("Transfer log transaction is missing from full block")
                passive_hashes.add(tx_hash)
            for tx_hash in passive_hashes:
                if self.store.put_candidate(by_hash[tx_hash]):
                    candidates += 1
                    passive_candidates += 1
            self.store.record_chain_block(height, block["hash"], block["parentHash"])
            if self.progress:
                self.progress(candidates - before_candidates,
                              passive_candidates - before_passive)
            previous_hash = block["hash"].lower()
            blocks += 1
        return ScanResult(blocks=blocks, candidates=candidates,
                          passive_candidates=passive_candidates)

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
