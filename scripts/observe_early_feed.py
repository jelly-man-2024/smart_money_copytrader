"""Explicitly enabled, bounded read-only shadow capture; never starts live trading."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit

from smart_money.config import load_endpoint_env
from smart_money.early_shadow import JsonlSink, ShadowCollector
from smart_money.early_shadow_mysql import ShadowBusinessReader
from smart_money.relay_api import RelayPublicClient
from smart_money.rpc import ReadOnlyRpc
from smart_money.registry import CHAIN_ID


async def capture(args):
    import websockets
    load_endpoint_env()
    rpc = ReadOnlyRpc(os.environ["ROBINHOOD_RPC_URL"], concurrency=2, timeout=2)
    feed_url = os.environ["ROBINHOOD_FEED_URL"]
    if urlsplit(feed_url).scheme != "wss":
        raise ValueError("shadow_feed_requires_wss")
    if int(await rpc.call("eth_chainId"), 16) != CHAIN_ID:
        raise ValueError("shadow_wrong_chain")
    business = ShadowBusinessReader()
    wallets = await asyncio.to_thread(business.wallets)
    sink = JsonlSink(args.output)
    try:
        collector = ShadowCollector(rpc, RelayPublicClient(timeout=2), business, sink,
                                    queue_size=args.queue_size, workers=args.workers)
        # No automatic reconnect: a new run explicitly records a new session.
        async with websockets.connect(feed_url, max_size=16 * 1024 * 1024,
                                      max_queue=2, open_timeout=5, close_timeout=2) as ws:
            return await collector.run(ws, wallets, args.seconds)
    finally:
        sink.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--enable-shadow", action="store_true",
                        help="required; enables this separate read-only process only")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--queue-size", type=int, default=32)
    args = parser.parse_args(argv)
    if not args.enable_shadow:
        print(json.dumps({"shadow_enabled": False, "network_accessed": False}))
        return 0
    if (args.output is None or not 1 <= args.seconds <= 3600
            or not 1 <= args.workers <= 4 or not 1 <= args.queue_size <= 256):
        parser.error("output required; seconds 1..3600, workers 1..4, queue-size 1..256")
    if args.output.exists():
        parser.error("output must not already exist")
    try:
        result = asyncio.run(capture(args))
        print(json.dumps({"shadow_only": True, "counters": result}))
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(json.dumps({"error_type": type(exc).__name__, "shadow_only": True,
                          "broadcast_performed": False}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
