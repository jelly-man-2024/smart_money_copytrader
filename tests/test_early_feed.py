"""Offline tests: no network, signing, runtime Store, or live configuration changes."""
from copy import deepcopy
from dataclasses import asdict, replace
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

from eth_abi import decode, encode
from eth_account.messages import encode_typed_data
from eth_utils import keccak

from smart_money import registry as R
from smart_money.decode import CALLS, PACKED_OPS, KYBER_SWAP_EXECUTION
from smart_money.early_intent import (OUTER_TYPES, WRAPPER_TYPES, Observation, associate_order, canonical,
                                    fingerprint, parse_candidates, userop_digest, userop_signature_valid)
from smart_money.early_replay import (evaluate_candidate, reconcile, replay_cases,
                                    transaction_from_record)
from smart_money.quotes import Quote, QuotePolicy
from scripts.replay_early_feed import mysql_cases
from smart_money.relay_race import RACE_ADDRESS, RACE_RULE, RACE_CODE_HASH

ROOT = Path(__file__).resolve().parents[1]
A = "0x" + "11" * 20
B = "0x" + "22" * 20


def samples():
    return json.loads((ROOT / "data/early_feed_public_samples_2026-09-14.json").read_text())["cases"]


def transaction_record(tx):
    return {**asdict(tx), "data": "0x" + tx.data.hex(), "value": str(tx.value)}


def order_for(c):
    return {"requests": [{"id": "0x" + "ab" * 32, "recipient": c.wallet,
                          "status": "pending", "user": "source-payer",
                          "data": {"inTxs": [{"hash": "source-hash", "chainId": 792703809,
                                                "status": "success"}]},
                          "protocol": {"orderId": c.order_id, "deposit": {"origin": {
                              "amount": "1000000000", "chainId": 792703809,
                              "currency": next(iter(R.RELAY_USDG_EQUIVALENTS))[1],
                              "depositor": "source-payer", "transactionId": "source-hash"}},
                              "orderData": {"output": {"chainId": R.CHAIN_ID, "payments": [{"recipient": c.wallet,
                                  "currency": c.token_out, "minimumAmount": "1000000"}]}}}}]}


def snapshot(payload, at=100, provenance="synthetic_test_not_historical"):
    return {"payload": payload, "observed_at": at, "provenance": provenance}


class EarlyParsingTests(unittest.TestCase):
    def setUp(self):
        self.buy_case = next(c for c in samples() if c["expected_side"] == "BUY")
        self.sell_case = next(c for c in samples() if c["expected_side"] == "SELL")
        self.buy = transaction_from_record(self.buy_case["transaction"])
        self.sell = transaction_from_record(self.sell_case["transaction"])

    def test_real_buy_and_sell_structures(self):
        buy = parse_candidates(self.buy, self.buy_case["wallet"]).candidates[0]
        sell = parse_candidates(self.sell, self.sell_case["wallet"]).candidates[0]
        self.assertEqual(buy.route_kind, "relay_direct_kyber")
        self.assertEqual(sell.route_kind, "relay_sell_kyber")
        self.assertFalse(buy.blockers)
        self.assertFalse(sell.blockers)
        self.assertTrue(sell.metadata["signature_valid"])
        self.assertFalse(sell.metadata["authorization_verified"])
        self.assertFalse(buy.to_dict()["copy_eligible"])

    def test_sample_a_wrapper_requires_deployment_beyond_roundtrip(self):
        t = json.loads((ROOT / "data/relay_0a2b8f36_sample_a_2026-09-14.json").read_text())["transaction"]
        tx = replace(self.buy, hash=t["hash"], data=bytes.fromhex(t["input"][2:]))
        result = parse_candidates(tx, "0x1cfbe3af88266ccca29372661f45261c7d19be09")
        self.assertEqual(len(result.candidates), 1)
        c = result.candidates[0]
        self.assertFalse(c.blockers)
        self.assertEqual(c.minimum_output_raw, "4296079466195047419727677")
        self.assertEqual(c.metadata["required_runtime_code_hash"], RACE_CODE_HASH)
        result = evaluate_candidate(c, c.received_at, {}, enabled=True)
        self.assertEqual(result["checks"]["deployment"]["status"], "unverifiable")
        self.assertFalse(result["decision_passed"])

    def test_wrapper_parameter_guards_and_inner_unknown_preserved(self):
        t = json.loads((ROOT / "data/relay_0a2b8f36_sample_a_2026-09-14.json").read_text())["transaction"]
        tx = replace(self.buy, data=bytes.fromhex(t["input"][2:]))
        wallet = "0x1cfbe3af88266ccca29372661f45261c7d19be09"
        outer, trailer = canonical(OUTER_TYPES, tx.data[4:], 32)
        w, _ = canonical(WRAPPER_TYPES, outer[2][1][3][4:])
        def altered(field, value):
            new = list(w); new[field] = value
            v = list(outer); calls = list(v[2]); swap = list(calls[1])
            swap[3] = bytes.fromhex("998b5942") + encode(WRAPPER_TYPES, new)
            calls[1] = tuple(swap); v[2] = calls
            return replace(tx, data=tx.data[:4] + encode(OUTER_TYPES, v) + trailer)
        for field, value in ((3, 0), (4, A), (5, A), (6, ()), (8, b"not-an-order")):
            self.assertFalse(parse_candidates(altered(field, value), wallet).candidates)
        for field, value in ((0, R.NATIVE), (1, R.NATIVE), (2, 1), (3, 0), (4, b"")):
            routes = list(w[6]); route = list(routes[0]); route[field] = value; routes[0] = tuple(route)
            self.assertFalse(parse_candidates(altered(6, routes), wallet).candidates)
        c = parse_candidates(tx, wallet).candidates[0]
        self.assertFalse(c.metadata["inner_routes_semantically_decoded"])
        self.assertTrue(all(r["selected_or_executed"] == "unknown" for r in c.metadata["routes"]))
        self.assertFalse(parse_candidates(altered(7, False), wallet).candidates[0].metadata["emit_route_events"])

    def test_wrong_chain_contract_recipient_and_trailer_rejected(self):
        for tx, wallet in [(replace(self.buy, chain_id=1), self.buy_case["wallet"]),
                           (replace(self.buy, to=A), self.buy_case["wallet"]),
                           (self.buy, A),
                           (replace(self.buy, data=self.buy.data + bytes(32)), self.buy_case["wallet"]),
                           (replace(self.buy, data=self.buy.data[:100]), self.buy_case["wallet"])]:
            with self.subTest(to=tx.to, wallet=wallet):
                self.assertFalse(parse_candidates(tx, wallet).candidates)

    def test_allow_failure_rejected(self):
        v, trailer = canonical(OUTER_TYPES, self.buy.data[4:], 32)
        v = list(v)
        calls = list(v[2])
        calls[1] = (calls[1][0], True, calls[1][2], calls[1][3])
        v[2] = calls
        tx = replace(self.buy, data=self.buy.data[:4] + encode(OUTER_TYPES, v) + trailer)
        self.assertFalse(parse_candidates(tx, self.buy_case["wallet"]).candidates)

    def test_actual_userop_signature_cannot_be_forged_or_reused(self):
        (ops, _), _ = canonical([PACKED_OPS, "address"], self.sell.data[4:])
        op = next(op for op in ops if op[0] == self.sell_case["wallet"])
        self.assertTrue(userop_signature_valid(op))
        for field, value in ((0, A), (1, op[1] + 1), (3, op[3] + b"\x00"),
                             (8, bytes(65)), (2, b"\x77\x02"), (7, b"paymaster")):
            forged = list(op)
            forged[field] = value
            self.assertFalse(userop_signature_valid(forged))

    def test_digest_matches_independent_eip712_encoder(self):
        op = decode([PACKED_OPS, "address"], self.sell.data[4:])[0][0]
        names = ["sender", "nonce", "initCode", "callData", "accountGasLimits",
                 "preVerificationGas", "gasFees", "paymasterAndData"]
        types = ["address", "uint256", "bytes", "bytes", "bytes32", "uint256", "bytes32", "bytes"]
        message = encode_typed_data(
            domain_data={"name": "ERC4337", "version": "1", "chainId": R.CHAIN_ID,
                         "verifyingContract": R.ENTRYPOINT},
            message_types={"PackedUserOperation": [{"name": n, "type": t} for n, t in zip(names, types)]},
            message_data=dict(zip(names, op[:8])))
        self.assertEqual(userop_digest(op), keccak(b"\x19" + message.version + message.header + message.body))

    def test_bundled_other_wallet_never_becomes_a_candidate(self):
        self.assertFalse(parse_candidates(self.sell, B).candidates)

    def test_claim_then_swap_is_preserved(self):
        (ops, beneficiary), _ = canonical([PACKED_OPS, "address"], self.sell.data[4:])
        ops = list(ops)
        index = next(i for i, op in enumerate(ops) if op[0] == self.sell_case["wallet"])
        op = list(ops[index])
        (calls,), _ = canonical([CALLS], op[3][4:])
        claim = bytes.fromhex("815a4392") + encode(["address", "bool"], [op[0], False])
        op[3] = op[3][:4] + encode([CALLS], [[(R.RIPE_CLAIM, 0, claim), *calls]])
        ops[index] = op
        tx = replace(self.sell, data=self.sell.data[:4] + encode([PACKED_OPS, "address"], [ops, beneficiary]))
        result = parse_candidates(tx, self.sell_case["wallet"])
        self.assertEqual(len(result.candidates), 1)
        self.assertFalse(result.candidates[0].blockers)
        self.assertFalse(result.candidates[0].metadata["signature_valid"])

    def test_transfer_approval_unknown_are_not_trades(self):
        for selector in ("a9059cbb", "095ea7b3", "deadbeef"):
            tx = replace(self.buy, to=A, data=bytes.fromhex(selector) + encode(["address", "uint256"], [B, 100]))
            self.assertFalse(parse_candidates(tx, B).candidates)


class EarlyDecisionTests(unittest.TestCase):
    def setUp(self):
        case = next(c for c in samples() if c["expected_side"] == "BUY")
        tx = transaction_from_record(case["transaction"])
        self.c = replace(parse_candidates(tx, case["wallet"]).candidates[0],
                         received_at=100, feed_timestamp=100, fresh=True)
        self.order = order_for(self.c)
        self.policy = {"relationship_id": "test-1", "follower": A, "smart_wallet": self.c.wallet,
                       "config_snapshot_hash": "test-snapshot", "enabled": True, "stop_active": False,
                       "allowed_assets": [R.USDG], "allowed_protocols": ["relay_solver", "kyber", "0x"],
                       "execution_providers": ["kyber"], "max_input_raw": "1000000",
                       "buy_rule": {"mode": "fixed", "fixed_amount_raw": "100000"},
                       "sell_rule": {"mode": "proportional", "ratio_ppm": 1000000},
                       "quote_policy": asdict(QuotePolicy())}
        binding = {k: self.policy[k] for k in ("relationship_id", "follower", "smart_wallet", "config_snapshot_hash")}
        self.portfolio = {**binding, "budget_available_raw": "1000000", "lots": [],
                          "source_orphaned": False, "consumed_operation_keys": []}
        self.quote = Quote("kyber", "test", 1, "0x" + "ab" * 32, 100, R.USDG,
                           self.c.token_out, "100000", "200", "350000")
        ref = replace(self.quote, amount_in_raw="1000", amount_out_raw="2")
        self.market = {**binding, "quote": self.quote.to_dict(), "reference": ref.to_dict(), "gas_price_wei": "1"}
        self.snapshots = {"order": snapshot(self.order), "policy": snapshot(self.policy),
                          "portfolio": snapshot(self.portfolio), "market": snapshot(self.market)}

    def evaluate(self, **kw):
        return evaluate_candidate(self.c, kw.pop("at", 100.1), kw.pop("snapshots", self.snapshots),
                                  enabled=kw.pop("enabled", True))

    def test_order_does_not_need_destination_receipt_but_is_not_common_control(self):
        a = associate_order(self.c, Observation(100, "synthetic", self.order), 100.1)
        self.assertEqual(a["source_amount_raw"], "1000000000")
        self.assertNotEqual(a["source_amount_raw"], self.c.declared_input_raw)
        self.assertFalse(a["cross_chain_common_control_proven"])
        self.assertNotIn("actual_input_debit_raw", a)

    def test_order_mutation_negative_controls(self):
        for change in ("recipient", "payer", "order_id", "payment", "source_status", "duplicate", "status", "mapping", "chain"):
            doc = deepcopy(self.order)
            r = doc["requests"][0]
            if change == "recipient": r["recipient"] = B
            elif change == "payer": r["user"] = "gift-sender"
            elif change == "order_id": r["protocol"]["orderId"] = r["id"]
            elif change == "payment": r["protocol"]["orderData"]["output"]["payments"][0]["currency"] = B
            elif change == "source_status": r["data"]["inTxs"][0]["status"] = "failed"
            elif change == "duplicate": doc["requests"].append(deepcopy(r))
            elif change == "status": r["status"] = "refunded"
            elif change == "mapping": r["protocol"]["deposit"]["origin"]["currency"] = "unregistered"
            elif change == "chain": r["protocol"]["orderData"]["output"]["chainId"] = 8453
            with self.subTest(change=change), self.assertRaises(ValueError):
                associate_order(self.c, Observation(100, "test", doc), 100.1)

    def test_destination_chain_alias_is_exact_and_preserves_raw_evidence(self):
        for raw in (4663, "4663", "robinhood", 4663.0, True, None, "base", "Robinhood", "04663"):
            doc = deepcopy(self.order)
            doc["requests"][0]["protocol"]["orderData"]["output"]["chainId"] = raw
            with self.subTest(raw=raw):
                if type(raw) is int or raw in ("4663", "robinhood"):
                    result = associate_order(self.c, Observation(100, "test", doc), 100.1)
                    self.assertEqual(result["destination_chain_id_raw"], raw)
                    self.assertEqual(result["destination_chain_id"], "4663")
                else:
                    with self.assertRaisesRegex(ValueError, "destination_chain"):
                        associate_order(self.c, Observation(100, "test", doc), 100.1)

    def test_future_order_missing_context_and_disabled_default(self):
        self.snapshots["order"]["observed_at"] = 101
        self.assertEqual(self.evaluate()["checks"]["attribution"]["status"], "unverifiable")
        self.assertFalse(self.evaluate()["decision_passed"])
        self.snapshots["order"]["observed_at"] = 100
        self.assertTrue(self.evaluate()["decision_passed"])
        self.assertEqual(self.evaluate(enabled=False)["checks"]["decision"]["reason"], "offline_evaluator_disabled")
        self.assertFalse(self.evaluate()["copy_eligible"])
        self.assertFalse(self.evaluate()["preparation_passed"])

    def test_race_deployment_is_required_and_rehashes_actual_code(self):
        self.c = replace(self.c, route_kind="relay_wrapper")
        self.assertFalse(self.evaluate()["decision_passed"])
        fixture = json.loads((ROOT / "data/relay_race_runtime_2026-09-14.json").read_text())
        deployment = {"chain_id": R.CHAIN_ID, "contract": RACE_ADDRESS, "rule": RACE_RULE,
                      "code": fixture["runtime_code"], "block_hash": "0x" + "ab" * 32}
        self.snapshots["deployment"] = snapshot(deployment)
        self.assertTrue(self.evaluate()["decision_passed"])
        for field, value in (("chain_id", 8453), ("contract", A), ("rule", "other"),
                              ("block_hash", "invalid"), ("code", "0x"),
                              ("code", "0x00" + fixture["runtime_code"][4:])):
            with self.subTest(field=field):
                s = deepcopy(self.snapshots)
                s["deployment"]["payload"][field] = value
                s["deployment"]["payload"]["claimed_code_hash"] = RACE_CODE_HASH
                self.assertFalse(self.evaluate(snapshots=s)["decision_passed"])
        for observed_at in (101, 90):
            s = deepcopy(self.snapshots); s["deployment"]["observed_at"] = observed_at
            self.assertFalse(self.evaluate(snapshots=s)["decision_passed"])
        s = deepcopy(self.snapshots); s["deployment"]["capture_started_at"] = 90
        self.assertFalse(self.evaluate(snapshots=s)["decision_passed"])

    def test_race_final_truth_does_not_replace_early_deployment(self):
        case = deepcopy(next(c for c in samples() if c["expected_side"] == "BUY"))
        t = json.loads((ROOT / "data/relay_0a2b8f36_sample_a_2026-09-14.json").read_text())["transaction"]
        case["transaction"]["data"] = t["input"]
        case["wallet"] = "0x1cfbe3af88266ccca29372661f45261c7d19be09"
        before = replay_cases([case], enabled=True)
        case["truth"] = {"execution_status": "success", "runtime_code_hash": RACE_CODE_HASH}
        after = replay_cases([case], enabled=True)
        self.assertEqual(before["groups"], after["groups"])
        self.assertEqual(after["groups"]["feed:BUY"]["deployment_unverifiable"], 1)

    def test_dynamic_target_allowed_but_funding_asset_not_bypassed(self):
        self.assertTrue(self.evaluate()["decision_passed"])
        self.policy["allowed_assets"] = []
        self.assertEqual(self.evaluate()["checks"]["decision"]["reason"], "funding_asset_not_allowed")

    def test_stale_future_feed_and_backfill(self):
        for c in (replace(self.c, fresh=False), replace(self.c, observation_source="backfill"),
                  replace(self.c, received_at=101), replace(self.c, feed_timestamp=90)):
            r = evaluate_candidate(c, 100.1, self.snapshots, enabled=True)
            self.assertFalse(r["decision_passed"])
        self.assertFalse(self.evaluate(at=104)["decision_passed"])

    def test_stop_budget_position_quote_and_config_gates(self):
        for change, reason in (("stop", "relationship_disabled_stopped_or_mismatched"),
                               ("budget", "budget_insufficient"), ("config", "snapshot_relationship_or_config_mismatch"),
                               ("price", "intent_price_limit_not_met"), ("reference", "quote_missing_or_expired")):
            s = deepcopy(self.snapshots)
            if change == "stop": s["policy"]["payload"]["stop_active"] = True
            elif change == "budget": s["portfolio"]["payload"]["budget_available_raw"] = "1"
            elif change == "config": s["market"]["payload"]["config_snapshot_hash"] = "changed"
            elif change == "price": s["market"]["payload"]["quote"]["amount_out_raw"] = "1"
            elif change == "reference": s["market"]["payload"]["reference"]["observed_at"] = 50
            self.assertEqual(self.evaluate(snapshots=s)["checks"]["decision"]["reason"], reason)

    def test_deduplication_is_stage_independent_and_relationship_scoped(self):
        key = self.c.relationship_key("test-1", A)
        self.assertEqual(key, replace(self.c, path="incoming").relationship_key("test-1", A))
        self.assertEqual(key, replace(self.c, tx_hash="0x" + "ef" * 32).relationship_key("test-1", A))
        self.assertNotEqual(key, self.c.relationship_key("test-2", A))
        self.assertNotEqual(key, self.c.relationship_key("test-1", B))
        self.portfolio["consumed_operation_keys"].append(key)
        self.assertEqual(self.evaluate()["checks"]["decision"]["reason"], "operation_already_consumed")

    def test_proportional_buy_uses_order_payment_not_solver_amount(self):
        self.policy["buy_rule"] = {"mode": "proportional", "ratio_ppm": 100}
        result = self.evaluate()
        self.assertTrue(result["decision_passed"])
        self.assertEqual(result["plan_request"]["amount_in_raw"], "100000")

    def test_truth_never_leaks_into_early_decision(self):
        case = next(c for c in samples() if c["expected_side"] == "BUY")
        first = replay_cases([case], enabled=True)
        case["truth"] = {"stage": "relay_buy_evidenced", "behavior": "BUY", "execution_status": "success",
                         "tx_hash": self.c.tx_hash, "wallet": self.c.wallet, "token_in": self.c.token_in,
                         "token_out": self.c.token_out, "evidence": {"relay_order_id": self.c.order_id}}
        second = replay_cases([case], enabled=True)
        self.assertEqual(first["groups"], second["groups"])
        self.assertEqual(first["rows"][0]["results"][0]["checks"], second["rows"][0]["results"][0]["checks"])
        self.assertEqual(second["rows"][0]["results"][0]["posthoc_reconciliation"], "matched")

    def test_reconciliation_pending_failure_and_mismatch_are_distinct(self):
        self.assertEqual(reconcile(self.c, None), "pending")
        truth = {"tx_hash": self.c.tx_hash, "wallet": self.c.wallet, "execution_status": "failed"}
        self.assertEqual(reconcile(self.c, truth), "source_failed")
        truth["execution_status"] = "reverted"
        self.assertEqual(reconcile(self.c, truth), "source_failed")
        truth.update(stage="relay_buy_evidenced", execution_status="success", behavior="SELL")
        self.assertEqual(reconcile(self.c, truth), "direction_or_asset_mismatch")

    def test_sell_missing_account_history_is_unknown_even_with_valid_signature(self):
        case = next(c for c in samples() if c["expected_side"] == "SELL")
        c = parse_candidates(transaction_from_record(case["transaction"]), case["wallet"]).candidates[0]
        c = replace(c, received_at=100, feed_timestamp=100, fresh=True)
        r = evaluate_candidate(c, 100.1, {}, enabled=True)
        self.assertTrue(c.metadata["signature_valid"])
        self.assertEqual(r["checks"]["attribution"]["status"], "unverifiable")

    def test_sell_maps_declared_fraction_to_only_relationship_lots(self):
        case = next(c for c in samples() if c["expected_side"] == "SELL")
        c = parse_candidates(transaction_from_record(case["transaction"]), case["wallet"]).candidates[0]
        c = replace(c, received_at=100, feed_timestamp=100, fresh=True)
        p = deepcopy(self.policy)
        p.update(smart_wallet=c.wallet, max_input_raw="10000000000000000000")
        binding = {k: p[k] for k in ("relationship_id", "follower", "smart_wallet", "config_snapshot_hash")}
        local = 10 ** 18
        minimum = (int(c.minimum_output_raw) * local + int(c.declared_input_raw) - 1) // int(c.declared_input_raw)
        output = max(100, ((minimum * 2 + 99) // 100) * 100)
        q = replace(self.quote, input_asset=c.token_in, output_asset=R.USDG,
                    amount_in_raw=str(local), amount_out_raw=str(output))
        ref = replace(q, amount_in_raw=str(local // 100), amount_out_raw=str(output // 100))
        state = {**binding, "consumed_operation_keys": [], "source_orphaned": False, "lots": [{
            "lot_id": "lot-1", "created_at": 90, "relationship_id": "test-1", "token": c.token_in,
            "principal_asset": R.USDG, "token_remaining_raw": str(local), "reserved_raw": "0",
            "source_remaining_raw": c.declared_input_raw}]}
        snapshots = {"account": snapshot({"chain_id": R.CHAIN_ID, "wallet": c.wallet,
                                          "code": "0xef0100" + R.SIMPLE_ACCOUNT[2:],
                                          "block_hash": "0x" + "ab" * 32}),
                     "policy": snapshot(p), "portfolio": snapshot(state),
                     "market": snapshot({**binding, "quote": q.to_dict(), "reference": ref.to_dict(),
                                          "gas_price_wei": "1"})}
        r = evaluate_candidate(c, 100.1, snapshots, enabled=True)
        self.assertTrue(r["decision_passed"], r)
        self.assertEqual(r["plan_request"]["amount_in_raw"], str(local))
        for field, value, reason in (("source_remaining_raw", None, "source_position_basis_missing"),
                                     ("source_remaining_raw", "1", "source_position_basis_insufficient"),
                                     ("reserved_raw", str(local), "attributed_position_insufficient"),
                                     ("relationship_id", "another", "lot_relationship_mismatch")):
            altered = deepcopy(snapshots)
            altered["portfolio"]["payload"]["lots"][0][field] = value
            self.assertEqual(evaluate_candidate(c, 100.1, altered, True)["checks"]["decision"]["reason"], reason)

    def test_preparation_binds_exact_transaction_simulation_and_nonce(self):
        planned = self.evaluate()["plan_request"]
        desc = (self.c.token_in, self.c.token_out, (), (), (), (), A,
                int(planned["amount_in_raw"]), int(planned["minimum_output_raw"]), 512, b"")
        data = "0xe21fd0e9" + encode([KYBER_SWAP_EXECUTION], [(A, R.NATIVE, b"", desc, b"")]).hex()
        tx = {"chainId": R.CHAIN_ID, "from": A, "to": R.KYBER_META_AGGREGATION_ROUTER_V2,
              "data": data, "value": "0", "nonce": 4}
        preflight = {k: True for k in ("simulation", "balance", "allowance", "gas", "nonce", "configuration", "budget")}
        preflight.update(transaction_sha256=fingerprint(tx), network_pending_nonce=4)
        payload = {"relationship_key": planned["relationship_key"], "config_snapshot_hash": "test-snapshot",
                   "transaction": tx, "transaction_sha256": fingerprint(tx), "valid_until": 102,
                   "preflight": preflight}
        self.snapshots["preparation"] = snapshot(payload)
        self.assertTrue(self.evaluate()["preparation_passed"])
        for field, value in (("simulation", False), ("allowance", False), ("network_pending_nonce", 5),
                             ("transaction_sha256", "wrong")):
            s = deepcopy(self.snapshots)
            s["preparation"]["payload"]["preflight"][field] = value
            self.assertFalse(self.evaluate(snapshots=s)["preparation_passed"])
        self.snapshots["preparation"]["payload"]["transaction"]["to"] = B
        self.assertFalse(self.evaluate()["preparation_passed"])

    def test_nan_timestamps_negative_amounts_and_missing_candidates_fail_closed(self):
        with self.assertRaises(ValueError): self.evaluate(at=float("nan"))
        self.market["quote"]["amount_out_raw"] = "-1"
        self.assertFalse(self.evaluate()["decision_passed"])
        r = replay_cases([{"transaction": None}], enabled=True)
        self.assertEqual(len(r["errors"]), 1)

    def test_cli_runs_without_network_or_database(self):
        result = subprocess.run([sys.executable, "scripts/replay_early_feed.py", "--input",
                                 "data/early_feed_public_samples_2026-09-14.json", "--evaluate", "--summary"],
                                cwd=ROOT, capture_output=True, text=True, check=True)
        report = json.loads(result.stdout)
        self.assertFalse(report["live_enabled"])
        self.assertEqual(report["signature_valid_count"], 1)
        self.assertFalse(report["errors"])

    def test_mysql_export_is_read_only_and_rolls_back_on_error(self):
        class Cursor:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def execute(self, sql, params=None):
                self.sql.append(sql)
                if "paper_proposals" in sql: raise RuntimeError("test query failure")
            def fetchone(self): return {"t": "test"}
            sql = []
        class Connection:
            rolled_back = closed = False
            def cursor(self): return Cursor()
            def rollback(self): self.rolled_back = True
            def close(self): self.closed = True
        connection = Connection()
        with patch("smart_money.mysql_config.mysql_connection", return_value=connection):
            with self.assertRaises(RuntimeError): mysql_cases(2)
        self.assertTrue(connection.rolled_back and connection.closed)
        self.assertEqual(Cursor.sql[0], "START TRANSACTION READ ONLY")
        self.assertTrue(all(q.startswith(("SELECT", "START")) for q in Cursor.sql))
        self.assertFalse(any("wallet_keys" in q for q in Cursor.sql))
