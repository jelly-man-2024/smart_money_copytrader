"""Minimal endpoint-only dotenv loading; wallet secrets are deliberately ignored."""
from __future__ import annotations

import os
from pathlib import Path

ALLOWED_ENV_KEYS = frozenset({"ROBINHOOD_RPC_URL", "ROBINHOOD_FEED_URL"})
MAX_ENV_BYTES = 64 * 1024


def load_endpoint_env(path: str | Path = ".env") -> set[str]:
    source = Path(path)
    if not source.is_file():
        return set()
    if source.stat().st_size > MAX_ENV_BYTES:
        raise ValueError("endpoint environment file is too large")
    loaded = set()
    for raw in source.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if key not in ALLOWED_ENV_KEYS or key in os.environ:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if not value or "\x00" in value or "\n" in value or "\r" in value:
            raise ValueError(f"invalid endpoint configuration: {key}")
        os.environ[key] = value
        loaded.add(key)
    return loaded
