"""Bounded, public-only evidence for failed unsigned eth_call simulations.

No database, network, key access, retry or execution decision lives here.
Provider messages and arbitrary response objects are deliberately not retained.
"""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import parse_qsl, unquote, urlsplit

MAX_REVERT_BYTES = 8192
MAX_REASON_CHARS = 512
MAX_CALLDATA_BYTES = 64 * 1024


def _safe_reason(reason: str, endpoint: str) -> tuple[str, bool]:
    """Contract revert strings are untrusted, including ABI-encoded URL echoes."""
    parsed = urlsplit(endpoint)
    forbidden = [parsed.netloc, parsed.username, parsed.password,
                 *[unquote(p) for p in parsed.path.split("/") if len(p) >= 8],
                 *[v for _, v in parse_qsl(parsed.query) if v]]
    sensitive = re.search(
        r"://|www\.|\b(?:[\w-]+\.)+[a-zA-Z]{2,}\b|"
        r"(?:api[ _-]?key|secret|password|authorization|credential|"
        r"private[ _-]?key|passphrase|token)\s*[:=]|\bbearer\s|"
        r"[a-zA-Z0-9_/-]{32,}", reason, re.IGNORECASE)
    if (len(reason) > MAX_REASON_CHARS or not reason.isprintable() or sensitive
            or any(value and value in reason for value in forbidden)):
        return "[redacted revert reason]", True
    return reason, False


def rpc_error_diagnostic(method: str, error, endpoint: str = "") -> dict:
    """Only inspect bounded eth_call revert bytes; never serialize error.message."""
    error = error if isinstance(error, dict) else {}
    code = error.get("code")
    code = code if type(code) is int and -(2 ** 31) <= code < 2 ** 31 else None
    result = {"kind": "rpc_error", "code": code}
    if method != "eth_call":
        return result
    message = error.get("message")
    result["message_category"] = "unspecified"
    if isinstance(message, str):
        # Categories are fixed local strings, not snippets of a provider response.
        message = message[:2048].lower()
        for fragment, category in (
                ("out of gas", "out_of_gas"),
                ("intrinsic gas too low", "intrinsic_gas_too_low"),
                ("insufficient funds", "insufficient_funds"),
                ("execution reverted", "execution_reverted"),
                ("missing trie node", "historical_state_unavailable"),
                ("header not found", "block_unavailable"),
                ("timeout", "timeout")):
            if fragment in message:
                result["message_category"] = category
                break
    data = error.get("data")
    # Some providers wrap revert data once. Do not recurse into arbitrary objects.
    if isinstance(data, dict):
        data = data.get("data")
    if not isinstance(data, str) or not data.startswith("0x"):
        return {**result, "revert_data_status": "missing_or_unsupported"}
    if len(data) > 2 + MAX_REVERT_BYTES * 2:
        return {**result, "revert_data_status": "size_limit"}
    if len(data) % 2 or not re.fullmatch(r"0x[0-9a-fA-F]*", data):
        return {**result, "revert_data_status": "invalid_hex"}
    raw = bytes.fromhex(data[2:])
    result.update(revert_data_status="captured_summary", revert_data_bytes=len(raw),
                  revert_data_sha256=hashlib.sha256(raw).hexdigest())
    if len(raw) < 4:
        return {**result, "revert_kind": "empty_or_short"}
    selector = "0x" + raw[:4].hex()
    result["revert_selector"] = selector
    if selector == "0x08c379a0":
        result["revert_kind"] = "Error(string)"
        # Parse the single ABI string explicitly, with bounded offsets and length.
        if len(raw) < 68 or int.from_bytes(raw[4:36], "big") != 32:
            return {**result, "revert_decode_status": "malformed"}
        length = int.from_bytes(raw[36:68], "big")
        if length > len(raw) - 68:
            return {**result, "revert_decode_status": "malformed"}
        try:
            reason = raw[68:68 + length].decode("utf-8")
        except UnicodeDecodeError:
            return {**result, "revert_decode_status": "malformed"}
        reason, redacted = _safe_reason(reason, endpoint)
        result.update(revert_reason=reason, revert_reason_redacted=redacted)
    elif selector == "0x4e487b71" and len(raw) == 36:
        result.update(revert_kind="Panic(uint256)",
                      panic_code_raw=str(int.from_bytes(raw[4:], "big")))
    else:
        # Unknown ABI arguments may contain provider echoes. Keep identity/size,
        # not opaque bytes or arbitrary nested error fields.
        result["revert_kind"] = "custom_or_unknown"
    return result


class AggregatorSimulationError(ValueError):
    """Still fails closed as ValueError, with separately loggable public evidence."""

    def __init__(self, message: str, diagnostic: dict):
        super().__init__(message)
        self.diagnostic = diagnostic


def simulation_failure(plan, call: dict, started_at: float, completed_at: float,
                       elapsed_ms: float, failure_kind: str, rpc_error=None,
                       result_evidence: dict | None = None) -> dict:
    # Construct explicit fields, never vars()/asdict() on a signed transaction.
    calldata = call["data"]
    truncated = len(calldata) > 2 + MAX_CALLDATA_BYTES * 2
    recorded_call = {k: call[k] for k in ("from", "to", "value", "gas")}
    recorded_call["data"] = None if truncated else calldata
    exact_request = {"method": "eth_call", "params": [call, "pending"]}
    return {
        "schema_version": 1, "failure_kind": failure_kind,
        "started_at": started_at, "completed_at": completed_at,
        "elapsed_ms": round(elapsed_ms, 3),
        "chain_id": plan.chain_id, "provider": plan.execution_provider,
        "proposal_id": plan.proposal_id, "relationship_id": plan.relationship_id,
        "follower_wallet": plan.follower_wallet,
        "input_asset": plan.input_asset, "amount_in_raw": plan.amount_in_raw,
        "minimum_amount_out_raw": plan.minimum_amount_out_raw,
        "deadline": plan.deadline,
        "plan_max_fee_per_gas_raw": plan.max_fee_per_gas,
        "plan_max_priority_fee_per_gas_raw": plan.max_priority_fee_per_gas,
        "quote_observed_at": plan.quote_observed_at,
        "quote_context_basis": "unsigned_plan",
        "quote_block_number": plan.quote_block_number,
        "quote_block_hash": plan.quote_block_hash,
        "method": "eth_call", "block_parameter": "pending", "call": recorded_call,
        "pending_state_pinned": False,
        "quote_block_is_simulation_block": False,
        "request_sha256": hashlib.sha256(json.dumps(
            exact_request, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
        "calldata_bytes": (len(calldata) - 2) // 2,
        "calldata_sha256": hashlib.sha256(bytes.fromhex(calldata[2:])).hexdigest(),
        "calldata_omitted_size_limit": truncated,
        "rpc_error": rpc_error,
        "result": result_evidence,
        "broadcast_performed_by_simulation": False,
    }
