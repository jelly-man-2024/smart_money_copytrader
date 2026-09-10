from __future__ import annotations

import json
from pathlib import Path
import sqlite3

from .models import Signal

STAGE_RANK = {"intent": 0, "execution_observed": 1, "needs_review": 1, "swap_evidenced": 2, "failed": 3}


class Store:
    def __init__(self, path: str | Path):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(str(path))
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("""CREATE TABLE IF NOT EXISTS signals (
            event_id TEXT PRIMARY KEY, tx_hash TEXT NOT NULL, stage_rank INTEGER NOT NULL,
            payload TEXT NOT NULL, updated_at TEXT DEFAULT CURRENT_TIMESTAMP)""")
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
