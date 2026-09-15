"""Opt-in historical Kyber call diagnostics; never imported by trading runtime.

Separate from ReadOnlyRpc.call's production allowlist. Only fixed callTracer,
chain 4663, saved unsigned Kyber calls, numbered blocks, and <=2M gas are allowed.
No state/block overrides, Javascript tracers, signing, broadcast, or ledger writes.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re

from .config import load_endpoint_env
from .kyber import decode_kyber_swap
from .models import address, number
from .registry import CHAIN_ID, KYBER_META_AGGREGATION_ROUTER_V2, NATIVE
from .rpc import ReadOnlyRpc, RpcError
from .simulation_diagnostics import MAX_CALLDATA_BYTES, rpc_error_diagnostic


def hex_bytes(value, limit):
    if (not isinstance(value, str) or len(value) > 2 + limit * 2
            or not re.fullmatch(r"0x(?:[0-9a-fA-F]{2})*", value)):
        raise ValueError("invalid or oversized hex data")
    return bytes.fromhex(value[2:])


def saved_call(diagnostic: dict, gas: int | None = None) -> dict:
    if (diagnostic.get("schema_version") != 1 or diagnostic.get("chain_id") != CHAIN_ID
            or diagnostic.get("method") != "eth_call" or diagnostic.get("provider") != "kyber"
            or diagnostic.get("calldata_omitted_size_limit") is not False):
        raise ValueError("expected complete saved Kyber simulation diagnostic")
    call = diagnostic["call"]
    if not isinstance(call, dict) or set(call) != {"from", "to", "data", "gas", "value"}:
        raise ValueError("unexpected saved call fields")
    raw = hex_bytes(call["data"], MAX_CALLDATA_BYTES)
    if hashlib.sha256(raw).hexdigest() != diagnostic.get("calldata_sha256"):
        raise ValueError("saved calldata hash mismatch")
    follower = address(call["from"])
    if (follower == NATIVE or follower != diagnostic.get("follower_wallet")
            or address(call["to"]) != KYBER_META_AGGREGATION_ROUTER_V2
            or number(call["value"]) != 0):
        raise ValueError("saved call identity mismatch")
    decoded = decode_kyber_swap(call["data"])
    if (decoded["dst_receiver"] != follower or decoded["src_token"] != diagnostic.get("input_asset")
            or decoded["amount_raw"] != diagnostic.get("amount_in_raw")
            or decoded["minimum_amount_out_raw"] != diagnostic.get("minimum_amount_out_raw")):
        raise ValueError("saved call description mismatch")
    gas = number(call["gas"]) if gas is None else gas
    if type(gas) is not int or not 21000 <= gas <= 2_000_000:
        raise ValueError("diagnostic gas must be between 21000 and 2000000")
    return {**call, "from": follower, "to": KYBER_META_AGGREGATION_ROUTER_V2, "gas": hex(gas)}


def summarize_trace(trace: dict, endpoint: str = "") -> list[dict]:
    """Preserve call paths, not arbitrary provider messages or full calldata."""
    stack, rows = [(trace, "0", 0)], []
    while stack:
        frame, path, depth = stack.pop()
        if len(rows) >= 512 or depth > 64:
            raise ValueError("trace tree exceeds diagnostic limits")
        if not isinstance(frame, dict):
            raise ValueError("invalid call frame")
        kind = frame.get("type", "")
        if not isinstance(kind, str):
            raise ValueError("invalid call frame type")
        kind = kind.upper()
        if kind not in {"CALL", "STATICCALL", "DELEGATECALL", "CALLCODE", "CREATE", "CREATE2", "SELFDESTRUCT"}:
            raise ValueError("unsupported call frame type")
        data = hex_bytes(frame.get("input", "0x"), MAX_CALLDATA_BYTES)
        output = hex_bytes(frame.get("output", "0x"), MAX_CALLDATA_BYTES)
        error = frame.get("error")
        row = {"path": path, "type": kind, "from": address(frame["from"]),
               "to": address(frame["to"]) if frame.get("to") else None,
               "selector": "0x" + data[:4].hex(), "input_bytes": len(data),
               "input_sha256": hashlib.sha256(data).hexdigest(), "output_bytes": len(output),
               "failed": bool(error), "gas_raw": str(number(frame.get("gas", 0))),
               "gas_used_raw": str(number(frame.get("gasUsed", 0)))}
        if error:
            row["failure"] = rpc_error_diagnostic("eth_call", {
                "message": error, "data": frame.get("output", "0x")}, endpoint)
        rows.append(row)
        children = frame.get("calls", [])
        if not isinstance(children, list) or len(children) + len(stack) + len(rows) > 512:
            raise ValueError("invalid or oversized child calls")
        stack.extend((child, f"{path}.{i}", depth + 1)
                     for i, child in reversed(list(enumerate(children))))
    return rows


class DiagnosticTraceRpc(ReadOnlyRpc):
    def __init__(self, url: str):
        super().__init__(url, concurrency=1, timeout=10)

    async def trace_saved_call(self, diagnostic: dict, block: int, *, allow_rpc=False, gas=None):
        if allow_rpc is not True:
            raise PermissionError("explicit read-only diagnostic opt-in required")
        if type(block) is not int or not 0 < block < 2**64:
            raise ValueError("explicit historical block number required")
        call = saved_call(diagnostic, gas)
        if number(await self.call("eth_chainId")) != CHAIN_ID:
            raise ValueError("wrong diagnostic chain")
        header = await self.call("eth_getBlockByNumber", [hex(block), False])
        if not isinstance(header, dict) or number(header.get("number", -1)) != block:
            raise ValueError("diagnostic block unavailable")
        block_hash = header.get("hash")
        if len(hex_bytes(block_hash, 32)) != 32:
            raise ValueError("invalid diagnostic block hash")
        options = {"tracer": "callTracer", "timeout": "5s", "reexec": 0,
                   "tracerConfig": {"onlyTopCall": False, "withLog": False}}
        # Deliberately scoped exception: generic call() still rejects traceCall.
        async with self.semaphore:
            trace = await asyncio.to_thread(self._request, "debug_traceCall",
                                            [call, hex(block), options], next(self.ids))
        if (not isinstance(trace, dict) or trace.get("from", "").lower() != call["from"]
                or trace.get("to", "").lower() != call["to"]
                or trace.get("input", "").lower() != call["data"].lower()):
            raise ValueError("trace root does not match saved call")
        rows = summarize_trace(trace, self.url)
        after = await self.call("eth_getBlockByNumber", [hex(block), False])
        if not isinstance(after, dict) or after.get("hash") != block_hash:
            raise ValueError("diagnostic block changed during trace")
        return {"proposal_id": diagnostic.get("proposal_id"), "block_number": block,
                "block_hash": block_hash, "block_timestamp": number(header["timestamp"]),
                "gas_limit": number(call["gas"]), "original_gas_limit": number(diagnostic["call"]["gas"]),
                "historical_trace_not_original_pending_state": True, "calls": rows,
                "read_only": True, "broadcast_performed": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--log", required=True)
    parser.add_argument("--proposal-id", required=True)
    parser.add_argument("--block", type=int, required=True)
    parser.add_argument("--gas", type=int)
    parser.add_argument("--allow-rpc", action="store_true", help="opt in to read-only historical tracing")
    args = parser.parse_args()
    if not args.allow_rpc:
        parser.error("--allow-rpc is required; no network requests made")
    diagnostics = []
    with open(args.log) as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if (isinstance(event, dict) and event.get("proposal_id") == args.proposal_id
                    and isinstance(event.get("simulation_failure"), dict)):
                diagnostics.append(event["simulation_failure"])
    if len(diagnostics) != 1:
        parser.error("expected exactly one saved simulation failure for proposal")
    load_endpoint_env()
    client = DiagnosticTraceRpc(os.environ.get("ROBINHOOD_RPC_URL", "https://rpc.mainnet.chain.robinhood.com"))
    try:
        result = asyncio.run(client.trace_saved_call(diagnostics[0], args.block, allow_rpc=True, gas=args.gas))
    except (RpcError, ValueError, TypeError, KeyError) as exc:
        print(json.dumps({"read_only": True, "broadcast_performed": False,
                          "error_type": type(exc).__name__,
                          "rpc_diagnostic": exc.diagnostic if isinstance(exc, RpcError) else None}))
        return 1
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
