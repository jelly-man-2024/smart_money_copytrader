"""Persistent execution preparation pipeline with no signing or broadcasting."""
from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import hashlib
import time

from eth_account import Account
from eth_account.typed_transactions import TypedTransaction
from eth_utils import keccak, to_checksum_address
from hexbytes import HexBytes

from .execution_prep import (
    EXECUTION_TARGETS, ReadOnlyExecutionPreflight, UnsignedExecutionPlan,
    build_aggregator_execution_plan, build_execution_plan,
    build_early_aggregator_execution_plan,
    simulate_aggregator_execution,
)
from .key_source import LiveDatabaseSigner, OfflineDatabaseSigner
from .execution_controls import (
    require_mainnet_signing_enabled, require_offline_signing_enabled,
)
from .paper import AGGREGATOR_PROVIDERS
from .quotes import assess_quote
from .verified_feed_intent import VerifiedFeedIntent
from .simulation_diagnostics import AggregatorSimulationError


def check_early_execution_source(store, intent, signal, proposal, now=None):
    """Revalidate the original typed intent at each asynchronous execution boundary."""
    enrolled = "early_trial_id" in proposal["attribution"]
    if intent is None and not enrolled:
        return
    if not enrolled or not isinstance(intent, VerifiedFeedIntent):
        raise ValueError("early execution requires original verified intent and trial")
    expected = intent.quote_signal(time.time() if now is None else now)
    if signal.to_dict() != expected.to_dict():
        raise ValueError("early execution signal differs from verified intent")
    attr = proposal["attribution"]
    if (attr.get("copy_operation_order_id") != intent.candidate.order_id
            or attr.get("smart_wallet") != expected.wallet
            or proposal["source_tx_hash"] != expected.tx_hash
            or proposal["input_asset"] != expected.token_in
            or proposal["output_asset"] != expected.token_out):
        raise ValueError("early proposal differs from verified intent")
    for (payload,) in store.connection.execute(
            "SELECT payload FROM signals WHERE tx_hash=?", (expected.tx_hash,)).fetchall():
        import json
        current = json.loads(payload)
        if current.get("wallet") == expected.wallet and (
                current.get("canonical_status") == "orphaned"
                or current.get("stage") == "failed"):
            raise ValueError("early source failed or orphaned before execution")
    store._check_early_trial(proposal["attribution"], now)
    if proposal.get("status") == "reserved":
        store.execution_budget_evidence(proposal["proposal_id"])


def validate_transaction_matches_plan(transaction: dict,
                                      plan: UnsignedExecutionPlan) -> None:
    """Fail closed if the persisted signable transaction diverges from its plan."""
    required = {"chainId", "nonce", "to", "value", "data", "gas",
                "maxFeePerGas", "maxPriorityFeePerGas", "type"}
    if not isinstance(transaction, dict) or set(transaction) != required:
        raise ValueError("signable transaction fields do not match execution plan")
    try:
        matches = (
            int(transaction["chainId"]) == plan.chain_id
            and int(transaction["type"]) == 2
            and to_checksum_address(transaction["to"]).lower() == plan.to
            and int(transaction["value"]) == int(plan.value_raw)
            and transaction["data"].lower() == plan.data.lower()
            and int(transaction["gas"]) == plan.gas_limit
            and int(transaction["maxFeePerGas"]) == int(plan.max_fee_per_gas)
            and int(transaction["maxPriorityFeePerGas"])
            == int(plan.max_priority_fee_per_gas)
            and isinstance(transaction["nonce"], int)
            and transaction["nonce"] >= 0
        )
    except (AttributeError, KeyError, TypeError, ValueError):
        matches = False
    if not matches:
        raise ValueError("signable transaction does not match execution plan")


@dataclass(frozen=True)
class PreparedExecution:
    plan_id: str
    proposal_id: str
    nonce: int
    transaction: dict
    preflight: dict
    existing: bool = False


class OfflineSignedExecution:
    """Signed bytes container that fails closed under generic object serialization."""

    __slots__ = ("plan_id", "proposal_id", "signed_tx_hash", "_raw_transaction",
                 "preflight")

    def __init__(self, plan_id: str, proposal_id: str, signed_tx_hash: str,
                 raw_transaction: bytes, preflight: dict):
        self.plan_id = plan_id
        self.proposal_id = proposal_id
        self.signed_tx_hash = signed_tx_hash
        self._raw_transaction = raw_transaction
        self.preflight = preflight

    @property
    def raw_transaction(self) -> bytes:
        return self._raw_transaction

    def __repr__(self) -> str:
        return (f"OfflineSignedExecution(plan_id={self.plan_id!r}, "
                f"proposal_id={self.proposal_id!r}, "
                f"signed_tx_hash={self.signed_tx_hash!r}, "
                f"preflight={self.preflight!r})")


@dataclass(frozen=True)
class ReadOnlyBroadcastReview:
    proposal_id: str
    signed_tx_hash: str
    checked_at: float
    evidence: dict


class ExecutionPreparer:
    """Stops at an immutable unsigned plan; it cannot access keys or RPC broadcast."""

    def __init__(self, store, quoter, rpc, quote_policy,
                 allowed_protocols, allowed_assets, allowed_routes,
                 config_snapshot_hash: str, gas_limit_by_protocol: dict | None = None,
                 deadline_seconds: int = 120, fee_headroom_bps: int = 2000):
        self.store, self.quoter, self.rpc = store, quoter, rpc
        self.quote_policy = quote_policy
        self.allowed_protocols = allowed_protocols
        self.allowed_assets = allowed_assets
        self.allowed_routes = allowed_routes
        self.config_snapshot_hash = config_snapshot_hash
        self.gas_limits = gas_limit_by_protocol or {
            "v2": 220000, "v3": 350000, "v4": 500000, "kyber": 600000,
        }
        if (not 30 <= deadline_seconds <= 600
                or not 0 <= fee_headroom_bps <= 5000):
            raise ValueError("invalid execution preparation limits")
        self.deadline_seconds = deadline_seconds
        self.fee_headroom_bps = fee_headroom_bps

    @staticmethod
    def _result(row: dict, existing: bool) -> PreparedExecution:
        return PreparedExecution(row["plan_id"], row["proposal_id"],
                                 int(row["transaction"]["nonce"]),
                                 row["transaction"], row["preflight"], existing)

    async def _simulate_with_gas_retry(self, plan, validate, *, allow_retry=True):
        """One unsigned retry, changing only gas. A wrapped revert may hide OOG."""
        try:
            return plan, await simulate_aggregator_execution(self.rpc, plan)
        except AggregatorSimulationError as original:
            category = (original.diagnostic.get("rpc_error") or {}).get("message_category")
            if (not allow_retry or plan.execution_provider != "kyber"
                    or original.diagnostic.get("failure_kind") != "rpc_failure"
                    or category not in {"execution_reverted", "out_of_gas"}):
                raise
            max_fee = int(plan.max_fee_per_gas)
            gas_limit = min((plan.gas_limit * 3 + 1) // 2, 2_000_000)
            if max_fee > 0:
                gas_limit = min(gas_limit, int(self.quote_policy.max_gas_cost_wei) // max_fee)
            if gas_limit <= plan.gas_limit:
                raise
            retry_plan = replace(plan, gas_limit=gas_limit)
            evidence = {"status": "started", "original_gas_limit": plan.gas_limit,
                        "retry_gas_limit": gas_limit,
                        "original_simulation_failure": original.diagnostic}
            started = time.monotonic()
            try:
                validate()
                # Check funds/allowance/fee budget before the extra simulation.
                # This read-only preflight does not reserve a nonce.
                await ReadOnlyExecutionPreflight(
                    self.rpc, EXECUTION_TARGETS,
                    self.quote_policy.max_gas_cost_wei,
                    max_quote_age_seconds=self.quote_policy.max_age_seconds).check(retry_plan)
                validate()
                result = await simulate_aggregator_execution(self.rpc, retry_plan)
                validate()
                evidence["status"] = "simulation_passed"
                return retry_plan, {**result, "gas_retry": evidence}
            except AggregatorSimulationError as failed:
                evidence["status"] = "failed"
                failed.diagnostic = {**failed.diagnostic, "gas_retry": evidence}
                raise
            finally:
                evidence["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)

    async def prepare(self, signal, proposal_id: str,
                      now: float | None = None, *, early_intent=None) -> PreparedExecution:
        try:
            return await self._prepare(signal, proposal_id, now, early_intent=early_intent)
        except AggregatorSimulationError as exc:
            retry = getattr(self.quoter, "begin_simulation_route_retry", None)
            proposal = self.store.paper_proposal(proposal_id)
            if retry is None or proposal is None or self.store.execution_plan(proposal_id):
                raise
            evidence = retry(signal, proposal["amount_in_raw"], exc.diagnostic)
            if evidence is None:
                raise
            started = time.monotonic()
            try:
                result = await self._prepare(
                    signal, proposal_id, now, early_intent=early_intent,
                    retry_evidence=evidence,
                    minimum_floor=exc.diagnostic["minimum_amount_out_raw"],
                    original_deadline=exc.diagnostic["deadline"])
                evidence["status"] = "prepared"
                return result
            except BaseException:
                evidence["status"] = "failed"
                raise
            finally:
                evidence["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)

    async def _prepare(self, signal, proposal_id, now=None, *, early_intent=None,
                       retry_evidence=None, minimum_floor=None, original_deadline=None):
        fixed_now = now
        existing = self.store.execution_plan(proposal_id)
        if existing:
            check_early_execution_source(self.store, early_intent, signal,
                                         self.store.paper_proposal(proposal_id), now)
            if existing["status"] != "prepared":
                raise ValueError("execution plan is not reusable")
            return self._result(existing, True)
        proposal = self.store.paper_proposal(proposal_id)
        if proposal is None or proposal["status"] != "reserved":
            raise ValueError("proposal is not reserved")
        attribution = proposal["attribution"]
        check_early_execution_source(self.store, early_intent, signal, proposal, now)
        if "early_trial_id" in attribution:
            self.store.check_early_trial_proposal(proposal_id)
        follower = attribution.get("follower_wallet")
        relationship = attribution.get("relationship_id")
        snapshot = attribution.get("config_snapshot_hash")
        if (not follower or not relationship or snapshot != self.config_snapshot_hash
                or proposal["source_event_id"] != signal.event_id):
            raise ValueError("proposal attribution or config snapshot mismatch")
        quote, reference, gas_price_raw = await self.quoter.quote_with_reference(
            signal, proposal["amount_in_raw"])
        now = time.time() if now is None else now
        swap = None
        if signal.protocol in AGGREGATOR_PROVIDERS:
            swap = await self.quoter.build_aggregator_transaction(
                signal, proposal["amount_in_raw"], follower,
                self.quote_policy.max_slippage_bps,
                original_deadline if original_deadline is not None else int(now) + self.deadline_seconds)
            if minimum_floor is not None and int(swap.minimum_amount_out_raw) < int(minimum_floor):
                raise ValueError("alternative route minimum below original protected minimum")
            # The built transaction's own output is the figure its on-chain minimum
            # protects, so risk is assessed on that fresher figure; the route quote
            # keeps supplying the small reference quote for price-impact estimation.
            quote = replace(quote, amount_out_raw=swap.amount_out_raw,
                            gas_estimate_raw=str(swap.gas_estimate))
        now = time.time() if fixed_now is None else fixed_now
        accepted, reason, risk = assess_quote(
            signal, quote, reference, self.quote_policy, gas_price_raw, now)
        if not accepted:
            raise ValueError(f"execution requote rejected: {reason}")
        gas_price = int(gas_price_raw)
        max_fee = (gas_price * (10000 + self.fee_headroom_bps) + 9999) // 10000
        simulation = {}
        if swap is not None:
            gas_limit = max(self.gas_limits.get(signal.protocol, 0),
                            swap.gas_estimate * 13 // 10 + 50_000)
            if early_intent is not None:
                plan = build_early_aggregator_execution_plan(
                    early_intent, follower, relationship, proposal_id, quote,
                    risk["minimum_amount_out_raw"], swap, gas_limit, str(max_fee), "0",
                    self.allowed_protocols, self.allowed_assets, now)
            else:
                plan = build_aggregator_execution_plan(
                    signal, follower, relationship, proposal_id, quote,
                    risk["minimum_amount_out_raw"], swap, gas_limit, str(max_fee), "0",
                    self.allowed_protocols, self.allowed_assets, self.allowed_routes)
            def validate_retry():
                checked_at = time.time() if fixed_now is None else fixed_now
                check_early_execution_source(self.store, early_intent, signal, proposal, checked_at)
                ok, why, _ = assess_quote(
                    signal, quote, reference, self.quote_policy, gas_price_raw, checked_at)
                if not ok:
                    raise ValueError(f"execution quote invalid during gas retry: {why}")

            plan, simulation = await self._simulate_with_gas_retry(
                plan, validate_retry, allow_retry=retry_evidence is None)
            simulation["aggregator"] = swap.public_evidence()
        else:
            plan = build_execution_plan(
                signal, follower, relationship, proposal_id, quote,
                risk["minimum_amount_out_raw"], int(now) + self.deadline_seconds,
                self.gas_limits.get(signal.protocol, 0), str(max_fee), "0",
                self.allowed_protocols, self.allowed_assets, self.allowed_routes,
            )
        preflight = await ReadOnlyExecutionPreflight(
            self.rpc, EXECUTION_TARGETS,
            self.quote_policy.max_gas_cost_wei,
            max_quote_age_seconds=self.quote_policy.max_age_seconds).check(plan, now)
        preflight.update(simulation)
        if retry_evidence is not None:
            preflight["route_retry"] = {**retry_evidence, "status": "simulation_passed"}
        completed_at = time.time() if fixed_now is None else fixed_now
        check_early_execution_source(self.store, early_intent, signal, proposal, completed_at)
        accepted, reason, _ = assess_quote(
            signal, quote, reference, self.quote_policy, gas_price_raw, completed_at)
        if not accepted:
            raise ValueError(f"execution quote expired or invalid after preflight: {reason}")
        preflight["checked_at"] = completed_at
        reservation_id = hashlib.sha256(
            f"nonce:{proposal_id}:{follower}:{relationship}".encode()).hexdigest()
        nonce, status = self.store.reserve_execution_nonce(
            reservation_id, follower, relationship, proposal_id,
            plan.chain_id, preflight["pending_nonce"])
        if status not in {"reserved", "signed"}:
            raise ValueError("nonce reservation is not reusable")
        transaction = {
            "chainId": plan.chain_id, "nonce": nonce, "to": to_checksum_address(plan.to),
            "value": int(plan.value_raw), "data": plan.data, "gas": plan.gas_limit,
            "maxFeePerGas": int(plan.max_fee_per_gas),
            "maxPriorityFeePerGas": int(plan.max_priority_fee_per_gas), "type": 2,
        }
        plan_id = hashlib.sha256(
            f"plan:{proposal_id}:{self.config_snapshot_hash}".encode()).hexdigest()
        record = {
            "plan_id": plan_id, "proposal_id": proposal_id,
            "follower_wallet": follower, "relationship_id": relationship,
            "config_snapshot_hash": self.config_snapshot_hash,
            "nonce_reservation_id": reservation_id, "transaction": transaction,
            "unsigned_plan": asdict(plan),
        }
        inserted = self.store.record_execution_plan(record, {
            **preflight, "requote": quote.to_dict(), "risk": risk,
        })
        persisted = self.store.execution_plan(proposal_id)
        return self._result(persisted, not inserted)


class OfflineExecutionSigner:
    """Revalidates a prepared plan and signs only in explicit offline_test mode."""

    def __init__(self, store, quoter, rpc, quote_policy, config_snapshot_hash: str,
                 signer_factory=OfflineDatabaseSigner, relationship_gate=None):
        self.store, self.quoter, self.rpc = store, quoter, rpc
        self.quote_policy = quote_policy
        self.config_snapshot_hash = config_snapshot_hash
        self.signer_factory = signer_factory
        self.relationship_gate = relationship_gate

    async def sign(self, signal, proposal_id: str,
                   now: float | None = None, *, early_intent=None) -> OfflineSignedExecution:
        return await self._sign(signal, proposal_id, now, recovering=False, early_intent=early_intent)

    async def recover_signed(self, signal, proposal_id: str,
                             now: float | None = None) -> OfflineSignedExecution:
        """Recreate lost in-memory bytes only for an unobserved signed attempt."""
        return await self._sign(signal, proposal_id, now, recovering=True)

    async def _sign(self, signal, proposal_id: str, now: float | None,
                    recovering: bool, early_intent=None) -> OfflineSignedExecution:
        fixed_now = now
        row = self.store.execution_plan(proposal_id)
        expected_status = "signed" if recovering else "prepared"
        if (row is None or row["status"] != expected_status
                or row["config_snapshot_hash"] != self.config_snapshot_hash
                or row.get("unsigned_plan") is None):
            raise ValueError(
                f"{expected_status} execution plan is unavailable or stale")
        self._authorize(row)
        proposal = self.store.paper_proposal(proposal_id)
        if (proposal is None or proposal["status"] != "reserved"
                or proposal["source_event_id"] != signal.event_id
                or signal.canonical_status == "orphaned"):
            raise ValueError("source proposal or signal is no longer eligible")
        check_early_execution_source(self.store, early_intent, signal, proposal, now)
        if self.relationship_gate is not None:
            self.relationship_gate.validate(
                row["relationship_id"], row["follower_wallet"], signal.wallet,
                row["config_snapshot_hash"])
        budget_evidence = self.store.execution_budget_evidence(proposal_id)
        if (budget_evidence.get("follower_wallet") != row["follower_wallet"]
                or budget_evidence.get("relationship_id") != row["relationship_id"]):
            raise ValueError("execution budget attribution does not match plan")
        reservation = self.store.execution_nonce_reservation(proposal_id)
        expected_nonce_status = "signed" if recovering else "reserved"
        if (reservation is None or reservation["status"] != expected_nonce_status
                or reservation["reservation_id"] != row["nonce_reservation_id"]
                or reservation["nonce"] != row["transaction"]["nonce"]):
            raise ValueError("nonce reservation is unavailable or stale")
        if recovering:
            attempts = self.store.execution_attempts(row["plan_id"])
            if (len(attempts) != 1 or attempts[0]["tx_hash"] != row["signed_tx_hash"]
                    or attempts[0]["status"] != "signed"):
                raise ValueError("signed transaction is already observed or not recoverable")
        quote, reference, gas_price = await self.quoter.quote_with_reference(
            signal, proposal["amount_in_raw"])
        now = time.time() if now is None else now
        accepted, reason, risk = assess_quote(
            signal, quote, reference, self.quote_policy, gas_price, now)
        original = UnsignedExecutionPlan(**row["unsigned_plan"])
        validate_transaction_matches_plan(row["transaction"], original)
        if (not accepted or int(quote.amount_out_raw) < int(original.minimum_amount_out_raw)):
            raise ValueError(reason or "refreshed quote is below transaction minimum")
        refreshed = replace(
            original, quote_observed_at=quote.observed_at,
            quote_block_number=quote.block_number, quote_block_hash=quote.block_hash)
        preflight = await ReadOnlyExecutionPreflight(
            self.rpc, frozenset({original.to}),
            self.quote_policy.max_gas_cost_wei,
            max_quote_age_seconds=self.quote_policy.max_age_seconds).check(refreshed, now)
        if preflight["pending_nonce"] > reservation["nonce"]:
            raise ValueError("reserved nonce is behind current pending nonce")
        if original.execution_provider in AGGREGATOR_PROVIDERS:
            preflight.update(await simulate_aggregator_execution(self.rpc, original))
        now = time.time() if fixed_now is None else fixed_now
        accepted, reason, risk = assess_quote(
            signal, quote, reference, self.quote_policy, gas_price, now)
        if not accepted:
            raise ValueError(f"quote invalid immediately before signing: {reason}")
        signer = self._signer(row)
        check_early_execution_source(self.store, early_intent, signal, proposal, now)
        if "early_trial_id" in proposal["attribution"]:
            self.store.check_early_trial_proposal(proposal_id)
        raw = signer.sign_transaction(row["transaction"])
        if Account.recover_transaction(raw).lower() != row["follower_wallet"]:
            raise ValueError("offline signature sender mismatch")
        tx_hash = "0x" + keccak(raw).hex()
        final_review = {
            "checked_at": now, "read_only": True,
            "relationship_revalidated": self.relationship_gate is not None,
            "config_snapshot_hash": row["config_snapshot_hash"],
            "quote": quote.to_dict(), "reference_quote": reference.to_dict(),
            "risk": risk, "preflight": preflight,
            "budget": budget_evidence,
            "transaction_fields_verified": True,
            "sender_recovered": row["follower_wallet"],
        }
        if recovering:
            if tx_hash != row["signed_tx_hash"]:
                raise ValueError("recovered signed transaction hash mismatch")
        elif not self.store.mark_execution_plan_signed(
                row["plan_id"], row["nonce_reservation_id"], tx_hash, final_review):
            raise ValueError("execution plan signing state changed concurrently")
        return OfflineSignedExecution(
            row["plan_id"], proposal_id, tx_hash, raw,
            {**preflight, "requote": quote.to_dict(), "risk": risk})

    def _authorize(self, row: dict) -> None:
        require_offline_signing_enabled()

    def _signer(self, row: dict):
        return self.signer_factory(row["follower_wallet"])


class LiveExecutionSigner(OfflineExecutionSigner):
    """Sign one approved mainnet plan; raw bytes remain memory-only."""

    def __init__(self, store, quoter, rpc, quote_policy, config_snapshot_hash: str,
                 signer_factory=LiveDatabaseSigner, relationship_gate=None):
        super().__init__(store, quoter, rpc, quote_policy, config_snapshot_hash,
                         signer_factory, relationship_gate)

    def _authorize(self, row: dict) -> None:
        require_mainnet_signing_enabled(
            row["follower_wallet"], row["relationship_id"],
            row["config_snapshot_hash"])

    def _signer(self, row: dict):
        return self.signer_factory(
            row["follower_wallet"], row["relationship_id"],
            row["config_snapshot_hash"])


class ReadOnlyPreBroadcastReviewer:
    """Last read-only review of signed bytes; deliberately cannot broadcast."""

    def __init__(self, store, quoter, rpc, quote_policy, relationship_gate):
        if relationship_gate is None:
            raise ValueError("pre-broadcast review requires a fresh relationship gate")
        self.store, self.quoter, self.rpc = store, quoter, rpc
        self.quote_policy = quote_policy
        self.relationship_gate = relationship_gate

    async def review(self, signal, proposal_id: str, raw_transaction: bytes,
                     now: float | None = None, *, early_intent=None) -> ReadOnlyBroadcastReview:
        fixed_now = now
        if (not isinstance(raw_transaction, bytes) or not raw_transaction
                or len(raw_transaction) > 1024 * 1024):
            raise ValueError("invalid signed transaction bytes")
        row = self.store.execution_plan(proposal_id)
        proposal = self.store.paper_proposal(proposal_id)
        if (row is None or row["status"] != "signed" or not row["final_review"]
                or proposal is None or proposal["status"] != "reserved"
                or proposal["source_event_id"] != signal.event_id
                or signal.canonical_status == "orphaned"):
            raise ValueError("signed execution is unavailable or stale")
        check_early_execution_source(self.store, early_intent, signal, proposal, now)
        self._authorize(row)
        tx_hash = "0x" + keccak(raw_transaction).hex()
        if tx_hash != row["signed_tx_hash"]:
            raise ValueError("signed transaction hash does not match execution plan")
        if Account.recover_transaction(raw_transaction).lower() != row["follower_wallet"]:
            raise ValueError("signed transaction sender mismatch")
        try:
            decoded = TypedTransaction.from_bytes(HexBytes(raw_transaction)).as_dict()
            signable = {
                "chainId": decoded["chainId"], "nonce": decoded["nonce"],
                "to": "0x" + bytes(decoded["to"]).hex(), "value": decoded["value"],
                "data": "0x" + bytes(decoded["data"]).hex(), "gas": decoded["gas"],
                "maxFeePerGas": decoded["maxFeePerGas"],
                "maxPriorityFeePerGas": decoded["maxPriorityFeePerGas"],
                "type": decoded["type"],
            }
        except Exception:
            raise ValueError("signed transaction cannot be decoded") from None
        original = UnsignedExecutionPlan(**row["unsigned_plan"])
        validate_transaction_matches_plan(signable, original)
        self.relationship_gate.validate(
            row["relationship_id"], row["follower_wallet"], signal.wallet,
            row["config_snapshot_hash"])
        budget = self.store.execution_budget_evidence(proposal_id)
        if (budget.get("follower_wallet") != row["follower_wallet"]
                or budget.get("relationship_id") != row["relationship_id"]):
            raise ValueError("execution budget attribution does not match plan")
        quote, reference, gas_price = await self.quoter.quote_with_reference(
            signal, proposal["amount_in_raw"])
        now = time.time() if now is None else now
        accepted, reason, risk = assess_quote(
            signal, quote, reference, self.quote_policy, gas_price, now)
        if not accepted or int(quote.amount_out_raw) < int(original.minimum_amount_out_raw):
            raise ValueError(reason or "pre-broadcast quote is below transaction minimum")
        refreshed = replace(
            original, quote_observed_at=quote.observed_at,
            quote_block_number=quote.block_number, quote_block_hash=quote.block_hash)
        preflight = await ReadOnlyExecutionPreflight(
            self.rpc, frozenset({original.to}),
            self.quote_policy.max_gas_cost_wei,
            max_quote_age_seconds=self.quote_policy.max_age_seconds).check(refreshed, now)
        if preflight["pending_nonce"] != signable["nonce"]:
            raise ValueError("network pending nonce does not exactly match signed transaction")
        if original.execution_provider in AGGREGATOR_PROVIDERS:
            preflight.update(await simulate_aggregator_execution(self.rpc, original))
        evidence = {
            "read_only": True, "broadcast_performed": False,
            "quote_max_age_seconds": self.quote_policy.max_age_seconds,
            "relationship_revalidated": True, "transaction_hash_verified": True,
            "sender_recovered": row["follower_wallet"], "budget": budget,
            "quote": quote.to_dict(), "reference_quote": reference.to_dict(),
            "risk": risk, "preflight": preflight,
        }
        now = time.time() if fixed_now is None else fixed_now
        accepted, reason, risk = assess_quote(
            signal, quote, reference, self.quote_policy, gas_price, now)
        if not accepted:
            raise ValueError(f"quote invalid after broadcast preflight: {reason}")
        evidence["risk"] = risk
        check_early_execution_source(self.store, early_intent, signal, proposal, now)
        if "early_trial_id" in proposal["attribution"]:
            trial = self.store.check_early_trial_proposal(proposal_id)
            evidence["early_trial_id"] = trial["trial_id"]
            evidence["early_trial_expires_at"] = trial["expires_at"]
        return ReadOnlyBroadcastReview(proposal_id, tx_hash, now, evidence)

    def _authorize(self, row: dict) -> None:
        require_offline_signing_enabled()


class LivePreBroadcastReviewer(ReadOnlyPreBroadcastReviewer):
    """Run the same final review under the exact mainnet relationship gate."""

    def _authorize(self, row: dict) -> None:
        require_mainnet_signing_enabled(
            row["follower_wallet"], row["relationship_id"],
            row["config_snapshot_hash"])
