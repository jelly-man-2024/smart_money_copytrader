"""Recover recorded historical evidence, without moving later facts into the past.

Only the caller's read-only business cursor is used. No runtime Store, RPC,
current relationship configuration, signer, or mutable balance reconstruction.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from statistics import median

from .early_intent import fingerprint, hash32, parse_candidates, timestamp, uint
from .early_replay import evaluate_candidate, transaction_from_record
from .models import address
from .quotes import Quote


def obj(value):
    return json.loads(value) if isinstance(value, str) else value


def epoch(value):
    if isinstance(value, datetime):
        return value.replace(tzinfo=timezone.utc).timestamp()
    return timestamp(value)


def read_context(cursor, cases):
    """Bounded SELECTs within the caller's START TRANSACTION READ ONLY."""
    hashes = sorted({c['transaction']['hash'] for c in cases if c.get('transaction')})
    if not hashes or len(hashes) > 1000:
        raise ValueError('history_cohort_size_invalid')
    cursor.execute('SELECT UTC_TIMESTAMP(6) AS utc_now,NOW(6) AS session_now')
    clock = cursor.fetchone()
    if abs((clock['utc_now'] - clock['session_now']).total_seconds()) > .01:
        raise ValueError('history_database_session_not_utc')
    slots = ','.join(['%s'] * len(hashes))
    queries = {
        'candidates': f'SELECT tx_hash,created_at,updated_at FROM candidates WHERE tx_hash IN ({slots})',
        'signals': f'SELECT event_id,tx_hash,payload,updated_at FROM signals WHERE tx_hash IN ({slots})',
        'proposals': f'''SELECT proposal_id,source_tx_hash,quote_payload,attribution_payload,created_at
            FROM paper_proposals WHERE source_tx_hash IN ({slots})''',
        'decisions': f'''SELECT decision_id,payload,trigger_mode,accepted,reason,created_at
            FROM paper_decisions WHERE JSON_UNQUOTE(JSON_EXTRACT(payload,'$.source_signal.tx_hash'))
            IN ({slots})''',
        'plans': f'''SELECT e.plan_id,e.proposal_id,e.relationship_id,e.follower_wallet,
            e.config_snapshot_hash,e.plan_payload,e.preflight_payload,e.final_review_payload,
            e.created_at,e.updated_at,p.source_tx_hash
            FROM execution_plans e JOIN paper_proposals p ON p.proposal_id=e.proposal_id
            WHERE p.source_tx_hash IN ({slots})''',
        'solver_evidence': f'''SELECT evidence_id,order_id,kind,wallet,tx_hash,payload,created_at
            FROM solver_order_evidence WHERE tx_hash IN ({slots})''',
        'fills': f'''SELECT f.fill_id,f.order_id,f.attribution_payload,f.quote_observed_at,
            f.filled_at,p.source_tx_hash
            FROM paper_fills f JOIN paper_orders o ON o.order_id=f.order_id
            JOIN paper_proposals p ON p.proposal_id=o.proposal_id
            WHERE p.source_tx_hash IN ({slots})''',
        'reserved_lots': f'''SELECT r.proposal_id,r.token_amount_raw,r.status AS reservation_status,
            l.lot_id,l.token,l.principal_asset,l.token_initial_raw,l.token_remaining_raw,
            l.principal_initial_raw,l.principal_remaining_raw,l.attribution_payload,
            l.created_at,l.updated_at,p.source_tx_hash
            FROM paper_position_reservations r JOIN paper_positions l ON l.lot_id=r.lot_id
            JOIN paper_proposals p ON p.proposal_id=r.proposal_id
            WHERE p.source_tx_hash IN ({slots})''',
    }
    context = {'captured_at_utc': str(clock['utc_now']), 'tables': {}}
    for name, sql in queries.items():
        cursor.execute(sql + ' LIMIT 10001', tuple(hashes))
        rows = cursor.fetchall()
        if len(rows) > 10000:
            raise ValueError('history_query_truncated_' + name)
        for row in rows:
            for key, value in list(row.items()):
                if key.endswith('payload') and value is not None:
                    row[key] = obj(value)
                elif isinstance(value, datetime):
                    row[key] = epoch(value)
        context['tables'][name] = rows
    return context


def read_log(path, cases):
    """Freeze the opened prefix. Line order is NOT a wall-clock timestamp."""
    scope = {(c['transaction']['hash'], c['wallet']) for c in cases if c.get('transaction')}
    rows, digest, consumed, malformed = [], hashlib.sha256(), 0, 0
    with path.open('rb') as stream:
        size = os.fstat(stream.fileno()).st_size
        for line_no, raw in enumerate(stream, 1):
            if consumed + len(raw) > size:
                break
            digest.update(raw)
            consumed += len(raw)
            try:
                r = json.loads(raw)
            except (ValueError, UnicodeError):
                malformed += 1
                continue
            if not isinstance(r, dict):
                continue
            key = (r.get('tx_hash'), r.get('wallet'))
            if key not in scope:
                parts = str(r.get('source_event_id', '')).split(':')
                key = tuple(parts[1:3]) if len(parts) >= 3 else ()
            if key in scope:
                rows.append({'line': line_no, 'tx_hash': key[0], 'wallet': key[1], 'payload': r})
    return {'path': str(path), 'prefix_bytes': consumed, 'sha256': digest.hexdigest(),
            'malformed_lines': malformed, 'rows': rows}


def _binding(payload, case):
    return (str(payload.get('relationship_id')) == str(case.get('relationship_id'))
            and payload.get('follower_wallet') == case.get('historical_follower')
            and payload.get('config_snapshot_hash') == case.get('historical_config_hash'))


def _evidence(kind, payload, source, at=None, complete=False, note=None):
    return {'kind': kind, 'source': source, 'available_by': at,
            'timestamp_scope': 'recorded_upper_bound' if at is not None else 'not_recorded',
            'complete_for_snapshot': complete, 'payload_sha256': fingerprint(payload),
            'fields': sorted(payload), 'note': note}


def reconstruct_case(case, context, log):
    """Add only explicitly recorded snapshot content. Truth never fills gaps."""
    case = deepcopy(case)
    tx = transaction_from_record(case['transaction'])
    at = tx.received_at
    tables = context['tables']
    selected = next((r for r in tables['proposals'] if r['proposal_id'] == case['record_id']), None)
    if selected is None:
        raise ValueError('history_proposal_missing')
    attr = selected['attribution_payload']
    if (attr.get('source_tx_hash') != tx.hash or attr.get('smart_wallet') != case['wallet']
            or str(attr.get('relationship_id')) != str(case.get('relationship_id'))):
        raise ValueError('history_proposal_identity_mismatch')
    case['historical_follower'] = attr.get('follower_wallet')
    case['historical_config_hash'] = attr.get('config_snapshot_hash')
    address(case['historical_follower'])
    digest = case['historical_config_hash']
    if not isinstance(digest, str) or len(digest) != 64 or len(bytes.fromhex(digest)) != 32:
        raise ValueError('history_config_binding_missing')
    candidates = parse_candidates(tx, case['wallet']).candidates
    own_logs = [r for r in log['rows'] if (r['tx_hash'], r['wallet']) == (tx.hash, case['wallet'])]
    intents = [r for r in own_logs if r['payload'].get('stage') == 'intent'
               and r['payload'].get('behavior') in {'BUY', 'SELL'}]
    strict = [r for r in own_logs if r['payload'].get('stage') in {
        'swap_evidenced', 'relay_buy_evidenced', 'relay_sell_evidenced'}]
    # Match original trade intent and prefer explicit stale to later enrichment.
    if any(r['payload'].get('fresh') is False for r in intents):
        case['transaction']['fresh'] = False
        tx = transaction_from_record(case['transaction'])
        candidates = parse_candidates(tx, case['wallet']).candidates
    inventory, market_options, decisions = [], [], []
    inventory.append(_evidence('transaction', case['transaction'], 'candidates:' + tx.hash,
                               at, True, 'original_calldata; no receipt fields used by parser'))
    inventory.append(_evidence('policy', attr, 'paper_proposals:' + case['record_id'],
                               selected['created_at'], note='binding/hash only; not full policy or stop history'))

    def signal_evidence(signal, source, observed=None):
        if signal.get('tx_hash') != tx.hash or signal.get('wallet') != case['wallet']:
            return
        e = signal.get('evidence', {})
        if e.get('relay_order_id'):
            inventory.append(_evidence('order', e, source, observed,
                note='derived post-receipt summary; not raw requests/orderData/source-status'))
        if e.get('account_state_source'):
            inventory.append(_evidence('account', e, source, observed,
                note='state-source assertion only; no complete code/block/capture snapshot'))

    def market(payload, source, persisted_at):
        q, ref = payload.get('quote'), payload.get('reference_quote')
        if not isinstance(q, dict) or not isinstance(ref, dict) or payload.get('gas_price_wei') is None:
            return
        if any(not isinstance(q.get(k), str) for k in ('input_asset', 'output_asset', 'amount_in_raw')):
            return
        try:
            qt, rt = timestamp(q['observed_at']), timestamp(ref['observed_at'])
            for raw in (q, ref):
                Quote(**raw)
                address(raw['input_asset'])
                address(raw['output_asset'])
                uint(raw['amount_in_raw'])
                uint(raw['amount_out_raw'])
                hash32(raw['block_hash'])
            uint(str(payload['gas_price_wei']), False)
        except (ValueError, KeyError, TypeError):
            inventory.append(_evidence('market', payload, source, note='incomplete or malformed recorded quote'))
            return
        # Gas is fetched after both quotes, without its own completion timestamp.
        # Use immutable row persistence as a conservative availability upper bound
        # for the whole bundle. Keep quote times separately for latency analysis.
        if max(qt, rt) > persisted_at:
            inventory.append(_evidence('market', payload, source, note='quote timestamp after persistence; inconsistent'))
            return
        p = {'relationship_id': str(case['relationship_id']),
             'follower': case['historical_follower'], 'smart_wallet': case['wallet'],
             'config_snapshot_hash': case['historical_config_hash'],
             'quote': q, 'reference': ref, 'gas_price_wei': payload['gas_price_wei']}
        inventory.append(_evidence('market', p, source, persisted_at, True,
                                   'row persistence upper bound includes gas; quote times kept separately'))
        market_options.append({'payload': p, 'observed_at': persisted_at,
                               'provenance': source})

    market(selected.get('quote_payload') or {}, 'paper_proposals:' + case['record_id'], selected['created_at'])
    for row in tables['decisions']:
        p = row['payload']
        s = p.get('source_signal', {})
        if s.get('tx_hash') != tx.hash or s.get('wallet') != case['wallet'] or not _binding(p, case):
            continue
        source = 'paper_decisions:' + row['decision_id']
        signal_evidence(s, source, row['created_at'])
        market(p, source, row['created_at'])
        decisions.append({'source': source, 'created_at': row['created_at'],
                          'trigger_mode': row['trigger_mode'], 'accepted': bool(row['accepted']),
                          'reason': row['reason'], 'source_stage': s.get('stage')})
    for row in tables['signals']:
        if row['tx_hash'] == tx.hash:
            signal_evidence(row['payload'], 'signals:' + row['event_id'], None)
    for row in own_logs:
        signal_evidence(row['payload'], log['path'] + ':' + str(row['line']))
    for row in tables['plans']:
        if row['proposal_id'] != case['record_id']:
            continue
        if not _binding(row, case):
            raise ValueError('history_plan_binding_mismatch')
        inventory.append(_evidence('preparation', row['preflight_payload'], 'execution_plans:' + row['plan_id'],
                                   row['created_at'], note='recorded actual preparation, not an earlier execution'))
        final = row.get('final_review_payload') or {}
        if final.get('budget'):
            inventory.append(_evidence('portfolio', final['budget'], 'execution_plans:' + row['plan_id'] + ':final_review',
                note='post-reservation budget/lot check; not pre-Feed portfolio; checked_at may precede completion'))
    for row in tables['reserved_lots']:
        if row['proposal_id'] == case['record_id']:
            inventory.append(_evidence('portfolio', row, 'paper_position_reservations:' + row['lot_id'],
                note='current mutable reservation/remaining balance; creation time cannot date current contents'))
    for row in tables['solver_evidence']:
        if row['tx_hash'] == tx.hash and row['wallet'] == case['wallet']:
            inventory.append(_evidence('solver_evidence', row['payload'], 'solver_order_evidence:' + row['evidence_id'],
                                       row['created_at'], note='receipt-based deposit/delivery, not raw API snapshot'))
    # Preserve existing caller-provided snapshots. Choose the newest recorded
    # market available at Feed; if none, retain earliest future snapshot so that
    # replay explicitly reports future evidence, not "database had no quote".
    case.setdefault('snapshots', {})
    if market_options and 'market' not in case['snapshots']:
        available = [m for m in market_options if at is not None and m['observed_at'] <= at]
        case['snapshots']['market'] = (max(available, key=lambda m: m['observed_at']) if available
                                     else min(market_options, key=lambda m: m['observed_at']))
    # Upper bound from an immutable decision signal, not mutable signals.updated_at.
    strict_times = [d['created_at'] for d in decisions if d['source_stage'] in {
        'swap_evidenced', 'relay_buy_evidenced', 'relay_sell_evidenced'}]
    deadline = min(tx.timestamp + 3, at + 3) if tx.timestamp is not None and at is not None else None
    for e in inventory:
        t = e['available_by']
        e['availability_at_feed'] = ('time_unknown' if t is None or at is None else
                                     'recorded_by_feed' if t <= at else 'recorded_after_feed')
        e['within_freshness_window'] = t <= deadline if t is not None and deadline is not None else None
    supported = [c for c in candidates if not c.blockers]
    fresh_supported = [c for c in supported if at is not None and
                       evaluate_candidate(c, at, {}, True)['checks']['freshness']['status'] == 'pass']
    missing = {}
    needed = {'policy', 'portfolio', 'market', 'preparation'}
    if any(c.side == 'BUY' for c in candidates): needed.add('order')
    if any(c.side == 'SELL' for c in candidates): needed.add('account')
    if any(c.route_kind == 'relay_wrapper' for c in candidates): needed.add('deployment')
    for kind in sorted(needed):
        records = [e for e in inventory if e['kind'] == kind]
        complete = [e for e in records if e['complete_for_snapshot']]
        missing[kind] = ('not_found_in_audited_sources' if not records else
                         'partial_only' if not complete else
                         'complete_recorded_by_feed' if any(e['availability_at_feed'] == 'recorded_by_feed' for e in complete)
                         else 'complete_but_after_feed_or_undated')
    audit = {'tx_hash': tx.hash, 'wallet': case['wallet'], 'record_id': case['record_id'],
             'relationship_id': case['relationship_id'], 'side': case['expected_side'],
             'observation_source': tx.observation_source, 'feed_received_at': at,
             'feed_timestamp': tx.timestamp, 'freshness_deadline': deadline,
             'parse_supported': bool(supported), 'parse_supported_and_fresh': bool(fresh_supported),
             'semantic_blockers': sorted({b for c in candidates for b in c.blockers}),
             'original_intent_lines': [r['line'] for r in intents],
             'original_intent_precedes_strict_log': bool(intents and strict and min(r['line'] for r in intents) < min(r['line'] for r in strict)),
             'strict_signal_available_by': min(strict_times) if strict_times else None,
             'strict_timestamp_scope': 'decision_persistence_upper_bound_not_first_observation',
             'original_decisions': decisions, 'snapshot_inventory': missing, 'evidence': inventory,
             'restored_snapshot_names': sorted(case['snapshots']),
             'parsed_intents': [{'side': c.side, 'token_in': c.token_in, 'token_out': c.token_out,
                                'declared_input_raw': c.declared_input_raw, 'minimum_output_raw': c.minimum_output_raw,
                                'order_id': c.order_id, 'route_kind': c.route_kind,
                                'signature_valid': c.metadata.get('signature_valid')} for c in candidates],
             'earliest_recorded_quote_delay_ms': round((min(m['payload']['quote']['observed_at'] for m in market_options)-at)*1000,3)
                 if market_options and at is not None else None,
             'live_enabled': False}
    return case, audit


def reconstruct_cases(cases, context, log):
    restored, rows = [], []
    for case in cases:
        new, audit = reconstruct_case(case, context, log)
        restored.append(new)
        rows.append(audit)
    groups = {}
    for row in rows:
        g = groups.setdefault(row['observation_source'] + ':' + row['side'], Counter())
        g['records'] += 1
        for k in ('parse_supported', 'parse_supported_and_fresh', 'original_intent_precedes_strict_log'):
            g[k] += row[k]
        for name, status in row['snapshot_inventory'].items(): g[name + ':' + status] += 1
        g['restored_market'] += 'market' in row['restored_snapshot_names']
        g['original_feed_decisions'] += sum(d['trigger_mode']=='feed_intent' for d in row['original_decisions'])
        g['original_feed_accepted'] += sum(d['trigger_mode']=='feed_intent' and d['accepted'] for d in row['original_decisions'])
    timings = {}
    for key in groups:
        delays = [r['earliest_recorded_quote_delay_ms'] for r in rows
                  if r['observation_source']+':'+r['side'] == key
                  and r['earliest_recorded_quote_delay_ms'] is not None]
        timings[key] = {'count':len(delays), 'min_ms':min(delays) if delays else None,
                        'median_ms':round(median(delays),3) if delays else None,
                        'max_ms':max(delays) if delays else None,
                        'scope':'Feed receive to original first full-size quote completion; not savings'}
    return restored, {'scope': 'recorded_business_context_not_counterfactual_execution',
        'captured_at_utc': context['captured_at_utc'],
        'table_counts': {k: len(v) for k,v in context['tables'].items()},
        'context_sha256': fingerprint(context), 'log': {k:v for k,v in log.items() if k!='rows'},
        'groups': groups, 'rows': rows, 'original_quote_timing': timings, 'live_enabled': False,
        'notes': ['supported intent parsing is not authenticated ownership or execution',
                  'only followed historical trades; cannot estimate population precision or false-follow rate',
                  'late/partial evidence is preserved but never synthesized into early passes',
                  'zero proven complete decisions is not proof that no trade could have been advanced',
                  'audit covers selected business rows and specified log, not every possible external archive']}


def compact_history(audit):
    """Small per-record report; full evidence remains available without --summary."""
    audit = deepcopy(audit)
    for row in audit['rows']:
        evidence = row.pop('evidence')
        row['evidence_digest'] = fingerprint(evidence)
        # One dated and one undated example per type; count all sources. Never
        # silently truncate a completeness or eligibility calculation.
        row['evidence_summary'] = {}
        for kind in sorted({e['kind'] for e in evidence}):
            items = [e for e in evidence if e['kind'] == kind]
            dated = sorted((e for e in items if e['available_by'] is not None), key=lambda e: e['available_by'])
            undated = [e for e in items if e['available_by'] is None]
            examples = dated[:1] + undated[:1]
            row['evidence_summary'][kind] = {'records': len(items), 'examples': [
                {k: e[k] for k in ('source', 'available_by', 'note', 'payload_sha256')} for e in examples]}
    return audit


def audit_archives(paths, cases):
    """Inventory explicitly named public archives; never use file mtime as capture time."""
    hashes = {c['transaction']['hash'] for c in cases if c.get('transaction')}
    wallets = {c['wallet'] for c in cases}
    orders = {c.order_id for row in cases if row.get('transaction') for c in
              parse_candidates(transaction_from_record(row['transaction']), row['wallet']).candidates}
    result = []
    for path in paths:
        raw = path.read_bytes()
        if len(raw) > 16 * 1024 * 1024:
            raise ValueError('history_archive_too_large')
        document = json.loads(raw)
        text = raw.decode('utf-8').lower()
        requests = document.get('requests') if isinstance(document, dict) else None
        result.append({'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest(),
            'kind': 'relay_requests' if isinstance(requests, list) else 'other_public_evidence',
            'request_count': len(requests) if isinstance(requests, list) else None,
            'cohort_tx_matches': sorted(h for h in hashes if h in text),
            'cohort_order_matches': sorted(o for o in orders if o in text),
            'cohort_wallet_matches': sorted(w for w in wallets if w in text),
            'early_snapshots_created': False,
            'note': 'text matches are archive indexing only, not attribution or point-in-time proof'})
    return result
