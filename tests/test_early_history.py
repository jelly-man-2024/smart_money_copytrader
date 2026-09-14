"""Historical reconstruction tests; synthetic context, no live dependencies."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest

from smart_money.early_history import (audit_archives, compact_history, read_context, read_log,
                                       reconstruct_case, reconstruct_cases)
from smart_money.early_replay import replay_cases

ROOT = Path(__file__).resolve().parents[1]


def fixture(side='BUY'):
    cases = json.loads((ROOT/'data/early_feed_public_samples_2026-09-14.json').read_text())['cases']
    c = deepcopy(next(c for c in cases if c['expected_side'] == side))
    c.update(record_id='proposal', relationship_id='r1', snapshots={})
    at = c['transaction']['received_at']
    binding = {'relationship_id': 'r1', 'follower_wallet': '0x'+'11'*20,
               'config_snapshot_hash': 'ab'*32}
    attr = {**binding, 'smart_wallet': c['wallet'], 'source_tx_hash': c['transaction']['hash']}
    quote = {'quote': {'observed_at': at+5, 'input_asset': '0x'+'22'*20,
                       'output_asset': '0x'+'33'*20, 'amount_in_raw': '1', 'amount_out_raw': '2',
                       'protocol': 'kyber', 'source': 'synthetic', 'block_number': 123,
                       'block_hash': '0x'+'55'*32, 'gas_estimate_raw': None}, 'gas_price_wei': '10'}
    quote['reference_quote'] = {**quote['quote'], 'observed_at': at+5.5}
    tables = {k: [] for k in ('candidates', 'signals', 'proposals', 'decisions', 'plans',
                              'solver_evidence', 'fills', 'reserved_lots')}
    tables['proposals'] = [{'proposal_id': 'proposal', 'source_tx_hash': attr['source_tx_hash'],
                            'attribution_payload': attr, 'quote_payload': quote, 'created_at': at+6}]
    context = {'captured_at_utc': 'synthetic', 'tables': tables}
    log = {'path': 'synthetic.log', 'rows': [], 'sha256': 'ab'*32, 'prefix_bytes': 0}
    return c, context, log, binding, at


class HistoryTests(unittest.TestCase):
    def test_saved_late_market_recovered_without_early_pass(self):
        c, ctx, log, _, at = fixture()
        restored, audit = reconstruct_case(c, ctx, log)
        self.assertEqual(restored['snapshots']['market']['observed_at'], at+6)
        self.assertEqual(audit['earliest_recorded_quote_delay_ms'], 5000)
        self.assertEqual(audit['snapshot_inventory']['market'], 'complete_but_after_feed_or_undated')
        self.assertEqual(audit['snapshot_inventory']['policy'], 'partial_only')
        self.assertFalse(replay_cases([restored], True)['rows'][0]['results'][0]['decision_passed'])
        self.assertEqual(c['snapshots'], {})

    def test_recorded_early_market_keeps_its_time_and_latest_available(self):
        c, ctx, log, binding, at = fixture()
        p = ctx['tables']['proposals'][0]
        p['quote_payload']['quote']['observed_at'] = at-1
        p['quote_payload']['reference_quote']['observed_at'] = at-.8
        p['created_at'] = at-.5
        restored, audit = reconstruct_case(c, ctx, log)
        self.assertEqual(restored['snapshots']['market']['observed_at'], at-.5)
        self.assertEqual(audit['snapshot_inventory']['market'], 'complete_recorded_by_feed')

    def test_quote_time_does_not_backdate_gas_availability(self):
        c, ctx, log, _, at = fixture()
        q = ctx['tables']['proposals'][0]['quote_payload']
        q['quote']['observed_at'] = q['reference_quote']['observed_at'] = at-1
        restored, audit = reconstruct_case(c, ctx, log)
        self.assertGreater(restored['snapshots']['market']['observed_at'], at)
        self.assertEqual(audit['snapshot_inventory']['market'], 'complete_but_after_feed_or_undated')

    def test_future_quote_after_persistence_is_not_recovered(self):
        c, ctx, log, _, at = fixture()
        ctx['tables']['proposals'][0]['quote_payload']['quote']['observed_at'] = at+100
        restored, audit = reconstruct_case(c, ctx, log)
        self.assertNotIn('market', restored['snapshots'])
        self.assertEqual(audit['snapshot_inventory']['market'], 'partial_only')

    def test_current_signal_and_final_summary_never_manufacture_order(self):
        c, ctx, log, _, at = fixture()
        signal = {'tx_hash': c['transaction']['hash'], 'wallet': c['wallet'],
                  'stage': 'relay_buy_evidenced', 'evidence': {'relay_order_id': '0x'+'12'*32}}
        ctx['tables']['signals'] = [{'event_id': 'final', 'tx_hash': signal['tx_hash'],
                                     'updated_at': at-100, 'payload': signal}]
        restored, audit = reconstruct_case(c, ctx, log)
        self.assertNotIn('order', restored['snapshots'])
        self.assertEqual(audit['snapshot_inventory']['order'], 'partial_only')
        self.assertIsNone(next(e for e in audit['evidence'] if e['kind']=='order')['available_by'])
        self.assertIsNone(audit['strict_signal_available_by'])

    def test_cross_relationship_decision_is_not_reused(self):
        c, ctx, log, binding, at = fixture()
        s = {'tx_hash': c['transaction']['hash'], 'wallet': c['wallet'], 'stage': 'relay_buy_evidenced'}
        ctx['tables']['decisions'] = [{'decision_id':'wrong', 'payload': {**binding,
            'relationship_id': 'other', 'source_signal': s}, 'created_at': at-1,
            'trigger_mode': 'feed_intent', 'accepted': True, 'reason': None}]
        _, audit = reconstruct_case(c, ctx, log)
        self.assertEqual(audit['original_decisions'], [])

    def test_proposal_or_plan_mismatch_fails(self):
        c, ctx, log, binding, at = fixture()
        ctx['tables']['proposals'][0]['attribution_payload']['smart_wallet'] = 'wrong'
        with self.assertRaisesRegex(ValueError, 'identity_mismatch'): reconstruct_case(c, ctx, log)
        c, ctx, log, binding, at = fixture()
        ctx['tables']['plans'] = [{**binding, 'proposal_id':'proposal', 'relationship_id':'other'}]
        with self.assertRaisesRegex(ValueError, 'binding_mismatch'): reconstruct_case(c, ctx, log)

    def test_stale_original_sell_log_overrides_enriched_flag(self):
        c, ctx, log, binding, at = fixture('SELL')
        c['transaction']['fresh'] = True
        h,w = c['transaction']['hash'], c['wallet']
        log['rows'] = [{'line':10, 'tx_hash':h,'wallet':w,'payload':{
            'tx_hash':h,'wallet':w,'stage':'intent','behavior':'SELL','fresh':False}},
            {'line':20,'tx_hash':h,'wallet':w,'payload':{'stage':'relay_sell_evidenced'}}]
        restored, audit = reconstruct_case(c, ctx, log)
        self.assertFalse(restored['transaction']['fresh'])
        self.assertTrue(audit['original_intent_precedes_strict_log'])
        self.assertFalse(audit['parse_supported_and_fresh'])
        self.assertIsNone(audit['strict_signal_available_by'])

    def test_reserved_lot_current_contents_not_backdated(self):
        c, ctx, log, binding, at = fixture('SELL')
        ctx['tables']['reserved_lots'] = [{'proposal_id':'proposal','lot_id':'lot',
            'created_at':at-1000,'updated_at':at+100,'token_remaining_raw':'0'}]
        restored, audit = reconstruct_case(c, ctx, log)
        self.assertNotIn('portfolio', restored['snapshots'])
        e = next(e for e in audit['evidence'] if e['kind']=='portfolio')
        self.assertIsNone(e['available_by'])

    def test_existing_snapshots_preserved_and_summary_counts_match(self):
        c, ctx, log, _, at = fixture()
        c['snapshots']['market'] = {'payload':{},'observed_at':at-1,'provenance':'explicit fixture'}
        restored, audit = reconstruct_cases([c], ctx, log)
        self.assertEqual(restored[0]['snapshots'], c['snapshots'])
        compact = compact_history(audit)
        self.assertEqual(compact['groups'], audit['groups'])
        self.assertNotIn('evidence', compact['rows'][0])
        self.assertIn('evidence', audit['rows'][0])

    def test_bounded_read_only_query_and_timezone(self):
        c, _, _, _, _ = fixture()
        class Cursor:
            queries=[]
            offset=0
            huge=False
            def execute(self, sql, params=None): self.queries.append(sql)
            def fetchone(self):
                t=datetime(2026,9,14)
                return {'utc_now':t,'session_now':t+timedelta(hours=self.offset)}
            def fetchall(self): return [{}]*10001 if self.huge else []
        q=Cursor()
        result=read_context(q,[c])
        self.assertEqual(len(result['tables']),8)
        self.assertTrue(all(s.startswith('SELECT ') for s in q.queries))
        self.assertFalse(any('wallet_keys' in s or 'FOR UPDATE' in s for s in q.queries))
        q.offset=8
        with self.assertRaisesRegex(ValueError,'not_utc'):read_context(q,[c])
        q.offset=0;q.huge=True
        with self.assertRaisesRegex(ValueError,'truncated'):read_context(q,[c])

    def test_log_reader_keeps_scope_and_no_invented_time(self):
        c, _, _, _, _ = fixture()
        r={'tx_hash':c['transaction']['hash'],'wallet':c['wallet'],'stage':'intent','behavior':'BUY'}
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'log.jsonl'
            p.write_text('bad\n'+json.dumps(r)+'\n'+json.dumps({**r,'wallet':'other'})+'\n')
            log=read_log(p,[c])
        self.assertEqual(len(log['rows']),1)
        self.assertEqual(log['malformed_lines'],1)
        self.assertEqual(log['rows'][0]['line'],2)

    def test_archive_match_is_not_early_snapshot(self):
        c, _, _, _, _ = fixture()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'public.json'
            p.write_text(json.dumps({'requests':[{'hash':c['transaction']['hash']}]}))
            a=audit_archives([p],[c])[0]
        self.assertEqual(a['cohort_tx_matches'],[c['transaction']['hash']])
        self.assertFalse(a['early_snapshots_created'])


if __name__ == '__main__': unittest.main()
