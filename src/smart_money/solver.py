"""Strict association of Relay API evidence with an evidenced source deposit."""
from __future__ import annotations

from copy import deepcopy

from .models import Signal
from . import registry as R


def _request(document: dict) -> dict:
    requests = document.get("requests")
    if not isinstance(requests, list) or len(requests) != 1:
        raise ValueError("relay order response is not unique")
    return requests[0]


def _request_identity(request: dict) -> tuple[str, str]:
    protocol = request.get("protocol", {})
    order_id = str(protocol.get("orderId", "")).lower()
    request_id = str(request.get("id", "")).lower()
    if (len(order_id) != 66 or not order_id.startswith("0x")
            or len(request_id) != 66 or not request_id.startswith("0x")
            or request_id == order_id):
        raise ValueError("relay request or order identity is invalid or conflated")
    return order_id, request_id


def relay_delivery_evidence(document: dict, deposit: Signal) -> list[dict]:
    """Validate order/source identity; API success is not destination-chain finality."""
    request = _request(document)
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
    checked_order_id, request_id = _request_identity(request)
    if checked_order_id != order_id:
        raise ValueError("relay order identity mismatch")
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


def relay_passive_buy(document: dict, candidate: Signal) -> Signal:
    """Promote one passive credit only after an exact Relay order association.

    Relay's API establishes order attribution, while the already-enriched candidate
    establishes the successful Robinhood-chain credit. Neither alone is sufficient.
    """
    new_candidate = (candidate.behavior in {
                         "EXTERNAL_DELIVERY_CANDIDATE", "INCOMING_TRANSFER"}
                     and candidate.stage == "needs_review")
    existing_association = (candidate.behavior == "BUY"
                            and candidate.stage == "relay_buy_evidenced"
                            and candidate.protocol == "relay_solver")
    if (not (new_candidate or existing_association)
            or candidate.execution_status != "success"
            or candidate.execution_success is not True):
        raise ValueError("signal is not a successful passive delivery candidate")
    positive = []
    for token, raw in candidate.evidence.get("wallet_erc20_deltas_raw", {}).items():
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            positive.append((token.lower(), amount))
    if len(positive) != 1:
        raise ValueError("passive delivery does not contain one unique token credit")
    token_out, amount_out = positive[0]

    request = _request(document)
    order_id, request_id = _request_identity(request)
    request_user = str(request.get("user", "")).lower()
    if (request.get("status") != "success" or len(request_user) != 42
            or str(request.get("recipient", "")).lower() != candidate.wallet):
        raise ValueError("relay request is not owned by the candidate wallet")
    protocol = request.get("protocol", {})
    origin = protocol.get("deposit", {}).get("origin", {})
    try:
        amount_in = int(origin.get("amount", "0"))
        origin_chain = int(origin.get("chainId"))
    except (TypeError, ValueError):
        raise ValueError("relay origin amount or chain is invalid") from None
    token_in = str(origin.get("currency", "")).lower()
    source_tx = str(origin.get("transactionId", "")).lower()
    if (amount_in <= 0 or len(token_in) != 42 or len(source_tx) != 66
            or str(origin.get("depositor", "")).lower() != request_user):
        raise ValueError("relay origin is not attributable to the request user")
    in_txs = request.get("data", {}).get("inTxs", [])
    if sum(str(item.get("hash", "")).lower() == source_tx
           and item.get("chainId") == origin_chain and item.get("status") == "success"
           for item in in_txs) != 1:
        raise ValueError("relay origin transaction is not uniquely successful")

    out_txs = [item for item in request.get("data", {}).get("outTxs", [])
               if str(item.get("hash", "")).lower() == candidate.tx_hash
               and item.get("chainId") == candidate.chain_id
               and item.get("status") == "success"]
    fills = protocol.get("settlement", {}).get("destination", {}).get("fills", [])
    if len(out_txs) != 1 or sum(
            item.get("chainId") == candidate.chain_id
            and str(item.get("transactionId", "")).lower() == candidate.tx_hash
            for item in fills) != 1:
        raise ValueError("relay destination transaction is not uniquely settled")
    credits = []
    for change in out_txs[0].get("stateChanges", []):
        detail = change.get("change", {})
        token = detail.get("data", {})
        try:
            amount = int(detail.get("balanceDiff", "0"))
        except (TypeError, ValueError):
            continue
        if (str(change.get("address", "")).lower() == candidate.wallet
                and detail.get("kind") == "token" and token.get("tokenKind") == "ft"
                and amount > 0):
            credits.append((str(token.get("tokenAddress", "")).lower(), amount))
    if credits != [(token_out, amount_out)]:
        raise ValueError("relay delivery credit does not match the local receipt")
    order_output = protocol.get("orderData", {}).get("output", {})
    payments = order_output.get("payments", [])
    matching_payments = []
    for payment in payments:
        try:
            minimum = int(payment.get("minimumAmount", "0"))
        except (TypeError, ValueError):
            continue
        if (str(payment.get("recipient", "")).lower() == candidate.wallet
                and str(payment.get("currency", "")).lower() == token_out
                and 0 < minimum <= amount_out):
            matching_payments.append(payment)
    if len(matching_payments) != 1:
        raise ValueError("relay order output does not uniquely authorize the wallet credit")

    result = deepcopy(candidate)
    result.behavior = "BUY"
    result.stage = "relay_buy_evidenced"
    result.token_in = token_in
    result.token_out = token_out
    result.amount_in_raw = str(amount_in)
    result.amount_out_raw = str(amount_out)
    result.protocol = "relay_solver"
    result.reasons = ["relay_order_and_local_delivery_exactly_associated",
                      "relay_origin_chain_receipt_not_independently_rechecked"]
    result.evidence.update({
        "source_orchestrator": "relay", "relay_order_id": order_id,
        "relay_request_id": request_id, "source_chain_id": str(origin_chain),
        "source_tx_hash": source_tx, "source_payer": request_user,
        "destination_tx_hash": candidate.tx_hash,
        "order_attribution": "relay_api_and_local_receipt_exact_match",
    })
    result.copy_eligible = False
    return result
