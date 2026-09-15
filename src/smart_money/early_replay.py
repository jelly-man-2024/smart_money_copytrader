"""Pure, point-in-time replay. It cannot reserve funds, sign or send a transaction."""
from __future__ import annotations

from collections import Counter
from dataclasses import fields

from . import registry as R
from .early_intent import (RULE_VERSION, Candidate, Observation, associate_order,
                           fingerprint, hash32, parse_candidates, timestamp, uint)
from .kyber import decode_kyber_swap
from .models import Signal, Transaction, address
from .paper import AmountRule, RATIO_SCALE
from .quotes import Quote, QuotePolicy, assess_quote
from .relay_race import verify_deployment


def transaction_from_record(record):
    """Accept stored public Transaction fields, not signed bytes or a final Signal."""
    data = record.get("data", "")
    if not isinstance(data, str) or not data.startswith("0x") or len(data) > 524290:
        raise ValueError("invalid_transaction_data")
    values = {f.name: record[f.name] for f in fields(Transaction) if f.name in record}
    values.update(data=bytes.fromhex(data[2:]), value=uint(str(record.get("value", "0")), False),
                  sender=address(record["sender"]), hash=hash32(record["hash"]))
    if values.get("to") is not None:
        values["to"] = address(values["to"])
    if type(values.get("fresh", False)) is not bool:
        raise ValueError("invalid_fresh_flag")
    return Transaction(**values)


def observation(record):
    if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
        raise ValueError("invalid_observation")
    provenance = record.get("provenance")
    if not isinstance(provenance, str) or not provenance:
        raise ValueError("observation_provenance_missing")
    return Observation(timestamp(record.get("observed_at")), provenance, record["payload"])


class MissingEvidence(ValueError):
    pass


def _snapshot(snapshots, name, at, max_age=None):
    raw = snapshots.get(name)
    if raw is None:
        raise MissingEvidence(name + "_snapshot_missing")
    obs = observation(raw)
    if not obs.available(at):
        raise MissingEvidence(name + "_not_available_at_decision")
    if max_age is not None and at - obs.observed_at > max_age:
        raise ValueError(name + "_snapshot_expired")
    if "capture_started_at" in raw:
        started = timestamp(raw["capture_started_at"])
        if started > obs.observed_at or (max_age is not None and at - started > max_age):
            raise ValueError(name + "_snapshot_capture_expired")
    return obs


def _bound(payload, policy):
    for name in ("relationship_id", "follower", "smart_wallet", "config_snapshot_hash"):
        if payload.get(name) != policy.get(name):
            raise ValueError("snapshot_relationship_or_config_mismatch")


def _planned_amount(c, policy, portfolio, attribution):
    rule = AmountRule(**policy["buy_rule" if c.side == "BUY" else "sell_rule"])
    if c.side == "BUY":
        amount = (uint(rule.fixed_amount_raw) if rule.mode == "fixed" else
                  uint(attribution["source_amount_raw"]) * rule.ratio_ppm // RATIO_SCALE)
        remaining = uint(portfolio["budget_available_raw"], False)
        if amount > remaining:
            raise ValueError("budget_insufficient")
        return amount, c.token_out
    lots = portfolio.get("lots")
    if not isinstance(lots, list):
        raise MissingEvidence("source_position_basis_missing")
    selected = [lot for lot in lots if lot.get("token") == c.token_in]
    if not selected:
        raise ValueError("attributed_position_insufficient")
    # All lots in this snapshot MUST belong to the already-bound relationship.
    if any(lot.get("relationship_id") != policy["relationship_id"] for lot in selected):
        raise ValueError("lot_relationship_mismatch")
    selected.sort(key=lambda lot: (timestamp(lot["created_at"]), lot["lot_id"]))
    principals = {address(lot["principal_asset"]) for lot in selected}
    if len(principals) != 1:
        raise ValueError("attributed_principal_asset_ambiguous")
    principal = next(iter(principals))
    if rule.mode == "fixed":
        amount = uint(rule.fixed_amount_raw)
    else:
        source_left = uint(c.declared_input_raw) * rule.ratio_ppm // RATIO_SCALE
        amount = 0
        for lot in selected:
            if source_left == 0:
                break
            if lot.get("source_remaining_raw") is None:
                raise MissingEvidence("source_position_basis_missing")
            source = uint(lot["source_remaining_raw"])
            local = uint(lot["token_remaining_raw"])
            take = min(source_left, source)
            amount += local * take // source
            source_left -= take
        if source_left:
            raise ValueError("source_position_basis_insufficient")
    available = sum(uint(lot["token_remaining_raw"], False)
                    - uint(lot["reserved_raw"], False) for lot in selected)
    if any(uint(lot["reserved_raw"], False) > uint(lot["token_remaining_raw"], False)
           for lot in selected) or amount > available:
        raise ValueError("attributed_position_insufficient")
    return amount, principal


def evaluate_candidate(c: Candidate, at: float, snapshots: dict, enabled=False,
                       *, feed_max_age_seconds=3.0, deployment_monitor=None) -> dict:
    """Enable only the OFFLINE evaluator. Result never grants live eligibility.

    Supplied snapshots are trusted recorded inputs, not authenticated by this
    function. Missing historical context is not replaced by today's state.
    """
    at = timestamp(at)
    if type(feed_max_age_seconds) not in (int, float) or not 0 < feed_max_age_seconds <= 6:
        raise ValueError("invalid_feed_max_age_seconds")
    checks, details = {}, {}

    def check(name, fn):
        try:
            value = fn()
            checks[name] = {"status": "pass"}
            return value
        except MissingEvidence as exc:
            checks[name] = {"status": "unverifiable", "reason": str(exc)}
        except (ValueError, KeyError, TypeError, IndexError, OverflowError) as exc:
            # KeyError and malformed input text must not leak arbitrary payloads.
            reason = str(exc) if type(exc) is ValueError else "invalid_" + name + "_snapshot"
            checks[name] = {"status": "reject", "reason": reason}
        return None

    def fresh():
        if c.observation_source != "feed":
            raise ValueError("not_live_feed")
        if c.fresh is not True:
            raise ValueError("feed_was_not_fresh")
        if c.received_at is None or c.feed_timestamp is None:
            raise MissingEvidence("feed_timing_missing")
        received, source = timestamp(c.received_at), timestamp(c.feed_timestamp)
        if (not source <= received <= at or at - source > feed_max_age_seconds
                or at - received > feed_max_age_seconds):
            raise ValueError("feed_intent_expired")
        if c.side == "BUY" and uint(c.metadata["permit_deadline"]) < at:
            raise ValueError("permit_expired")
        return True

    check("freshness", fresh)
    checks["semantics"] = ({"status": "reject", "reason": ";".join(c.blockers)}
                           if c.blockers else {"status": "pass"})

    def deployment():
        if deployment_monitor is not None:
            deployment_monitor.require_ready()
        # Only the explicitly injected runtime monitor removes the deployment
        # TTL. Historical replay and account/market/portfolio TTLs are unchanged.
        state = _snapshot(snapshots, "deployment", at, 3 if deployment_monitor is None else None)
        hash32(state.payload.get("block_hash"))
        result = verify_deployment(state.payload)
        return {**result, "block_hash": state.payload["block_hash"],
                "observed_at": state.observed_at, "provenance": state.provenance}

    deployed = check("deployment", deployment) if c.route_kind == "relay_wrapper" else None
    if c.route_kind != "relay_wrapper":
        checks["deployment"] = {"status": "not_applicable", "scope": "race_adapter_only"}
    if deployed:
        details["deployment"] = deployed

    def attribution():
        if c.side == "BUY":
            return associate_order(c, _snapshot(snapshots, "order", at), at)
        if not c.metadata.get("signature_valid"):
            raise ValueError("userop_signature_invalid")
        state = _snapshot(snapshots, "account", at, 3)
        p = state.payload
        if (p.get("wallet") != c.wallet or p.get("chain_id") != R.CHAIN_ID
                or R.delegation(p.get("code", "")) != R.SIMPLE_ACCOUNT):
            raise ValueError("account_implementation_mismatch")
        hash32(p.get("block_hash"))
        return {"basis": "userop_v08_ecdsa_and_observed_delegation",
                "source_amount_raw": c.declared_input_raw,
                "account_state_scope": "observed_not_transaction_prestate",
                "provenance": state.provenance, "observed_at": state.observed_at}

    attr = check("attribution", attribution)
    if attr:
        details["attribution"] = attr

    def decision():
        if not enabled:
            raise ValueError("offline_evaluator_disabled")
        prerequisites = ("freshness", "semantics", "attribution")
        if c.route_kind == "relay_wrapper":
            prerequisites += ("deployment",)
        if any(checks[k]["status"] != "pass" for k in prerequisites):
            raise MissingEvidence("early_prerequisites_not_met")
        policy = _snapshot(snapshots, "policy", at, 3).payload
        state = _snapshot(snapshots, "portfolio", at, 3).payload
        if (policy.get("enabled") is not True or policy.get("stop_active") is not False
                or policy.get("smart_wallet") != c.wallet):
            raise ValueError("relationship_disabled_stopped_or_mismatched")
        follower = address(policy["follower"])
        if follower == R.NATIVE or not policy.get("relationship_id") or not policy.get("config_snapshot_hash"):
            raise ValueError("relationship_identity_missing")
        _bound(state, policy)
        key = c.relationship_key(policy["relationship_id"], follower)
        if key in state["consumed_operation_keys"]:
            raise ValueError("operation_already_consumed")
        if state.get("source_orphaned") is not False:
            raise ValueError("source_canonical_status_unavailable_or_orphaned")
        protocol = "relay_solver" if c.side == "BUY" else ("0x" if c.route_kind.endswith("0x") else "kyber")
        if protocol not in policy["allowed_protocols"] or "kyber" not in policy["execution_providers"]:
            raise ValueError("protocol_or_provider_not_allowed")
        trusted = {address(a) for a in policy["allowed_assets"]}
        funding = c.token_in if c.side == "BUY" else c.token_out
        if funding not in trusted:
            raise ValueError("funding_asset_not_allowed")
        amount, output = _planned_amount(c, policy, state, attr)
        if amount <= 0:
            raise ValueError("planned_amount_rounds_to_zero")
        if output not in trusted and c.side == "SELL":
            raise ValueError("principal_asset_not_allowed")
        if amount > uint(policy["max_input_raw"]):
            raise ValueError("single_trade_limit_exceeded")
        market = _snapshot(snapshots, "market", at, 3).payload
        _bound(market, policy)
        qp = QuotePolicy(**policy["quote_policy"])
        quote, reference = Quote(**market["quote"]), Quote(**market["reference"])
        for q in (quote, reference):
            uint(q.amount_in_raw)
            uint(q.amount_out_raw)
            timestamp(q.observed_at)
            if q.gas_estimate_raw is not None:
                uint(q.gas_estimate_raw, False)
            if at < q.observed_at or at - q.observed_at > qp.max_age_seconds:
                raise ValueError("quote_missing_or_expired")
        if (quote.protocol != "kyber" or quote.amount_in_raw != str(amount)
                or quote.input_asset != c.token_in or quote.output_asset != output):
            raise ValueError("quote_request_mismatch")
        if output != c.token_out:
            raise MissingEvidence("cross_asset_source_price_basis_missing")
        minimum = (attr["order_minimum_raw"] if c.side == "BUY" else c.minimum_output_raw)
        if minimum is None:
            raise MissingEvidence("source_price_limit_missing")
        # This is an intent price bound, NOT actual_input_debit / output_credit.
        signal = Signal(c.tx_hash, c.wallet, "third_party" if c.side == "BUY" else "bundled_account",
                        c.side, c.path, None, "", token_in=c.token_in, token_out=output,
                        amount_in_raw=attr["source_amount_raw"], amount_limit_raw=minimum,
                        exact_in=True, protocol="kyber", fresh=True)
        ok, reason, risk = assess_quote(signal, quote, reference, qp, market["gas_price_wei"], at)
        if not ok:
            raise ValueError(reason)
        protected_minimum = max(uint(risk["minimum_amount_out_raw"]),
                                uint(risk["scaled_source_minimum_out_raw"]))
        return {"relationship_key": key, "relationship_id": policy["relationship_id"],
                "follower": follower, "config_snapshot_hash": policy["config_snapshot_hash"],
                "input_asset": c.token_in, "output_asset": output,
                "amount_in_raw": str(amount), "minimum_output_raw": str(protected_minimum),
                "risk": risk, "amount_basis": ("fixed_configuration" if policy[
                    "buy_rule" if c.side == "BUY" else "sell_rule"]["mode"] == "fixed" else
                    "order_source_payment" if c.side == "BUY" else "declared_sell_mapped_to_attributed_lots")}

    planned = check("decision", decision)
    if planned:
        details["plan_request"] = planned

    def preparation():
        if planned is None:
            raise MissingEvidence("decision_not_passed")
        p = _snapshot(snapshots, "preparation", at, 2).payload
        if timestamp(p["valid_until"]) <= at:
            raise ValueError("preparation_expired")
        if (p.get("relationship_key") != planned["relationship_key"]
                or p.get("config_snapshot_hash") != planned["config_snapshot_hash"]):
            raise ValueError("preparation_binding_mismatch")
        tx = p["transaction"]
        decoded = decode_kyber_swap(tx["data"])
        if (tx.get("to") != R.KYBER_META_AGGREGATION_ROUTER_V2 or tx.get("from") != planned["follower"]
                or tx.get("chainId") != R.CHAIN_ID or str(tx.get("value")) != "0"
                or decoded["dst_receiver"] != planned["follower"]
                or decoded["src_token"] != planned["input_asset"]
                or decoded["dst_token"] != planned["output_asset"]
                or decoded["amount_raw"] != planned["amount_in_raw"]
                or uint(decoded["minimum_amount_out_raw"]) < uint(planned["minimum_output_raw"])):
            raise ValueError("prepared_transaction_mismatch")
        if p.get("transaction_sha256") != fingerprint(tx):
            raise ValueError("preparation_integrity_mismatch")
        preflight = p["preflight"]
        if preflight.get("transaction_sha256") != fingerprint(tx):
            raise ValueError("preflight_transaction_mismatch")
        if uint(str(tx["nonce"]), False) != uint(str(preflight["network_pending_nonce"]), False):
            raise ValueError("pending_nonce_mismatch")
        for name in ("simulation", "balance", "allowance", "gas", "nonce", "configuration", "budget"):
            if preflight.get(name) is not True:
                raise ValueError("preflight_" + name + "_not_passed")
        return True

    check("preparation", preparation)
    return {"candidate": c.to_dict(), "evaluated_at": at, "checks": checks, **details,
            "decision_passed": checks["decision"]["status"] == "pass",
            "preparation_passed": checks["preparation"]["status"] == "pass",
            "live_enabled": False, "copy_eligible": False}


def reconcile(candidate, truth):
    """Post-hoc labels are used ONLY after evaluating the independent early path."""
    if not truth:
        return "pending"
    if (truth.get("tx_hash") != candidate.tx_hash or truth.get("wallet") != candidate.wallet):
        return "truth_identity_mismatch"
    if truth.get("canonical_status") == "orphaned":
        return "source_orphaned"
    if truth.get("execution_status") in {"failed", "reverted"}:
        return "source_failed"
    if truth.get("stage") not in {"swap_evidenced", "relay_buy_evidenced", "relay_sell_evidenced"}:
        return "pending"
    if truth.get("execution_status") != "success":
        return "pending"
    if [truth.get(k) for k in ("behavior", "token_in", "token_out")] != [
            candidate.side, candidate.token_in, candidate.token_out]:
        return "direction_or_asset_mismatch"
    evidence = truth.get("evidence", {})
    if evidence.get("relay_order_id", evidence.get("relay_deposit_order_id")) != candidate.order_id:
        return "order_mismatch"
    if candidate.side == "SELL" and evidence.get("actual_input_debit_raw") != candidate.declared_input_raw:
        return "input_amount_mismatch"
    return "matched"


def replay_cases(cases, enabled=False):
    rows, errors, seen = [], [], set()
    duplicates = 0
    for index, case in enumerate(cases):
        try:
            tx = transaction_from_record(case["transaction"])
            wallet = address(case["wallet"])
            key = (tx.hash, wallet, case.get("relationship_id"))
            if key in seen:
                duplicates += 1
                continue
            seen.add(key)
            parsed = parse_candidates(tx, wallet)
            at = case.get("decision_at", tx.received_at)
            if at is None:
                # Still report parsing, never manufacture historical time.
                results = [{"candidate": c.to_dict(), "decision_passed": False,
                            "preparation_passed": False,
                            "checks": {"timing": {"status": "unverifiable", "reason": "decision_time_missing"}}}
                           for c in parsed.candidates]
            else:
                results = [evaluate_candidate(c, at, case.get("snapshots", {}), enabled)
                           for c in parsed.candidates]
            for result, candidate in zip(results, parsed.candidates):
                result["posthoc_reconciliation"] = reconcile(candidate, case.get("truth"))
                # This timestamp is comparison-only; never passed to evaluate_candidate.
                strict_at = case.get("strict_evidence_observed_at")
                result["decision_lead_ms"] = None
                if result["decision_passed"] and strict_at is not None and at is not None:
                    strict_at = timestamp(strict_at)
                    if strict_at >= at:
                        result["decision_lead_ms"] = round((strict_at - at) * 1000, 3)
            rows.append({"tx_hash": tx.hash, "wallet": wallet,
                         "relationship_id": case.get("relationship_id"),
                         "observation_source": tx.observation_source,
                         "expected_side": case.get("expected_side", "unlabeled"),
                         "record_id": case.get("record_id"), "provenance": case.get("provenance"),
                         "parse_reasons": list(parsed.reasons), "results": results})
        except (ValueError, TypeError, KeyError, IndexError, OverflowError, AttributeError):
            errors.append({"index": index, "reason": "invalid_replay_case"})
    groups = {}
    for row in rows:
        key = row["observation_source"] + ":" + str(row["expected_side"])
        group = groups.setdefault(key, Counter())
        group["records"] += 1
        group["parsed_records"] += bool(row["results"])
        for name in ("semantics", "deployment", "attribution", "decision", "preparation"):
            for status in ("pass", "reject", "unverifiable", "not_applicable"):
                group[name + "_" + status] += sum(
                    result.get("checks", {}).get(name, {}).get("status") == status
                    for result in row["results"])
    reasons = Counter(check["reason"] for row in rows for result in row["results"]
                      for check in result.get("checks", {}).values() if check.get("reason"))
    return {"rule_version": RULE_VERSION, "offline_only": True, "live_enabled": False,
            "input_records": len(cases), "duplicate_records_skipped": duplicates,
            "invalid_records": len(errors),
            "copy_eligible": False, "groups": groups, "rejection_or_missing_reasons": dict(reasons),
            "rows": rows, "errors": errors,
            "notes": ["groups count records; stage counts count parsed operations",
                      "posthoc truth never enters early evaluation",
                      "decision_lead_ms requires two recorded times and is not end-to-end latency savings",
                      "unknown labels are not negative truth; no live false-follow rate inferred",
                      "snapshots require trustworthy capture provenance; hashes are integrity, not authentication"]}
