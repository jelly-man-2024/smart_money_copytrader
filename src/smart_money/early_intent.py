"""Offline early-intent analysis. NOT imported by the monitor or live executor.

Candidates, attributable orders and executable decisions are separate objects.
No receipt, final Signal, private key, database or network client is accepted by
the parser. Unknown ABI semantics remain blockers, even when a later trade fits.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import re

from eth_abi import decode, encode
from eth_abi.exceptions import DecodingError
from eth_keys import keys
from eth_utils import keccak

from . import registry as R
from .decode import CALLS, PACKED_OPS, RELAY_CALLS, Decoder, side
from .kyber import decode_kyber_swap
from .models import Transaction, address
from .relay_race import envelope as race_envelope

RULE_VERSION = "early-intent-offline-v3"
OUTER_TYPES = ["address", "((address,uint256)[],uint256,uint256)", RELAY_CALLS,
               "address", "address", "bytes", "bytes"]
WRAPPER = "0x039ec98a76f111092d4751365ff09dd2aec301e8"
WRAPPER_TYPES = ["address", "uint256", "address", "uint256", "address", "address",
                 "(address,address,uint256,uint256,bytes)[]", "bool", "bytes"]
WRAPPER_SIGNATURE_HINT = "race(" + ",".join(WRAPPER_TYPES) + ")"
MAX_CALLDATA = 256 * 1024


def userop_digest(op, chain_id=R.CHAIN_ID, entrypoint=R.ENTRYPOINT):
    """ERC-4337 v0.8, empty initCode/paymaster only (no 7702 hash override).

    Reference: eth-infinitism/account-abstraction releases/v0.8,
    core/EntryPoint.sol and core/UserOperationLib.sol. No signing operation.
    """
    if op[2] or op[7]:
        raise ValueError("userop_hash_variant_unsupported")
    domain = keccak(encode(["bytes32", "bytes32", "bytes32", "uint256", "address"], [
        keccak(text="EIP712Domain(string name,string version,uint256 chainId,address verifyingContract)"),
        keccak(text="ERC4337"), keccak(text="1"), chain_id, entrypoint]))
    typehash = keccak(text="PackedUserOperation(address sender,uint256 nonce,bytes initCode,bytes callData,bytes32 accountGasLimits,uint256 preVerificationGas,bytes32 gasFees,bytes paymasterAndData)")
    packed = encode(["bytes32", "address", "uint256", "bytes32", "bytes32", "bytes32",
                     "uint256", "bytes32", "bytes32"],
                    [typehash, op[0], op[1], keccak(op[2]), keccak(op[3]), op[4],
                     op[5], op[6], keccak(op[7])])
    return keccak(b"\x19\x01" + domain + keccak(packed))


def userop_signature_valid(op):
    """Recover the account key, not the bundler; this does not prove execution."""
    try:
        sig = op[8]
        if len(sig) != 65 or sig[64] not in (27, 28):
            return False
        r, s = int.from_bytes(sig[:32], "big"), int.from_bytes(sig[32:64], "big")
        if s > 0x7fffffffffffffffffffffffffffffff5d576e7357a4501ddfe92f46681b20a0:
            return False
        recovered = keys.Signature(vrs=(sig[64] - 27, r, s)).recover_public_key_from_msg_hash(userop_digest(op))
        return recovered.to_checksum_address().lower() == op[0]
    except Exception:
        return False


def canonical(types, payload, trailer_size=0):
    values = decode(types, payload)
    encoded = encode(types, values)
    if len(payload) != len(encoded) + trailer_size or payload[:len(encoded)] != encoded:
        raise ValueError("noncanonical_or_trailing_calldata")
    return values, payload[len(encoded):]


def uint(raw, positive=True):
    if (not isinstance(raw, str) or not re.fullmatch(r"[0-9]{1,78}", raw)
            or int(raw) >= 2 ** 256 or (positive and int(raw) == 0)):
        raise ValueError("invalid_raw_amount")
    return int(raw)


def timestamp(value):
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(value) or value < 0):
        raise ValueError("invalid_timestamp")
    return float(value)


def hash32(value):
    if not isinstance(value, str) or not re.fullmatch(r"0x[0-9a-fA-F]{64}", value):
        raise ValueError("invalid_hash")
    return value.lower()


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


@dataclass(frozen=True)
class Candidate:
    tx_hash: str
    wallet: str
    side: str
    path: str
    route_kind: str
    token_in: str
    token_out: str
    declared_input_raw: str
    minimum_output_raw: str | None
    order_id: str
    received_at: float | None
    feed_timestamp: int | None
    fresh: bool
    observation_source: str
    blockers: tuple[str, ...] = ()
    userop_index: int | None = None
    metadata: dict = field(default_factory=dict)

    @property
    def operation_key(self):
        # Neither stage nor config version belongs here: fallback is the SAME trade.
        # A bundler may repackage the same order in another transaction.
        return fingerprint([R.CHAIN_ID, self.wallet, self.order_id])

    def relationship_key(self, relationship_id, follower):
        return fingerprint([self.operation_key, str(relationship_id), address(follower)])

    def to_dict(self):
        return {**asdict(self), "operation_key": self.operation_key,
                "rule_version": RULE_VERSION, "copy_eligible": False}


@dataclass(frozen=True)
class ParseResult:
    candidates: tuple[Candidate, ...] = ()
    reasons: tuple[str, ...] = ()


def _candidate(tx, wallet, **fields):
    return Candidate(tx_hash=hash32(tx.hash), wallet=address(wallet),
                     received_at=tx.received_at, feed_timestamp=tx.timestamp,
                     fresh=tx.fresh, observation_source=tx.observation_source, **fields)


def _permit_buy(tx, wallet):
    v, trailer = canonical(OUTER_TYPES, tx.data[4:], 32)
    user, permit, calls, refund, nft, metadata, signature = v
    if len(permit[0]) != 1 or len(calls) != 3:
        raise ValueError("unsupported_permit_or_call_count")
    if any(allow or value for _, allow, value, _ in calls) or tx.value:
        raise ValueError("optional_or_native_call")
    token, amount = permit[0][0]
    if token != R.USDG or not 0 < amount < 2 ** 255 or len(signature) != 65:
        raise ValueError("unsupported_permit_funding")
    approval, swap, cleanup = calls
    if approval[0] != token or approval[3][:4].hex() != "095ea7b3":
        raise ValueError("unexpected_approval")
    (spender, allowance), _ = canonical(["address", "uint256"], approval[3][4:])
    if spender != swap[0] or allowance < amount:
        raise ValueError("approval_swap_mismatch")
    if cleanup[0] != R.RELAY_ROUTER or cleanup[3][:4].hex() != "9bb43718":
        raise ValueError("unsupported_delivery")
    (tokens, recipients, amounts, _), _ = canonical(
        ["address[]", "address[]", "uint256[]", "bytes"], cleanup[3][4:])
    if len(tokens) != 1 or recipients != (wallet,) or amounts != (0,):
        raise ValueError("delivery_not_unique_full_balance")
    out = tokens[0]
    if side(token, out) != "BUY":
        raise ValueError("not_a_buy")
    target, _, _, body = swap
    details = {"funding_user": user, "solver_input_raw": str(amount),
               "permit_deadline": str(permit[2]), "refund_to": refund,
               "nft_recipient": nft, "metadata_hex": "0x" + metadata.hex(),
               "source_payment_is_solver_input": False}
    minimum = None
    if target == WRAPPER and body[:4].hex() == "998b5942":
        w, _ = canonical(WRAPPER_TYPES, body[4:])
        if (w[0] != token or w[1] != amount or w[2] != out
                or w[4:6] != (R.RELAY_ROUTER, R.RELAY_ROUTER) or len(w[6]) > 256):
            raise ValueError("wrapper_delivery_mismatch")
        routes = []
        for i, (a, b, x, y, payload) in enumerate(w[6]):
            route = {"index": i, "address_words": [a, b],
                     "integer_words_raw": [str(x), str(y)],
                     "selector": "0x" + payload[:4].hex(),
                     "calldata_sha256": hashlib.sha256(payload).hexdigest(),
                     "selected_or_executed": "unknown"}
            if a == R.KYBER_META_AGGREGATION_ROUTER_V2 and payload[:4].hex() == "e21fd0e9":
                try:
                    from .decode import KYBER_SWAP_EXECUTION
                    canonical([KYBER_SWAP_EXECUTION], payload[4:])
                    route["kyber_description"] = decode_kyber_swap("0x" + payload.hex())
                except (ValueError, DecodingError, OverflowError):
                    route["inner_decode_error"] = "invalid_kyber_description"
            routes.append(route)
        # Meaning is conditional on the separately verified deployment snapshot.
        # No current/historical code is smuggled into this calldata-only parser.
        details.update(race_envelope(w))
        for route in routes:
            route.update(target=route["address_words"][0], approval_target=route["address_words"][1],
                         native_value_raw=route["integer_words_raw"][0],
                         gas_limit_raw=route["integer_words_raw"][1])
        details.update(route_count=len(w[6]), routes=routes,
                       signature_hint=WRAPPER_SIGNATURE_HINT,
                       signature_hint_is_contract_verification=False)
        minimum = str(w[3])
        kind, blockers = "relay_wrapper", ()
    elif target == R.KYBER_META_AGGREGATION_ROUTER_V2 and body[:4].hex() == "e21fd0e9":
        from .decode import KYBER_SWAP_EXECUTION
        canonical([KYBER_SWAP_EXECUTION], body[4:])
        k = decode_kyber_swap("0x" + body.hex())
        if (k["src_token"] != token or k["dst_token"] != out
                or k["amount_raw"] != str(amount) or k["dst_receiver"] != R.RELAY_ROUTER):
            raise ValueError("kyber_delivery_mismatch")
        minimum = k["minimum_amount_out_raw"]
        details["kyber_flags"] = k["flags"]
        # The observed flag 512 is retained, not generalized to arbitrary flags.
        kind = "relay_direct_kyber"
        blockers = () if k["flags"] == "512" else ("kyber_flags_unsupported",)
    elif target == R.ZERO_X_ALLOWANCE_HOLDER and body[:4].hex() == "2213bc0b":
        (operator, src, quantity, destination, inner), _ = canonical(
            ["address", "address", "uint256", "address", "bytes"], body[4:])
        if src != token or quantity != amount:
            raise ValueError("zero_x_input_mismatch")
        details.update(operator=operator, target=destination, inner_selector="0x" + inner[:4].hex())
        kind, blockers = "relay_direct_0x", ("zero_x_output_and_minimum_unparsed",)
    else:
        raise ValueError("unsupported_swap_call")
    return _candidate(tx, wallet, side="BUY", path="relay/1", route_kind=kind,
                      token_in=token, token_out=out, declared_input_raw=str(amount),
                      minimum_output_raw=minimum, order_id=hash32("0x" + trailer.hex()),
                      blockers=blockers, metadata=details)


def _check_calls(target, body, wallet, order_id, depth=0):
    """Validate container encodings/allowFailure independently of the legacy decoder."""
    if depth > 12:
        raise ValueError("call_depth_exceeded")
    sel = body[:4].hex()
    children = ()
    if target == wallet:
        if sel == "34fcd5be":
            (children,), _ = canonical([CALLS], body[4:])
        elif sel == "b61d27f6":
            call, _ = canonical(["address", "uint256", "bytes"], body[4:])
            children = (call,)
        else:
            raise ValueError("account_layout_unsupported")
    elif target == R.RELAY_PROXY and sel == "f9e4bab4":
        types = ["address[]", "uint256[]", RELAY_CALLS, "address", "address", "bytes"]
        # The observed proxy appends the orderId OUTSIDE its ABI encoding.
        decoded = decode(types, body[4:])
        extra = len(body) - 4 - len(encode(types, decoded))
        if extra not in (0, 32):
            raise ValueError("unexpected_relay_trailer")
        v, trailer = canonical(types, body[4:], extra)
        if trailer and "0x" + trailer.hex() != order_id:
            raise ValueError("relay_trailer_order_mismatch")
        if len(v[0]) != len(v[1]):
            raise ValueError("relay_funding_array_mismatch")
        if any(c[1] for c in v[2]):
            raise ValueError("optional_relay_call")
        children = tuple((c[0], c[2], c[3]) for c in v[2])
    elif target == R.RELAY_ROUTER and sel == "cd6e13f7":
        v, _ = canonical([RELAY_CALLS, "address", "address", "bytes"], body[4:])
        if any(c[1] for c in v[0]):
            raise ValueError("optional_relay_call")
        children = tuple((c[0], c[2], c[3]) for c in v[0])
    elif target == R.KYBER_META_AGGREGATION_ROUTER_V2 and sel == "e21fd0e9":
        from .decode import KYBER_SWAP_EXECUTION
        canonical([KYBER_SWAP_EXECUTION], body[4:])
        k = decode_kyber_swap("0x" + body.hex())
        if k["dst_receiver"] != R.RELAY_ROUTER or k["dst_token"] != R.USDG:
            raise ValueError("kyber_sell_recipient_mismatch")
    elif target == R.RELAY_ROUTER and sel == "73b7bb2f":
        v, _ = canonical(["address[]", "address[]", "bytes[]", "uint256[]"], body[4:])
        if not len(v[0]) == len(v[1]) == len(v[2]) == len(v[3]):
            raise ValueError("cleanup_array_mismatch")
        children = tuple((dest, 0, payload) for dest, payload in zip(v[1], v[2]))
    elif sel == "095ea7b3":
        canonical(["address", "uint256"], body[4:])
    elif target == R.DEPOSITORY:
        types = {"5a1ee3ac": ["address", "address", "bytes32"],
                 "e8017952": ["address", "address", "uint256", "bytes32"]}.get(sel)
        if types is None:
            raise ValueError("deposit_method_unsupported")
        v, _ = canonical(types, body[4:])
        if v[0] != wallet or v[1] != R.USDG or "0x" + v[-1].hex() != order_id:
            raise ValueError("deposit_identity_mismatch")
    if len(children) > 256:
        raise ValueError("call_count_exceeded")
    for dest, value, child in children:
        if value:
            raise ValueError("native_call_unsupported")
        _check_calls(dest, child, wallet, order_id, depth + 1)


def parse_candidates(tx: Transaction, wallet: str) -> ParseResult:
    """Parse known structures without claiming execution or account authorization."""
    try:
        wallet = address(wallet)
        hash32(tx.hash)
        if tx.chain_id != R.CHAIN_ID or len(tx.data) > MAX_CALLDATA:
            raise ValueError("wrong_chain_or_oversized_calldata")
        if tx.to == R.RELAY_PROXY and tx.data[:4].hex() == "0a2b8f36":
            return ParseResult((_permit_buy(tx, wallet),))
        if tx.to != R.ENTRYPOINT or tx.data[:4].hex() != "765e827f":
            return ParseResult(reasons=("unsupported_path",))
        (ops, _), _ = canonical([PACKED_OPS, "address"], tx.data[4:])
        if len(ops) > 256:
            raise ValueError("userop_count_exceeded")
        # A layout hypothesis is useful for coverage, but NEVER proves delegation.
        signals = Decoder({wallet: {}}, {wallet: R.SIMPLE_ACCOUNT}).decode(tx)
        candidates, reasons = [], []
        for i, op in enumerate(ops):
            if op[0] != wallet:
                continue
            group = [s for s in signals if s.userop_index == i]
            trades = [s for s in group if s.behavior in {"BUY", "SELL", "TOKEN_SWAP"}]
            if len(trades) != 1 or any(s.behavior == "UNKNOWN" for s in group):
                reasons.append("ambiguous_or_nontrade_userop")
                continue
            s = trades[0]
            if s.behavior != "SELL" or not s.evidence.get("relay_deposit_order_id"):
                reasons.append("unsupported_userop_trade")
                continue
            blockers = []
            if (sum(s.behavior == "INTENT_DEPOSIT" for s in group) != 1
                    or any(s.behavior not in {"SELL", "INTENT_DEPOSIT", "APPROVAL", "CLAIM"}
                           for s in group)):
                blockers.append("unaccounted_userop_actions")
            try:
                _check_calls(wallet, op[3], wallet, s.evidence["relay_deposit_order_id"])
            except ValueError as exc:
                blockers.append(str(exc))
            if op[2] or op[7] or len(op[8]) != 65:
                blockers.append("userop_authorization_layout_unsupported")
            if s.protocol == "0x":
                blockers.append("zero_x_output_and_minimum_unparsed")
            if s.protocol == "kyber" and s.evidence.get("kyber_flags") != "512":
                blockers.append("kyber_flags_unsupported")
            uint(s.amount_in_raw)
            candidates.append(_candidate(
                tx, wallet, side="SELL", path=s.path, route_kind="relay_sell_" + s.protocol,
                token_in=s.token_in, token_out=s.token_out, declared_input_raw=s.amount_in_raw,
                minimum_output_raw=s.amount_limit_raw,
                order_id=hash32(s.evidence["relay_deposit_order_id"]), userop_index=i,
                blockers=tuple(blockers), metadata={
                    "account_layout_hypothesis": R.SIMPLE_ACCOUNT,
                    "authorization_verified": False,
                    "signature_valid": userop_signature_valid(op),
                    "userop_fingerprint": hashlib.sha256(encode([PACKED_OPS], [[op]])).hexdigest(),
                }))
        return ParseResult(tuple(candidates), tuple(reasons))
    except (ValueError, TypeError, KeyError, IndexError, OverflowError, DecodingError):
        # Input may contain arbitrary bytes; never echo its text in diagnostics.
        return ParseResult(reasons=("malformed_or_unsupported_structure",))


@dataclass(frozen=True)
class Observation:
    """Time is when this process received evidence, not the source event's time."""
    observed_at: float
    provenance: str
    payload: dict

    def available(self, at):
        return bool(self.provenance and timestamp(self.observed_at) <= timestamp(at))


def associate_order(candidate: Candidate, observation: Observation, at: float) -> dict:
    """Pre-delivery BUY association; never reads outTx credit or settlement fills.

    The source payment may belong to another person. This mirrors the accepted
    order-association policy, not a claim of common cross-chain wallet control.
    """
    if not observation.available(at):
        raise ValueError("order_not_available_at_decision")
    if candidate.side != "BUY":
        raise ValueError("not_a_relay_buy")
    requests = observation.payload.get("requests")
    if not isinstance(requests, list) or len(requests) != 1:
        raise ValueError("order_not_unique")
    request = requests[0]
    protocol = request.get("protocol", {})
    if (hash32(protocol.get("orderId")) != candidate.order_id
            or hash32(request.get("id")) == candidate.order_id
            or address(request.get("recipient")) != candidate.wallet):
        raise ValueError("order_identity_mismatch")
    # Pending/unknown states are not guessed. This allowlist can expand only with
    # independently preserved provider examples; overall success is not required.
    if request.get("status") not in {"success", "pending"}:
        raise ValueError("order_status_unsupported")
    origin = protocol.get("deposit", {}).get("origin", {})
    payer, currency = origin.get("depositor"), origin.get("currency")
    if not isinstance(payer, str) or not payer or payer != request.get("user"):
        raise ValueError("source_payer_mismatch")
    amount = uint(origin.get("amount"))
    chain = origin.get("chainId")
    if type(chain) is not int:
        raise ValueError("source_chain_missing")
    source_tx = origin.get("transactionId")
    if not isinstance(source_tx, str) or not source_tx:
        raise ValueError("source_transaction_missing")
    ins = request.get("data", {}).get("inTxs", [])
    if sum(i.get("hash") == source_tx and i.get("chainId") == chain
           and i.get("status") == "success" for i in ins) != 1:
        raise ValueError("source_payment_not_uniquely_successful")
    if (chain, currency) not in R.RELAY_USDG_EQUIVALENTS:
        raise ValueError("source_funding_mapping_unsupported")
    order_data = request.get("orderData") or protocol.get("orderData") or {}
    output = order_data.get("output", {})
    # Before there is a destination receipt, chain identity must come from the
    # order itself; the same recipient/token bytes can exist on another chain.
    destination_chain_raw = output.get("chainId")
    if not ((type(destination_chain_raw) is int and destination_chain_raw == R.CHAIN_ID)
            or (type(destination_chain_raw) is str
                and destination_chain_raw in {str(R.CHAIN_ID), "robinhood"})):
        raise ValueError("order_destination_chain_missing_or_mismatched")
    payments = output.get("payments", [])
    if (len(payments) != 1 or address(payments[0].get("recipient")) != candidate.wallet
            or address(payments[0].get("currency")) != candidate.token_out):
        raise ValueError("order_payment_mismatch")
    minimum = uint(payments[0].get("minimumAmount"))
    hint = candidate.metadata.get("request_hint")
    if hint is not None and hint != request["id"]:
        raise ValueError("request_hint_mismatch")
    return {"basis": "relay_order_source_payment_and_calldata",
            "cross_chain_common_control_proven": False,
            "source_receipt_independently_verified": False,
            "source_amount_raw": str(amount), "source_currency": currency,
            "source_chain_id": str(chain), "source_payer": payer,
            "destination_chain_id": str(R.CHAIN_ID),
            "destination_chain_id_raw": destination_chain_raw,
            "source_tx_hash": source_tx, "order_minimum_raw": str(minimum),
            "request_id": request["id"], "order_id": candidate.order_id,
            "observed_at": observation.observed_at, "provenance": observation.provenance,
            "document_sha256": fingerprint(observation.payload)}
