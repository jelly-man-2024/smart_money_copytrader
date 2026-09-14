"""Reconcile source evidence without changing the follower's real assets/budget."""
import json
from .registry import CHAIN_ID


class SourcePositionStore:
    def reconcile_early_source_positions(self, ledger_scope, signal):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            changed = self._reconcile_early_source_positions(ledger_scope, signal)
            self.connection.commit()
            return changed
        except Exception:
            self.connection.rollback()
            raise

    def _reconcile_early_source_positions(self, ledger_scope, signal):
        """Explicit strict-evidence handoff, no liquidation or budget release.

        Only pending early BUY lots with the SAME source order/wallet/tx may be
        resolved. Partial local exits require review rather than a guessed basis.
        """
        order = signal.evidence.get("relay_order_id", signal.evidence.get("relay_deposit_order_id"))
        if not order or signal.chain_id != CHAIN_ID or signal.canonical_status == "orphaned":
            return 0
        strict = (signal.stage in {"swap_evidenced", "relay_buy_evidenced"}
                  and signal.execution_status == "success" and signal.execution_success is True)
        failed = (signal.stage == "failed" and signal.execution_status == "reverted"
                  and signal.execution_success is False)
        if not strict and not failed:
            return 0
        rows = self.connection.execute("""SELECT lot_id,token,token_initial_raw,
            token_remaining_raw,attribution_payload FROM paper_positions WHERE wallet=?""",
            (ledger_scope,)).fetchall()
        changed = 0
        for lot_id, token, initial, remaining, payload in rows:
            attr = json.loads(payload)
            if (attr.get("source_position_status") != "pending"
                    or attr.get("source_tx_hash") != signal.tx_hash
                    or attr.get("smart_wallet") != signal.wallet
                    or attr.get("copy_operation_order_id") != order):
                continue
            actual = signal.evidence.get("actual_output_credit_raw")
            if failed:
                state = "source_failed"
            elif signal.behavior != "BUY" or signal.token_out != token:
                state = "source_mismatch"
            elif initial != remaining:
                state = "needs_review_after_local_exit"
            elif (not isinstance(actual, str) or not actual.isascii() or not actual.isdecimal()
                  or len(actual) > 78 or not 0 < int(actual) < 2 ** 256):
                continue
            else:
                state = "confirmed"
                attr["source_position_initial_raw"] = actual
                attr["source_position_remaining_raw"] = actual
            attr["source_position_status"] = state
            attr["source_position_evidence_event_id"] = signal.event_id
            self.connection.execute("UPDATE paper_positions SET attribution_payload=? WHERE lot_id=?",
                                    (json.dumps(attr, sort_keys=True), lot_id))
            changed += 1
        return changed

    def _reconcile_early_source_signal(self, signal):
        """Caller owns the signal/fill transaction; handle either arrival order."""
        scopes = self.connection.execute("""SELECT DISTINCT p.wallet
            FROM paper_positions p JOIN paper_fills f ON p.buy_fill_id=f.fill_id
            JOIN paper_orders o ON f.order_id=o.order_id
            JOIN paper_proposals q ON o.proposal_id=q.proposal_id
            WHERE q.source_tx_hash=?""", (signal.tx_hash,)).fetchall()
        for (scope,) in scopes:
            self._reconcile_early_source_positions(scope, signal)

    def _invalidate_early_source_tx(self, tx_hash):
        """Reorg invalidates basis, never erases owned assets or operation fences."""
        rows = self.connection.execute("""SELECT p.lot_id,p.attribution_payload
            FROM paper_positions p JOIN paper_fills f ON p.buy_fill_id=f.fill_id
            JOIN paper_orders o ON f.order_id=o.order_id
            JOIN paper_proposals q ON o.proposal_id=q.proposal_id
            WHERE q.source_tx_hash=?""", (tx_hash,)).fetchall()
        for lot_id, payload in rows:
            attr = json.loads(payload)
            if "source_position_status" not in attr:
                continue
            attr["source_position_status"] = "source_orphaned"
            attr.pop("source_position_initial_raw", None)
            attr.pop("source_position_remaining_raw", None)
            self.connection.execute("UPDATE paper_positions SET attribution_payload=? WHERE lot_id=?",
                                    (json.dumps(attr, sort_keys=True), lot_id))
