"""Explicit trial handoff using the existing ledger and execution pipeline.

No keys, signing, broadcasts or Feed connections in this module. The monitor
supplies its executor only when an existing operator-started trial is selected.
"""
from dataclasses import asdict
import asyncio
import hashlib
import time

from .early_decision import EarlyDecisionEngine
from .paper import budget_bucket
from .verified_feed_intent import VerifiedFeedIntent
from .execution_pipeline import check_early_execution_source
from .runtime_safety import trip_execution_stop
from .execution_controls import _stop_controls
from .early_timing import EARLY_FEED_MAX_AGE_SECONDS
from .models import Signal
from . import registry as R


class EarlyRuntime:
    def __init__(self, store, quoter, gate, policies, trial_id, execute, healthy, report,
                 *, deployment_monitor=None):
        self.store, self.quoter, self.gate = store, quoter, gate
        self.policies, self.trial_id, self.execute = tuple(policies), trial_id, execute
        self.healthy, self.report = healthy, report
        self.deployment_monitor = deployment_monitor

    async def prefetch(self, candidate):
        """Bounded read-only hints. No order identity, budget or execution permission."""
        if candidate.side != "BUY" or candidate.token_in != R.USDG or not self.healthy():
            return
        for policy in self.policies:
            rule = policy.buy_rules.get("USDG")
            if (policy.wallet != candidate.wallet or rule is None or rule.mode != "fixed"
                    or policy.execution_providers != ("kyber",)
                    or R.USDG not in policy.allowed_assets
                    or "relay_solver" not in policy.allowed_protocols):
                continue
            amount = int(rule.fixed_amount_raw)
            if amount < 2 or amount > int(policy.budget_limits["USDG"]):
                continue
            signal = Signal(candidate.tx_hash, candidate.wallet, "third_party", "BUY",
                candidate.path, None, "", token_in=candidate.token_in,
                token_out=candidate.token_out, protocol="kyber")
            started = time.time()
            with self.quoter.execution_context(candidate.operation_key, policy.follower_wallet,
                    policy.snapshot_hash, policy.quote_policy.max_age_seconds) as context:
                results = await asyncio.gather(
                    self.quoter._request_route(signal, str(amount)),
                    self.quoter._request_route(signal, str(max(1, amount // 100))),
                    return_exceptions=True)
                self.report("early_quote_prefetch", source_tx_hash=candidate.tx_hash,
                    relationship_id=policy.relationship_id,
                    elapsed_ms=round((time.time() - started) * 1000, 3),
                    success=not any(isinstance(r, BaseException) for r in results),
                    route_requests=context["route_requests"],
                    shared_route_reuses=context.get("shared_route_reuses", 0))

    def snapshots(self, intent, policy):
        started = time.time()
        _stop_controls()
        self.gate.validate(policy.relationship_id, policy.follower_wallet,
                           policy.wallet, policy.snapshot_hash)
        c = intent.candidate
        bucket = budget_bucket(c.token_in if c.side == "BUY" else c.token_out)
        binding = dict(relationship_id=policy.relationship_id, follower=policy.follower_wallet,
                       smart_wallet=policy.wallet, config_snapshot_hash=policy.snapshot_hash)
        lots = []
        for lot_id in self.store.open_paper_position_ids():
            lot = self.store.paper_position(lot_id)
            if lot["wallet"] != policy.ledger_scope or lot["token"] != c.token_in:
                continue
            attr = lot["attribution"]
            reserved = self.store.connection.execute("""SELECT token_amount_raw
                FROM paper_position_reservations WHERE lot_id=? AND status='active'""", (lot_id,)).fetchall()
            source = self.store.signal(attr.get("source_position_evidence_event_id",
                                                attr.get("source_event_id", "")))
            basis = attr.get("source_position_remaining_raw")
            if basis is None and lot["token_initial_raw"] == lot["token_remaining_raw"]:
                basis = attr.get("source_amount_out_raw")
            # Legacy lots need current strict evidence, not a missing-status default.
            status = attr.get("source_position_status", "confirmed" if source and
                source.execution_success is True and source.stage in {
                    "swap_evidenced", "relay_buy_evidenced"} else "pending")
            if source and source.canonical_status == "orphaned":
                status = "source_orphaned"
            lots.append(dict(lot_id=lot_id, created_at=len(lots), token=lot["token"],
                relationship_id=attr.get("relationship_id"), principal_asset=lot["principal_asset"],
                token_remaining_raw=lot["token_remaining_raw"], source_remaining_raw=basis,
                reserved_raw=str(sum(int(r[0]) for r in reserved)), source_position_status=status))
        budget = self.store.paper_budget(policy.ledger_scope, bucket)
        if budget is None or bucket not in policy.buy_rules:
            raise ValueError("early budget or buy rule missing")
        state = dict(binding, observed_at=started, lots=lots,
                     source_orphaned=False, consumed_operation_keys=[],
                     budget_available_raw=str(int(budget["limit_raw"]) - int(budget["invested_raw"])
                                              - int(budget["reserved_raw"])))
        config = dict(binding, observed_at=started, enabled=True, stop_active=False,
            buy_rule=asdict(policy.buy_rules[bucket]), sell_rule=asdict(policy.sell_rule),
            max_input_raw=(policy.budget_limits[bucket] if c.side == "BUY" else
                           str(sum(int(l["token_remaining_raw"]) for l in lots))),
            allowed_protocols=sorted(policy.allowed_protocols), allowed_assets=sorted(policy.allowed_assets),
            execution_providers=list(policy.execution_providers), quote_policy=asdict(policy.quote_policy))
        return config, state

    async def __call__(self, tx, evidence):
        for item in evidence.get("candidates", []):
            if not item.get("recognized_intent"):
                continue
            candidate = item["candidate"]
            for policy in self.policies:
                if policy.wallet != candidate["wallet"]:
                    continue
                proposal_id = None
                reserved_here = False
                context = None
                try:
                    if not self.healthy():
                        raise ValueError("early Feed unhealthy")
                    intent = VerifiedFeedIntent.verify(tx, policy.wallet, candidate["path"],
                        item["snapshots"], time.time(), deployment_monitor=self.deployment_monitor)
                    trial = self.store.early_trial_status(self.trial_id)
                    if not trial or not trial["eligible"]:
                        raise ValueError("early trial inactive")
                    with self.quoter.execution_context(intent.candidate.operation_key,
                            policy.follower_wallet, policy.snapshot_hash,
                            policy.quote_policy.max_age_seconds) as context:
                        config, portfolio = self.snapshots(intent, policy)
                        decision = await EarlyDecisionEngine(self.quoter).evaluate(intent, config, portfolio)
                        signal = intent.quote_signal(time.time())
                        proposal_id = hashlib.sha256((self.trial_id + ":" +
                            decision["relationship_key"]).encode()).hexdigest()
                        attr = dict(follower_wallet=policy.follower_wallet,
                            relationship_id=policy.relationship_id, smart_wallet=policy.wallet,
                            config_snapshot_hash=policy.snapshot_hash, source_event_id=signal.event_id,
                            source_tx_hash=signal.tx_hash, source_behavior=signal.behavior,
                            source_stage="intent", source_amount_in_raw=signal.amount_in_raw,
                            source_amount_out_raw=None, source_position_status="pending",
                            copy_operation_order_id=intent.candidate.order_id, early_trial_id=self.trial_id,
                            early_received_at=tx.received_at, early_checked_at=decision["checked_at"],
                            early_feed_timestamp=tx.timestamp,
                            early_feed_max_age_seconds=EARLY_FEED_MAX_AGE_SECONDS,
                            early_deployment_verification=intent.deployment_evidence(),
                            early_decision_exceeds_3s=(decision["checked_at"] - tx.timestamp > 3
                                                      or decision["checked_at"] - tx.received_at > 3),
                            amount_basis=decision["amount_basis"], early_decision=decision)
                        proposal = dict(proposal_id=proposal_id,
                            source_event_id="early:" + decision["relationship_key"],
                            source_tx_hash=signal.tx_hash, wallet=policy.ledger_scope,
                            trigger_mode="feed_intent", strategy_version=policy.strategy_version,
                            input_asset=signal.token_in, output_asset=signal.token_out,
                            budget_bucket=budget_bucket(signal.token_in if signal.behavior == "BUY"
                                                        else signal.token_out),
                            amount_in_raw=decision["amount_in_raw"], attribution=attr, quote=decision["quote"])
                        check_early_execution_source(self.store, intent, signal, proposal)
                        if not self.healthy():
                            raise ValueError("early Feed unhealthy after decision")
                        self.store.put(signal)
                        reserve = (self.store.reserve_paper_proposal if signal.behavior == "BUY"
                                   else self.store.reserve_paper_sell)
                        ok, reason = reserve(proposal)
                        if not ok or reason == "proposal_already_exists":
                            raise ValueError(reason)
                        reserved_here = True
                        self.report("early_decision_reserved", proposal_id=proposal_id,
                                    relationship_id=policy.relationship_id, checked_at=time.time())
                        await self.execute(policy, signal, proposal_id, early_intent=intent)
                except Exception as exc:
                    if not isinstance(exc, (ValueError, PermissionError)):
                        trip_execution_stop()
                    if reserved_here and not self.store.execution_plan(proposal_id):
                        self.store.cancel_paper_proposal(proposal_id, "early_handoff_rejected")
                    self.report("early_handoff_rejected", proposal_id=proposal_id,
                                relationship_id=policy.relationship_id,
                                error_type=type(exc).__name__, reason=str(exc)[:160])
                finally:
                    if isinstance(context, dict):
                        self.report("early_quote_requests", proposal_id=proposal_id,
                            relationship_id=policy.relationship_id,
                            route_retry=context.get("route_retry"),
                            shared_route_reuses=context.get("shared_route_reuses", 0),
                            **{k: context.get(k, 0) for k in ("route_requests", "build_requests", "quote_reuses", "refreshes")})
