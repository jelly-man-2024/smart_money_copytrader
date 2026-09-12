"""Strict association of Relay API evidence with an evidenced source deposit."""
from __future__ import annotations

from .models import Signal
from . import registry as R


def relay_delivery_evidence(document: dict, deposit: Signal) -> list[dict]:
    """Validate order/source identity; API success is not destination-chain finality."""
    requests = document.get("requests")
    if not isinstance(requests, list) or len(requests) != 1:
        raise ValueError("relay order response is not unique")
    request = requests[0]
    order_id = deposit.evidence.get("order_id")
    if (deposit.behavior != "INTENT_DEPOSIT"
            or deposit.evidence.get("solver_order_status") != "source_deposit_evidenced"):
        raise ValueError("source deposit is not evidenced")
    protocol = request.get("protocol", {})
    origin = protocol.get("deposit", {}).get("origin", {})
    expected_origin = {
        "amount": deposit.amount_in_raw, "chainId": R.CHAIN_ID,
        "currency": deposit.token_in, "depositor": deposit.wallet,
        "depository": R.DEPOSITORY, "transactionId": deposit.tx_hash,
    }
    if protocol.get("orderId", "").lower() != order_id or any(
            str(origin.get(key, "")).lower() != str(value).lower()
            for key, value in expected_origin.items()):
        raise ValueError("relay origin does not match source deposit")
    request_id = request.get("id", "").lower()
    if len(request_id) != 66 or request_id == order_id:
        raise ValueError("relay request id is missing or conflated with order id")
    if request.get("status") != "success" or request.get("user", "").lower() != deposit.wallet:
        raise ValueError("relay request user or status mismatch")
    in_txs = request.get("data", {}).get("inTxs", [])
    if sum(item.get("hash", "").lower() == deposit.tx_hash
           and item.get("chainId") == R.CHAIN_ID and item.get("status") == "success"
           for item in in_txs) != 1:
        raise ValueError("relay input transaction not uniquely matched")
    out_txs = request.get("data", {}).get("outTxs", [])
    fills = protocol.get("settlement", {}).get("destination", {}).get("fills", [])
    fill_keys = {(item.get("chainId"), item.get("transactionId")) for item in fills}
    result = []
    for out_tx in out_txs:
        key = (out_tx.get("chainId"), out_tx.get("hash"))
        if out_tx.get("status") != "success" or key not in fill_keys:
            continue
        credits = []
        for change in out_tx.get("stateChanges", []):
            detail = change.get("change", {})
            token = detail.get("data", {})
            try:
                amount = int(detail.get("balanceDiff", "0"))
            except (TypeError, ValueError):
                continue
            if (change.get("address") == request.get("recipient") and detail.get("kind") == "token"
                    and token.get("tokenKind") == "ft" and amount > 0):
                credits.append({"token": token.get("tokenAddress"), "amount_raw": str(amount)})
        if len(credits) != 1:
            continue
        result.append({
            "order_id": order_id, "request_id": request_id, "wallet": deposit.wallet,
            "source_tx_hash": deposit.tx_hash, "destination_chain_id": str(key[0]),
            "destination_tx_hash": key[1], "destination_recipient": request.get("recipient"),
            "destination_token": credits[0]["token"],
            "destination_amount_raw": credits[0]["amount_raw"],
            "relay_status": "success", "proof_source": "relay_public_requests_v2",
            "destination_chain_status": "api_reported_not_independently_rechecked",
        })
    if len(result) != 1:
        raise ValueError("relay destination delivery not uniquely evidenced")
    return result
