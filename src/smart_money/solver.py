"""Strict association of Relay API evidence with an evidenced source deposit."""
from __future__ import annotations

from copy import deepcopy

from .models import Signal
from . import registry as R


def _opaque(value, name: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ValueError(f"relay {name} is invalid")
    return value.lower() if value.startswith("0x") else value


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
    if deposit.chain_id != R.CHAIN_ID:
        # Our own Relay deposits exist only on Robinhood Chain: no other chain
        # has a verified depository here. Refuse rather than compare against
        # another chain's address and fail for an unrelated reason.
        raise ValueError("relay deposits are only evidenced on Robinhood Chain")
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


def _funding_normalization(chain, local_input: str, source_currency: str) -> str:
    """Name the assumption behind treating source funding as the local asset.

    Robinhood converts Solana USDC into USDG, two different assets, which the
    operator approved. Arc settles in USDC itself at the same six decimals as
    the source, so there the mapping only renames the asset and keeps the scale.
    """
    if local_input == source_currency:
        return "identity"
    if chain.chain_id == R.CHAIN_ID:
        return "solana_usdc_6_to_robinhood_usdg_6_operator_approved"
    return f"relay_source_currency_to_chain_{chain.chain_id}_settlement_asset"


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
    request_user = _opaque(request.get("user"), "request user", 128)
    if (request.get("status") != "success"
            or str(request.get("recipient", "")).lower() != candidate.wallet):
        raise ValueError("relay request is not owned by the candidate wallet")
    protocol = request.get("protocol", {})
    origin = protocol.get("deposit", {}).get("origin", {})
    try:
        amount_in = int(origin.get("amount", "0"))
        origin_chain = int(origin.get("chainId"))
    except (TypeError, ValueError):
        raise ValueError("relay origin amount or chain is invalid") from None
    source_currency = _opaque(origin.get("currency"), "origin currency", 128)
    source_tx = _opaque(origin.get("transactionId"), "origin transaction")
    source_payer = _opaque(origin.get("depositor"), "origin depositor", 128)
    if amount_in <= 0 or source_payer != request_user:
        raise ValueError("relay origin is not attributable to the request user")
    in_txs = request.get("data", {}).get("inTxs", [])
    if sum(_opaque(item.get("hash"), "input transaction") == source_tx
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
    order_data = request.get("orderData") or protocol.get("orderData") or {}
    order_output = order_data.get("output", {})
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

    chain = R.chain_for(candidate.chain_id)
    settlement = chain.settlement_asset
    local_input = (settlement if settlement is not None
                   and (origin_chain, source_currency) in chain.relay_usdg_equivalents
                   else source_currency)
    result = deepcopy(candidate)
    result.behavior = "BUY"
    result.stage = "relay_buy_evidenced"
    result.token_in = local_input
    result.token_out = token_out
    result.amount_in_raw = str(amount_in)
    result.amount_out_raw = str(amount_out)
    result.protocol = "relay_solver"
    result.reasons = ["relay_order_and_local_delivery_exactly_associated",
                      "relay_origin_chain_receipt_not_independently_rechecked"]
    result.evidence.update({
        "source_orchestrator": "relay", "relay_order_id": order_id,
        "relay_request_id": request_id, "source_chain_id": str(origin_chain),
        "source_currency": source_currency, "source_tx_hash": source_tx,
        "source_payer": request_user, "local_funding_asset": local_input,
        "funding_normalization": _funding_normalization(chain, local_input, source_currency),
        "destination_tx_hash": candidate.tx_hash,
        "actual_input_debit_raw": str(amount_in),
        "actual_output_credit_raw": str(amount_out),
        "order_attribution": "relay_api_and_local_receipt_exact_match",
    })
    result.copy_eligible = False
    return result


def relay_confirmed_sell(document: dict, candidate: Signal) -> Signal:
    """Close one Relay-orchestrated SELL from the Relay order when no swap event
    was recognised locally.

    The local receipt already proves the wallet's token debit and the USDG deposit
    into the Relay depository under one order id; what it could not prove is that
    the debit was a swap rather than an arbitrary transfer (the venue emitted an
    unknown event). Relay's own request for that order states which token and
    amount the user sold and how much USDG the deposit carried. Only a request
    whose user, order id, origin deposit, sold currency and input transaction all
    equal the local evidence is accepted; several users can share one bundled
    transaction, so the response is filtered rather than assumed unique.
    """
    if candidate.chain_id != R.CHAIN_ID:
        raise ValueError("relay sells are only evidenced on Robinhood Chain")
    if (candidate.behavior != "SELL" or candidate.stage != "needs_review"
            or candidate.protocol not in {"0x", "kyber"}
            or candidate.evidence.get("source_orchestrator") != "relay"
            or candidate.execution_status != "success"
            or candidate.execution_success is not True
            or "relay_sell_evidence_not_uniquely_closed" not in candidate.reasons):
        raise ValueError("signal is not an unclosed successful relay sell")
    order_id = str(candidate.evidence.get("relay_deposit_order_id", "")).lower()
    if len(order_id) != 66 or not order_id.startswith("0x"):
        raise ValueError("relay sell has no deposit order id")
    if not candidate.token_in or candidate.token_out != R.USDG:
        raise ValueError("relay sell must debit one token into the USDG deposit")
    deltas = candidate.evidence.get("wallet_erc20_deltas_raw", {})
    try:
        debit = -int(deltas.get(candidate.token_in, "0"))
        declared = int(candidate.amount_in_raw or "0")
    except (TypeError, ValueError):
        raise ValueError("relay sell wallet debit is invalid") from None
    if debit <= 0 or debit != declared:
        raise ValueError("relay sell wallet debit does not match the declared amount")
    for token, raw in deltas.items():
        try:
            amount = int(raw)
        except (TypeError, ValueError):
            continue
        if amount != 0 and token.lower() != candidate.token_in:
            raise ValueError("relay sell wallet moved more than the sold token")

    requests = document.get("requests")
    if not isinstance(requests, list) or not requests:
        raise ValueError("relay order response is empty")
    matches = []
    for request in requests:
        if not isinstance(request, dict) or request.get("status") != "success":
            continue
        protocol = request.get("protocol", {})
        if str(protocol.get("orderId", "")).lower() != order_id:
            continue
        matches.append(request)
    if len(matches) != 1:
        raise ValueError("relay order id is not uniquely present in the response")
    request = matches[0]
    checked_order_id, request_id = _request_identity(request)
    if checked_order_id != order_id:
        raise ValueError("relay order identity mismatch")
    metadata = request.get("data", {}).get("metadata", {})
    if (str(request.get("user", "")).lower() != candidate.wallet
            or str(metadata.get("sender", "")).lower() != candidate.wallet):
        raise ValueError("relay request is not owned by the candidate wallet")
    protocol = request.get("protocol", {})
    origin = protocol.get("deposit", {}).get("origin", {})
    try:
        deposit_amount = int(origin.get("amount", "0"))
    except (TypeError, ValueError):
        raise ValueError("relay origin amount is invalid") from None
    expected_origin = {
        "chainId": candidate.chain_id, "currency": candidate.token_out,
        "depositor": candidate.wallet, "depository": R.DEPOSITORY,
        "transactionId": candidate.tx_hash,
    }
    if deposit_amount <= 0 or any(
            str(origin.get(key, "")).lower() != str(value).lower()
            for key, value in expected_origin.items()):
        raise ValueError("relay origin deposit does not match the local receipt")
    if str(candidate.evidence.get("relay_deposit_amount_raw")) != str(deposit_amount):
        raise ValueError("relay origin amount does not match the local deposit event")
    currency_in = metadata.get("currencyIn", {})
    sold = currency_in.get("currency", {})
    try:
        sold_amount = int(currency_in.get("amount", "0"))
    except (TypeError, ValueError):
        raise ValueError("relay sold amount is invalid") from None
    if (sold.get("chainId") != candidate.chain_id
            or str(sold.get("address", "")).lower() != candidate.token_in
            or sold_amount != debit):
        raise ValueError("relay sold currency does not match the wallet debit")
    in_txs = request.get("data", {}).get("inTxs", [])
    if (len(in_txs) != 1
            or str(in_txs[0].get("hash", "")).lower() != candidate.tx_hash
            or in_txs[0].get("chainId") != candidate.chain_id
            or in_txs[0].get("status") != "success"):
        raise ValueError("relay input transaction is not uniquely this sell")
    currency_out = metadata.get("currencyOut", {})
    destination = currency_out.get("currency", {})
    destination_chain = destination.get("chainId")
    destination_currency = _opaque(destination.get("address"), "destination currency", 128)
    try:
        destination_amount = int(currency_out.get("amount", "0"))
    except (TypeError, ValueError):
        raise ValueError("relay destination amount is invalid") from None
    if (not isinstance(destination_chain, int) or destination_amount <= 0
            or destination_amount > deposit_amount):
        raise ValueError("relay destination settlement is not a plausible payout")

    result = deepcopy(candidate)
    result.stage = "relay_sell_evidenced"
    result.amount_out_raw = str(deposit_amount)
    result.reasons = [
        reason for reason in result.reasons
        if reason != "relay_sell_evidence_not_uniquely_closed"
    ] + [
        "relay_order_confirms_sell_without_recognized_swap_event",
        "relay_destination_chain_not_independently_rechecked",
        "relay_source_deposit_is_not_destination_finality_or_trade_approval",
    ]
    result.evidence.update({
        "relay_order_id": order_id, "relay_request_id": request_id,
        "actual_input_debit_raw": str(debit),
        "actual_output_deposit_raw": str(deposit_amount),
        "actual_output_credit_raw": str(deposit_amount),
        "relay_destination_chain_id": str(destination_chain),
        "relay_destination_currency": destination_currency,
        "relay_destination_amount_raw": str(destination_amount),
        "relay_destination_recipient": _opaque(request.get("recipient"), "recipient", 128),
        "order_attribution": "relay_api_order_and_local_deposit_exact_match",
    })
    result.copy_eligible = False
    return result
