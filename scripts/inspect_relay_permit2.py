"""Offline calldata inspection only; never connects to RPC or emits trading signals.

Run: .venv/bin/python scripts/inspect_relay_permit2.py <public-fixture.json>
The 0x998b5942 wrapper layout is structural, NOT a verified contract ABI.
"""
from __future__ import annotations

import json
from pathlib import Path
import sys

from eth_abi import decode, encode
from eth_utils import keccak

from smart_money import registry as R
from smart_money.decode import KYBER_SWAP_EXECUTION

OUTER_TYPES = [
    "address", "((address,uint256)[],uint256,uint256)",
    "(address,bool,uint256,bytes)[]", "address", "address", "bytes", "bytes",
]
SIGNATURE = "permit2TransferAndMulticall(" + ",".join(OUTER_TYPES) + ")"
WRAPPER = "0x039ec98a76f111092d4751365ff09dd2aec301e8"
WRAPPER_TYPES = [
    "address", "uint256", "address", "uint256", "address", "address",
    "(address,address,uint256,uint256,bytes)[]", "bool", "bytes",
]


def canonical(types, payload, allow_trailer=False):
    values = decode(types, payload)
    encoded = encode(types, values)
    if payload[:len(encoded)] != encoded:
        raise ValueError("non-canonical ABI payload")
    trailer = payload[len(encoded):]
    if trailer and not allow_trailer:
        raise ValueError("unexpected trailing bytes")
    return values, trailer


def inspect(transaction):
    if transaction["chain_id"] != R.CHAIN_ID or transaction["to"].lower() != R.RELAY_PROXY:
        raise ValueError("wrong chain or contract")
    raw = transaction["input"]
    if not raw.startswith("0x") or len(raw) > 2 + 256 * 1024 * 2:
        raise ValueError("invalid calldata size/encoding")
    data = bytes.fromhex(raw[2:])
    if data[:4] != keccak(text=SIGNATURE)[:4]:
        raise ValueError("wrong selector")
    v, trailer = canonical(OUTER_TYPES, data[4:], allow_trailer=True)
    user, permit, calls, refund, nft, metadata, signature = v
    if len(calls) > 256 or len(permit[0]) > 256:
        raise ValueError("batch limit")
    out = {
        "tx_hash": transaction["hash"], "function": SIGNATURE,
        "calldata_bytes": len(data), "outer_roundtrip": True,
        "funding_user": user,
        "permit": {"permitted": [{"token": t, "amount_raw": str(a)} for t, a in permit[0]],
                   "nonce": str(permit[1]), "deadline": str(permit[2])},
        "refund_to": refund, "nft_recipient": nft,
        "metadata_hex": "0x" + metadata.hex(), "permit_signature_bytes": len(signature),
        "trailing_bytes_hex": "0x" + trailer.hex(),
        "trailer_note": "Not an ABI argument; sample A matches previously observed Relay orderId.",
        "classification": "inspection_only_not_a_confirmed_trade", "calls": [],
    }
    for i, (target, allow, value, body) in enumerate(calls):
        item = {"index": i, "target": target, "allow_failure": allow,
                "native_value_raw": str(value), "selector": "0x" + body[:4].hex(),
                "calldata_bytes": len(body)}
        if body[:4].hex() == "095ea7b3":
            (spender, amount), _ = canonical(["address", "uint256"], body[4:])
            item.update(function="approve", spender=spender, amount_raw=str(amount),
                        note="ERC20-shaped calldata, not proof of execution or token identity")
        elif target == R.RELAY_ROUTER and body[:4].hex() == "9bb43718":
            c, _ = canonical(["address[]", "address[]", "uint256[]", "bytes"], body[4:])
            if not len(c[0]) == len(c[1]) == len(c[2]) or len(c[0]) > 256:
                raise ValueError("invalid cleanup arrays")
            item.update(function="cleanupErc20s", deliveries=[
                {"token": t, "recipient": r, "amount_parameter_raw": str(a),
                 "amount_mode": "full_router_balance" if a == 0 else "fixed"}
                for t, r, a in zip(*c[:3])], metadata_hex="0x" + c[3].hex())
        elif target == WRAPPER and body[:4].hex() == "998b5942":
            w, _ = canonical(WRAPPER_TYPES, body[4:])
            if len(w[6]) > 256:
                raise ValueError("route limit")
            item.update(
                abi_status="inferred_layout_roundtrip_only_not_verified_semantics",
                header_words=[str(x) for x in w[:6]], boolean_parameter=w[7],
                extra_bytes_hex="0x" + w[8].hex(),
                extra_utf8=w[8].decode("utf-8", errors="replace"), routes=[])
            for j, route in enumerate(w[6]):
                a, b, c, d, payload = route
                entry = {"index": j, "header_fields": [a, b, str(c), str(d)],
                         "selector": "0x" + payload[:4].hex(), "calldata_bytes": len(payload),
                         "execution_status": "unknown_from_calldata"}
                if a == R.KYBER_META_AGGREGATION_ROUTER_V2 and payload[:4].hex() == "e21fd0e9":
                    (k,), _ = canonical([KYBER_SWAP_EXECUTION], payload[4:])
                    desc = k[3]
                    entry["kyber_description"] = {
                        "token_in": desc[0], "token_out": desc[1],
                        "amount_in_raw": str(desc[7]), "min_return_raw": str(desc[8]),
                        "recipient": desc[6], "flags": str(desc[9]),
                        "client_data_utf8_untrusted": k[4].decode("utf-8", errors="replace"),
                    }
                item["routes"].append(entry)
        else:
            item["abi_status"] = "unknown"
        out["calls"].append(item)
    return out


if __name__ == "__main__":
    fixture = json.loads(Path(sys.argv[1]).read_text())
    print(json.dumps(inspect(fixture["transaction"]), ensure_ascii=False, indent=2))
