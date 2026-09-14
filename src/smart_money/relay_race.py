"""Narrow race-envelope contract for one pinned, experimentally checked runtime.

This is bytecode-backed interpretation, NOT published-source verification or an
audit of its arbitrary inner routers. It expresses conditional output intent,
not successful execution, owner identity, finality or trading authorization.
Evidence: data/relay_race_runtime_2026-09-14.json; tests/test_relay_race_evm.py.
"""
from eth_utils import keccak

from .models import address
from .registry import CHAIN_ID, NATIVE

RACE_ADDRESS = "0x039ec98a76f111092d4751365ff09dd2aec301e8"
RACE_CODE_HASH = "0xf5ba65338ab45430556c6b876e875413a9a9f94866ef40553b65f4025e947dcd"
RACE_CODE_BYTES = 4721
RACE_RULE = "relay-race-pinned-runtime-v1"


def envelope(values):
    """Call only after exact canonical ABI decoding and outer delivery binding."""
    token_in, amount, token_out, minimum, recipient, refund, routes, emit_logs, extra = values
    if token_in == token_out or NATIVE in (token_in, token_out, recipient, refund):
        raise ValueError("race_invalid_assets_or_recipient")
    if recipient == RACE_ADDRESS or amount <= 0 or minimum <= 0:
        raise ValueError("race_invalid_amount_or_recipient")
    if not 1 <= len(routes) <= 256:
        raise ValueError("race_route_count_unsupported")
    # Native funding, arbitrary empty calls and self-recursion are deliberately
    # outside this adapter, even if the contract could execute some such calls.
    for target, approval_target, native_value, gas_limit, data in routes:
        if (target in (NATIVE, RACE_ADDRESS) or approval_target in (NATIVE, RACE_ADDRESS)
                or native_value != 0 or gas_limit <= 0 or len(data) < 4):
            raise ValueError("race_route_shape_unsupported")
    try:
        hint = extra.decode("ascii")
        if len(hint) != 66 or not hint.startswith("0x") or len(bytes.fromhex(hint[2:])) != 32:
            raise ValueError()
    except (UnicodeError, ValueError):
        raise ValueError("race_request_hint_unsupported") from None
    return {"race_rule": RACE_RULE, "required_runtime_code_hash": RACE_CODE_HASH,
            "source_verified": False, "runtime_semantics_basis": "pinned_bytecode_and_local_evm_tests",
            "minimum_output_is_conditional_on_runtime_match": True,
            "minimum_output_raw": str(minimum), "recipient": recipient, "refund_to": refund,
            "emit_route_events": emit_logs, "request_hint": hint.lower(),
            "route_selection": "trial_best_output_then_commit_with_fallback",
            "inner_routes_semantically_decoded": False}


def verify_deployment(payload):
    if (payload.get("chain_id") != CHAIN_ID or address(payload.get("contract")) != RACE_ADDRESS
            or payload.get("rule") != RACE_RULE):
        raise ValueError("race_deployment_identity_mismatch")
    code = payload.get("code")
    if not isinstance(code, str) or not code.startswith("0x") or len(code) != 2 + RACE_CODE_BYTES * 2:
        raise ValueError("race_runtime_code_mismatch")
    try:
        digest = "0x" + keccak(bytes.fromhex(code[2:])).hex()
    except ValueError:
        raise ValueError("race_runtime_code_mismatch") from None
    if digest != RACE_CODE_HASH:
        raise ValueError("race_runtime_code_mismatch")
    return {"contract": RACE_ADDRESS, "runtime_code_hash": digest, "rule": RACE_RULE,
            "state_scope": "observed_block_not_transaction_prestate", "source_verified": False}
