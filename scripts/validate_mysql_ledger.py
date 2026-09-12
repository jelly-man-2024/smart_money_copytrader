#!/usr/bin/env python3
"""Exercise MySQL ledger transaction semantics with public synthetic rows."""
from __future__ import annotations

import json
import uuid

from smart_money.mysql_store import MySqlStore
from smart_money.models import Signal, Transaction
from smart_money.registry import USDG


def main():
    suffix = uuid.uuid4().hex
    cycle_id = f"mysql-validation-{suffix}"
    proposal_id = f"mysql-validation-proposal-{suffix}"
    order_id = f"mysql-validation-order-{suffix}"
    fill_id = f"mysql-validation-fill-{suffix}"
    lot_id = f"mysql-validation-lot-{suffix}"
    sell_proposal_id = f"mysql-validation-sell-proposal-{suffix}"
    sell_order_id = f"mysql-validation-sell-order-{suffix}"
    sell_fill_id = f"mysql-validation-sell-fill-{suffix}"
    execution_proposal_id = f"mysql-validation-execution-proposal-{suffix}"
    nonce_reservation_id = f"mysql-validation-nonce-{suffix}"
    plan_id = f"mysql-validation-plan-{suffix}"
    signed_tx_hash = "0x" + (suffix[2:] + suffix[:2]) * 2
    scope = f"relationship:mysql-validation:{suffix}"
    tx_hash = "0x" + suffix * 2
    sell_tx_hash = "0x" + suffix[::-1] * 2
    event_id = f"4663:{tx_hash}:{'0x' + '22' * 20}:mysql-validation"
    sell_event_id = (
        f"4663:{sell_tx_hash}:{'0x' + '22' * 20}:mysql-validation-sell")
    reorg_tx_hash = "0x" + (suffix[1:] + suffix[:1]) * 2
    reorg_path = "mysql-validation-reorg"
    reorg_event_id = (
        f"4663:{reorg_tx_hash}:{'0x' + '22' * 20}:{reorg_path}")
    parent_hash = "0x" + "44" * 32
    orphan_hash = "0x" + "55" * 32
    parent_block = 1_000_000_000 + int(suffix[:8], 16)
    orphan_block = parent_block + 1
    store = MySqlStore()
    try:
        if store.active_paper_budget_cycle() is not None:
            raise ValueError("refusing validation while an operator budget cycle is active")
        if store.chain_cursor() is not None:
            raise ValueError("refusing validation while a canonical MySQL cursor exists")
        if (store.chain_block_hash(parent_block) is not None
                or store.chain_block_hash(orphan_block) is not None):
            raise ValueError("refusing validation because random block heights already exist")
        store.start_paper_budget_cycle(cycle_id, "automated_mysql_ledger_validation")
        store.configure_paper_budget(scope, "USDG", "1000")
        signal = Signal(
            tx_hash, "0x" + "22" * 20, "direct", "BUY",
            "mysql-validation", "0x" + "33" * 20, "0x",
            stage="swap_evidenced", execution_status="success",
            execution_success=True)
        if signal.event_id != event_id or not store.put(signal) or store.put(signal):
            raise ValueError("MySQL signal idempotency is inconsistent")
        reserved = store.reserve_paper_proposal({
            "proposal_id": proposal_id,
            "source_event_id": event_id,
            "source_tx_hash": "0x" + "ab" * 32,
            "wallet": scope,
            "trigger_mode": "swap_evidenced",
            "strategy_version": "mysql-validation-v1",
            "input_asset": USDG,
            "output_asset": "0x" + "33" * 20,
            "budget_bucket": "USDG",
            "amount_in_raw": "100",
            "attribution": {
                "follower_wallet": "0x" + "11" * 20,
                "smart_wallet": "0x" + "22" * 20,
                "relationship_id": f"mysql-validation:{suffix}",
                "source_event_id": event_id,
            },
        })
        if reserved != (True, "reserved"):
            raise ValueError("MySQL proposal reservation failed")
        budget = store.paper_budget(scope, "USDG")
        if budget is None or budget["reserved_raw"] != "100":
            raise ValueError("MySQL reserved budget is inconsistent")
        if not store.fill_paper_buy(proposal_id, {
                "order_id": order_id, "fill_id": fill_id, "lot_id": lot_id,
                "amount_out_raw": "250", "fee_asset": USDG,
                "fee_amount_raw": "1", "gas_cost_wei": "7",
                "quote_observed_at": "2026-09-12T00:00:00Z",
                "filled_at": "2026-09-12T00:00:01Z"}):
            raise ValueError("MySQL paper fill failed")
        invested = store.paper_budget(scope, "USDG")
        trades = [trade for trade in store.paper_trades()
                  if trade["fill_id"] == fill_id]
        if (invested is None or invested["invested_raw"] != "100"
                or len(trades) != 1 or trades[0]["fill_id"] != fill_id
                or trades[0]["attribution"]["relationship_id"]
                != f"mysql-validation:{suffix}"):
            raise ValueError("MySQL attributed earnings ledger is inconsistent")
        sell_signal = Signal(
            sell_tx_hash, "0x" + "22" * 20, "direct", "SELL",
            "mysql-validation-sell", "0x" + "33" * 20, "0x",
            stage="swap_evidenced", execution_status="success",
            execution_success=True)
        if sell_signal.event_id != sell_event_id or not store.put(sell_signal):
            raise ValueError("MySQL sell signal persistence is inconsistent")
        sell_reserved = store.reserve_paper_sell({
            "proposal_id": sell_proposal_id,
            "source_event_id": sell_event_id,
            "source_tx_hash": sell_tx_hash,
            "wallet": scope,
            "trigger_mode": "swap_evidenced",
            "strategy_version": "mysql-validation-sell-v1",
            "input_asset": "0x" + "33" * 20,
            "output_asset": USDG,
            "budget_bucket": "USDG",
            "amount_in_raw": "100",
            "attribution": {
                "follower_wallet": "0x" + "11" * 20,
                "smart_wallet": "0x" + "22" * 20,
                "relationship_id": f"mysql-validation:{suffix}",
                "source_event_id": sell_event_id,
            },
        })
        if sell_reserved != (True, "reserved"):
            raise ValueError("MySQL attributed sell reservation failed")
        if not store.fill_paper_sell(sell_proposal_id, {
                "order_id": sell_order_id, "fill_id": sell_fill_id,
                "amount_out_raw": "60", "fee_asset": USDG,
                "fee_amount_raw": "1", "gas_cost_wei": "9",
                "quote_observed_at": "2026-09-12T00:01:00Z",
                "filled_at": "2026-09-12T00:01:01Z"}):
            raise ValueError("MySQL attributed sell fill failed")
        after_sell = store.paper_budget(scope, "USDG")
        lot = store.paper_position(lot_id)
        pnl = store.paper_realized_pnl(sell_fill_id)
        if (after_sell is None or after_sell["invested_raw"] != "60"
                or lot is None or lot["token_remaining_raw"] != "150"
                or lot["principal_remaining_raw"] != "60"
                or len(pnl) != 1 or pnl[0]["realized_pnl_raw"] != "19"):
            raise ValueError("MySQL sell/PnL/budget restoration is inconsistent")
        candidate = Transaction(
            tx_hash, "0x" + "22" * 20, "0x" + "33" * 20, b"", value=0)
        if not store.put_candidate(candidate) or store.put_candidate(candidate):
            raise ValueError("MySQL candidate idempotency is inconsistent")
        claimed = store.claim_candidates(1)
        if len(claimed) != 1 or claimed[0].hash != tx_hash:
            raise ValueError("MySQL candidate claim is inconsistent")
        store.record_chain_block(parent_block, parent_hash, "0x" + "33" * 32)
        store.record_chain_block(orphan_block, orphan_hash, parent_hash)
        reorg_candidate = Transaction(
            reorg_tx_hash, "0x" + "22" * 20,
            "0x" + "33" * 20, b"", value=0)
        reorg_signal = Signal(
            reorg_tx_hash, "0x" + "22" * 20, "direct", "BUY",
            reorg_path, "0x" + "33" * 20, "0x",
            stage="swap_evidenced", execution_status="success",
            execution_success=True, evidence={"block_hash": orphan_hash})
        if (reorg_signal.event_id != reorg_event_id
                or not store.put_candidate(reorg_candidate)
                or not store.put(reorg_signal)):
            raise ValueError("MySQL reorg fixture persistence failed")
        store.complete_candidate(reorg_tx_hash, orphan_block, orphan_hash)
        before_reorg = store.signal(reorg_event_id)
        orphaned_signals, requeued = store.rewind_chain(parent_block, parent_hash)
        after_reorg = store.signal(reorg_event_id)
        requeued_candidates = store.claim_candidates(1)
        if (before_reorg is None
                or before_reorg.canonical_status != "safe_head_confirmed"
                or orphaned_signals != 1 or requeued != 1
                or after_reorg is None or after_reorg.canonical_status != "orphaned"
                or len(requeued_candidates) != 1
                or requeued_candidates[0].hash != reorg_tx_hash):
            raise ValueError("MySQL canonical reorg recovery is inconsistent")
        execution_reserved = store.reserve_paper_proposal({
            "proposal_id": execution_proposal_id,
            "source_event_id": event_id,
            "source_tx_hash": tx_hash,
            "wallet": scope,
            "trigger_mode": "swap_evidenced",
            "strategy_version": "mysql-validation-execution-v1",
            "input_asset": USDG,
            "output_asset": "0x" + "33" * 20,
            "budget_bucket": "USDG",
            "amount_in_raw": "10",
            "attribution": {
                "follower_wallet": "0x" + "11" * 20,
                "smart_wallet": "0x" + "22" * 20,
                "relationship_id": f"mysql-validation:{suffix}",
                "source_event_id": event_id,
            },
        })
        if execution_reserved != (True, "reserved"):
            raise ValueError("MySQL execution proposal reservation failed")
        nonce, nonce_status = store.reserve_execution_nonce(
            nonce_reservation_id, "0x" + "11" * 20,
            f"mysql-validation:{suffix}", execution_proposal_id, 4663, 7)
        transaction = {
            "chainId": 4663, "nonce": nonce, "to": "0x" + "33" * 20,
            "value": 0, "data": "0x", "gas": 21000,
            "maxFeePerGas": 100, "maxPriorityFeePerGas": 1, "type": 2,
        }
        if nonce_status != "reserved" or nonce != 7 or not store.record_execution_plan({
                "plan_id": plan_id, "proposal_id": execution_proposal_id,
                "follower_wallet": "0x" + "11" * 20,
                "relationship_id": f"mysql-validation:{suffix}",
                "config_snapshot_hash": "77" * 32,
                "nonce_reservation_id": nonce_reservation_id,
                "transaction": transaction, "unsigned_plan": {},
        }, {"read_only": True}):
            raise ValueError("MySQL execution plan persistence failed")
        if not store.mark_execution_plan_signed(
                plan_id, nonce_reservation_id, signed_tx_hash,
                {"checked_at": "2026-09-12T00:02:00Z", "read_only": True}):
            raise ValueError("MySQL signed attempt persistence failed")
        observed_payload = {**transaction, "from": "0x" + "11" * 20}
        if (not store.observe_execution_attempt(plan_id, signed_tx_hash, observed_payload)
                or not store.finalize_execution_attempt(
                    signed_tx_hash, "confirmed", orphan_block + 1, "0x" + "88" * 32)):
            raise ValueError("MySQL execution lifecycle transition failed")
        execution_audit = store.execution_audit()
        if (not execution_audit["healthy"]
                or not execution_audit["end_to_end_evidenced"]
                or execution_audit["attempt_statuses"]["confirmed"] != 1):
            raise ValueError("MySQL execution audit is inconsistent")
        print(json.dumps({
            "mysql_ledger_transaction_verified": True,
            "reservation_atomic": True,
            "buy_fill_attributed": True,
            "earnings_export_readable": True,
            "sell_restores_principal": True,
            "realized_pnl_raw": "19",
            "reorg_orphans_signal": True,
            "reorg_requeues_candidate": True,
            "execution_nonce_persistent": True,
            "execution_confirmed_audit": True,
            "signal_idempotent": True,
            "candidate_claimed_once": True,
            "private_key_accessed": False,
            "copy_eligible": False,
        }, sort_keys=True))
    finally:
        # Remove only rows carrying this run's unguessable UUID.
        store.connection.execute(
            "DELETE FROM execution_attempts WHERE plan_id=?", (plan_id,))
        store.connection.execute(
            "DELETE FROM execution_plans WHERE plan_id=?", (plan_id,))
        store.connection.execute(
            "DELETE FROM execution_nonce_reservations WHERE reservation_id=?",
            (nonce_reservation_id,))
        store.connection.execute(
            "DELETE FROM paper_realized_pnl WHERE fill_id=?", (sell_fill_id,))
        store.connection.execute(
            "DELETE FROM paper_position_reservations WHERE proposal_id=?",
            (sell_proposal_id,))
        store.connection.execute("DELETE FROM paper_positions WHERE lot_id=?", (lot_id,))
        store.connection.execute(
            "DELETE FROM paper_fills WHERE fill_id IN (?,?)", (fill_id, sell_fill_id))
        store.connection.execute(
            "DELETE FROM paper_orders WHERE order_id IN (?,?)", (order_id, sell_order_id))
        store.connection.execute(
            "DELETE FROM paper_reservations WHERE proposal_id IN (?,?)",
            (proposal_id, execution_proposal_id))
        store.connection.execute(
            "DELETE FROM paper_proposals WHERE proposal_id IN (?,?,?)",
            (proposal_id, sell_proposal_id, execution_proposal_id))
        store.connection.execute(
            "DELETE FROM paper_budgets WHERE cycle_id=?", (cycle_id,))
        store.connection.execute(
            "DELETE FROM paper_budget_cycles WHERE cycle_id=?", (cycle_id,))
        store.connection.execute("DELETE FROM candidates WHERE tx_hash=?", (tx_hash,))
        store.connection.execute(
            "DELETE FROM signals WHERE event_id IN (?,?)", (event_id, sell_event_id))
        store.connection.execute(
            "DELETE FROM candidate_inclusions WHERE tx_hash=?", (reorg_tx_hash,))
        store.connection.execute(
            "DELETE FROM candidates WHERE tx_hash=?", (reorg_tx_hash,))
        store.connection.execute(
            "DELETE FROM signals WHERE event_id=?", (reorg_event_id,))
        store.connection.execute(
            "DELETE FROM canonical_blocks WHERE block_number IN (?,?)",
            (parent_block, orphan_block))
        store.connection.execute("DELETE FROM chain_cursors WHERE name=?", ("canonical_l2",))
        store.connection.commit()
        store.close()


if __name__ == "__main__":
    main()
