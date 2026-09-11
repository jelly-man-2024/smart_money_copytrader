from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import time

from .models import Signal, Transaction

STAGE_RANK = {"intent": 0, "execution_observed": 1, "needs_review": 1, "swap_evidenced": 2, "failed": 3}
MAX_CANDIDATE_ATTEMPTS = 8


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS signals (
            event_id TEXT PRIMARY KEY, tx_hash TEXT NOT NULL, stage_rank INTEGER NOT NULL,
            payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS candidates (
            tx_hash TEXT PRIMARY KEY, payload TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('pending','queued','retry','complete','failed')),
            attempts INTEGER NOT NULL DEFAULT 0, next_attempt_at REAL NOT NULL DEFAULT 0,
            last_error TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS chain_cursors (
            name TEXT PRIMARY KEY, block_number INTEGER NOT NULL, block_hash TEXT NOT NULL,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
        self.connection.commit()

    def recover_inflight(self) -> int:
        recovered = self.connection.execute(
            "UPDATE candidates SET status='pending', updated_at=CURRENT_TIMESTAMP WHERE status='queued'"
        ).rowcount
        self.connection.commit()
        return max(recovered, 0)

    @staticmethod
    def _transaction_payload(tx: Transaction) -> str:
        return json.dumps({
            "hash": tx.hash, "sender": tx.sender, "to": tx.to, "data": "0x" + tx.data.hex(),
            "value": str(tx.value), "chain_id": tx.chain_id, "nonce": tx.nonce,
            "tx_type": tx.tx_type, "sequence": tx.sequence, "timestamp": tx.timestamp,
            "received_at": tx.received_at, "fresh": tx.fresh,
            "observation_source": tx.observation_source,
        }, sort_keys=True)

    @staticmethod
    def _transaction(payload: str) -> Transaction:
        row = json.loads(payload)
        row["data"] = bytes.fromhex(row["data"][2:])
        row["value"] = int(row["value"])
        return Transaction(**row)

    def put_candidate(self, tx: Transaction) -> bool:
        cursor = self.connection.execute(
            "INSERT OR IGNORE INTO candidates(tx_hash,payload,status) VALUES(?,?,'pending')",
            (tx.hash, self._transaction_payload(tx)),
        )
        self.connection.commit()
        return cursor.rowcount == 1

    def claim_candidates(self, limit: int, now: float | None = None) -> list[Transaction]:
        if limit <= 0:
            return []
        now = time.time() if now is None else now
        rows = self.connection.execute(
            """SELECT tx_hash,payload FROM candidates
               WHERE status IN ('pending','retry') AND next_attempt_at<=?
               ORDER BY created_at,tx_hash LIMIT ?""",
            (now, limit),
        ).fetchall()
        claimed = []
        for tx_hash, payload in rows:
            cursor = self.connection.execute(
                """UPDATE candidates SET status='queued', attempts=attempts+1,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE tx_hash=? AND status IN ('pending','retry') AND next_attempt_at<=?""",
                (tx_hash, now),
            )
            if cursor.rowcount == 1:
                claimed.append(self._transaction(payload))
        self.connection.commit()
        return claimed

    def retry_candidate(self, tx_hash: str, reason: str,
                        now: float | None = None) -> tuple[int, float | None]:
        now = time.time() if now is None else now
        row = self.connection.execute(
            "SELECT attempts FROM candidates WHERE tx_hash=?", (tx_hash,)
        ).fetchone()
        if row is None:
            raise KeyError("unknown candidate")
        attempts = row[0]
        if attempts >= MAX_CANDIDATE_ATTEMPTS:
            self.fail_candidate(tx_hash, reason + "_retry_exhausted")
            return attempts, None
        delay = float(min(60, 2 ** min(attempts, 6)))
        self.connection.execute(
            """UPDATE candidates SET status='retry', next_attempt_at=?, last_error=?,
               updated_at=CURRENT_TIMESTAMP WHERE tx_hash=?""",
            (now + delay, reason, tx_hash),
        )
        self.connection.commit()
        return attempts, delay

    def complete_candidate(self, tx_hash: str) -> None:
        self.connection.execute(
            """UPDATE candidates SET status='complete', next_attempt_at=0, last_error=NULL,
               updated_at=CURRENT_TIMESTAMP WHERE tx_hash=?""",
            (tx_hash,),
        )
        self.connection.commit()

    def fail_candidate(self, tx_hash: str, reason: str) -> None:
        self.connection.execute(
            """UPDATE candidates SET status='failed', next_attempt_at=0, last_error=?,
               updated_at=CURRENT_TIMESTAMP WHERE tx_hash=?""",
            (reason, tx_hash),
        )
        self.connection.commit()

    def candidate_counts(self) -> dict[str, int]:
        counts = {status: count for status, count in self.connection.execute(
            "SELECT status,COUNT(*) FROM candidates GROUP BY status"
        )}
        return {status: counts.get(status, 0) for status in (
            "pending", "queued", "retry", "complete", "failed"
        )}

    def chain_cursor(self, name: str = "canonical_l2") -> tuple[int, str] | None:
        row = self.connection.execute(
            "SELECT block_number,block_hash FROM chain_cursors WHERE name=?", (name,)
        ).fetchone()
        return (row[0], row[1]) if row else None

    def set_chain_cursor(self, block_number: int, block_hash: str,
                         name: str = "canonical_l2") -> None:
        if block_number < 0 or not isinstance(block_hash, str) or not block_hash.startswith("0x"):
            raise ValueError("invalid chain cursor")
        old = self.chain_cursor(name)
        if old and block_number < old[0]:
            raise ValueError("chain cursor rewind requires explicit reorg handling")
        self.connection.execute("""INSERT INTO chain_cursors(name,block_number,block_hash) VALUES(?,?,?)
            ON CONFLICT(name) DO UPDATE SET block_number=excluded.block_number,
            block_hash=excluded.block_hash, updated_at=CURRENT_TIMESTAMP""",
            (name, block_number, block_hash.lower()))
        self.connection.commit()

    def put(self, signal: Signal) -> bool:
        payload = json.dumps(signal.to_dict(), ensure_ascii=False, sort_keys=True)
        rank = STAGE_RANK[signal.stage]
        old = self.connection.execute("SELECT stage_rank, payload FROM signals WHERE event_id=?", (signal.event_id,)).fetchone()
        if old and (old[0] > rank or old[1] == payload):
            return False
        self.connection.execute("""INSERT INTO signals(event_id, tx_hash, stage_rank, payload) VALUES(?,?,?,?)
            ON CONFLICT(event_id) DO UPDATE SET stage_rank=excluded.stage_rank,
            payload=excluded.payload, updated_at=CURRENT_TIMESTAMP""", (signal.event_id, signal.tx_hash, rank, payload))
        self.connection.commit()
        return True

    def rows(self):
        for (payload,) in self.connection.execute("SELECT payload FROM signals ORDER BY event_id"):
            yield json.loads(payload)

    def close(self):
        self.connection.close()
