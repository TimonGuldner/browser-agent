from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

AIRTABLE_PAT = os.getenv('AIRTABLE_PAT', '').strip()
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID', 'appN6ox7fjGFyXZhL').strip()
TABLES = {
    'people': 'tbloK0jz2X6ffr0D2',
    'interactions': 'tbltGdYCuYsEZEghp',
    'run_reports': 'tblntmY8DXjSJWq8t',
}

CONFIG = Path('results/linkedin_learning_config.json')
STATE = Path('results/linkedin_learning_state.json')
LATEST = Path('results/linkedin_learning_latest.json')

BASE_SEARCH_QUERIES = [
    'Inhaber Handwerk',
    'Geschäftsführer lokale Dienstleistungen',
    'Inhaber Physiotherapie',
    'Inhaber Kosmetikstudio',
    'Inhaber Autowerkstatt',
    'Inhaber Gebäudereinigung',
    'Inhaber Dachdecker',
    'Inhaber Elektriker',
]

FUNNEL_REWARD = {
    'PAID': 12.0,
    'TRIAL': 9.0,
    'REPORT_VIEWED': 7.0,
    'CHECK_STARTED': 6.5,
    'CHECK_LINK_SENT': 5.5,
    'CHECK_OFFERED': 4.5,
    'INTERESTED': 4.0,
    'DISCOVERY': 3.0,
    'CONVERSATION_STARTED': 2.5,
    'CONNECTED': 1.5,
    'CONNECTION_SENT': 0.5,
    'READY_TO_CONTACT': 0.25,
    'RESEARCHED': 0.0,
    'NEW': 0.0,
    'NOT_NOW': -0.25,
    'NO_RESPONSE': -0.75,
    'NOT_INTERESTED': -2.0,
    'DO_NOT_CONTACT': -4.0,
}

QUERY_MARKER = re.compile(r'Learning Query:\s*([^\n;]+)', re.I)
VARIANT_MARKER = re.compile(r'Learning Variant:\s*([^\n;]+)', re.I)


def load(path: Path, default: Any):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def get_json(url: str) -> Any:
    req = urllib.request.Request(
        url,
        headers={'Authorization': f'Bearer {AIRTABLE_PAT}', 'User-Agent': 'locenix-linkedin-learning/1.0'},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def airtable_records(table_id: str, max_records: int = 500) -> list[dict[str, Any]]:
    if not AIRTABLE_PAT:
        return []
    records: list[dict[str, Any]] = []
    offset = ''
    while len(records) < max_records:
        params: dict[str, str] = {'pageSize': '100'}
        if offset:
            params['offset'] = offset
        payload = get_json(
            f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_id}?{urllib.parse.urlencode(params)}'
        )
        if not isinstance(payload, dict):
            break
        records.extend(payload.get('records') or [])
        offset = str(payload.get('offset') or '')
        if not offset:
            break
    return records[:max_records]


def fields(record: dict[str, Any]) -> dict[str, Any]:
    return record.get('fields') or {}


def truthy(value: Any) -> bool:
    return value is True or str(value).strip().lower() in {'true', '1', 'yes', 'active', 'started', 'paid'}


def person_reward(f: dict[str, Any]) -> float:
    status = str(f.get('Contact Status') or '').strip().upper()
    reward = FUNNEL_REWARD.get(status, 0.0)
    interest = str(f.get('Interest') or '').strip().upper()
    dm_status = str(f.get('DM Status') or '').strip().upper()

    if interest == 'HIGH':
        reward = max(reward, 4.5)
    elif interest == 'MEDIUM':
        reward = max(reward, 3.0)
    if dm_status == 'REPLIED' or f.get('Last Message From Lead'):
        reward = max(reward, 2.0)
    if truthy(f.get('Scanner Link Sent')):
        reward = max(reward, 5.0)
    elif truthy(f.get('Scanner Offered')):
        reward = max(reward, 4.0)
    if truthy(f.get('Trial')) or truthy(f.get('Trial Active')) or str(f.get('Trial Status') or '').upper() in {'ACTIVE', 'STARTED'}:
        reward = max(reward, 9.0)
    if truthy(f.get('Paid')) or truthy(f.get('Paid Customer')) or (isinstance(f.get('MRR'), (int, float)) and f.get('MRR') > 0):
        reward = max(reward, 12.0)
    if truthy(f.get('Do Not Contact')):
        reward = min(reward, -4.0)
    return reward


def title_segment(f: dict[str, Any]) -> str:
    text = f"{f.get('Job Title','')} {f.get('Why Suitable','')} {f.get('Notes','')}".lower()
    groups = [
        ('owner_founder', ('inhaber', 'owner', 'founder', 'gründer', 'geschaeftsfuehrer', 'geschäftsführer', 'ceo')),
        ('marketing', ('marketing', 'growth', 'seo', 'communications', 'kommunikation')),
        ('health_local', ('physio', 'osteopath', 'podolog', 'ergotherap', 'logop', 'heilprakt')),
        ('beauty_local', ('kosmetik', 'beauty', 'friseur', 'barber', 'nagel')),
        ('home_services', ('dach', 'elektr', 'sanit', 'heizung', 'reinigung', 'maler', 'handwerk')),
        ('automotive', ('auto', 'werkstatt', 'reifen')),
        ('agency', ('agentur', 'agency', 'consulting')),
    ]
    for name, needles in groups:
        if any(x in text for x in needles):
            return name
    return 'other'


def message_features(message: str) -> list[str]:
    msg = (message or '').strip()
    if not msg:
        return []
    lower = msg.lower()
    feats = []
    length = len(msg)
    feats.append('length_short' if length <= 350 else ('length_medium' if length <= 650 else 'length_long'))
    feats.append('question' if '?' in msg else 'no_question')
    if any(x in lower for x in ('visibility check', 'sichtbarkeitscheck', 'local visibility check', 'kostenlos', 'scanner')):
        feats.append('check_cta')
    else:
        feats.append('no_check_cta')
    if any(x in lower for x in ('google maps', 'google business', 'unternehmensprofil', 'local seo')):
        feats.append('specific_local_seo_problem')
    else:
        feats.append('generic_topic')
    return feats


def accumulate(bucket: dict[str, dict[str, float]], key: str, reward: float) -> None:
    if not key:
        return
    row = bucket.setdefault(key, {'samples': 0.0, 'reward': 0.0, 'positive': 0.0, 'negative': 0.0})
    row['samples'] += 1
    row['reward'] += reward
    if reward >= 2:
        row['positive'] += 1
    if reward < 0:
        row['negative'] += 1


def finalize(bucket: dict[str, dict[str, float]]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for key, row in bucket.items():
        samples = int(row['samples'])
        avg = row['reward'] / max(1, samples)
        out[key] = {
            'samples': samples,
            'reward': round(row['reward'], 3),
            'avg_reward': round(avg, 3),
            'positive_rate': round(row['positive'] / max(1, samples), 3),
            'negative_rate': round(row['negative'] / max(1, samples), 3),
        }
    return out


def raw_weight(avg_reward: float, samples: int) -> float:
    confidence = min(1.0, samples / 8.0)
    # Bounded soft learning: evidence can move a strategy, but never dominate instantly.
    signal = math.tanh(avg_reward / 4.0)
    return max(0.65, min(1.5, 1.0 + 0.5 * confidence * signal))


def smoothed_weight(previous: float, fresh: float, samples: int) -> float:
    alpha = min(0.45, 0.15 + samples * 0.03)
    return round(max(0.65, min(1.5, previous * (1 - alpha) + fresh * alpha)), 3)


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    previous = load(CONFIG, {})
    people = airtable_records(TABLES['people'], 500)
    interactions = airtable_records(TABLES['interactions'], 500)
    runs = airtable_records(TABLES['run_reports'], 250)

    query_stats_raw: dict[str, dict[str, float]] = {}
    title_stats_raw: dict[str, dict[str, float]] = {}
    source_stats_raw: dict[str, dict[str, float]] = {}
    message_stats_raw: dict[str, dict[str, float]] = {}
    variant_stats_raw: dict[str, dict[str, float]] = {}

    evaluated = 0
    attributed_queries = 0
    for record in people:
        f = fields(record)
        reward = person_reward(f)
        evaluated += 1
        notes = str(f.get('Notes') or '')
        query_match = QUERY_MARKER.search(notes)
        variant_match = VARIANT_MARKER.search(notes)
        query = query_match.group(1).strip() if query_match else ''
        variant = variant_match.group(1).strip() if variant_match else ''
        if query:
            attributed_queries += 1
            accumulate(query_stats_raw, query, reward)
        if variant:
            accumulate(variant_stats_raw, variant, reward)
        accumulate(title_stats_raw, title_segment(f), reward)
        accumulate(source_stats_raw, str(f.get('Source') or 'LinkedIn Search').strip(), reward)
        for feat in message_features(str(f.get('Last Message From Us') or '')):
            accumulate(message_stats_raw, feat, reward)

    query_stats = finalize(query_stats_raw)
    title_stats = finalize(title_stats_raw)
    source_stats = finalize(source_stats_raw)
    message_stats = finalize(message_stats_raw)
    variant_stats = finalize(variant_stats_raw)

    prev_weights = previous.get('query_weights') or {}
    query_weights: dict[str, float] = {}
    for query in BASE_SEARCH_QUERIES:
        stats = query_stats.get(query, {'samples': 0, 'avg_reward': 0.0})
        fresh = raw_weight(float(stats['avg_reward']), int(stats['samples']))
        query_weights[query] = smoothed_weight(float(prev_weights.get(query, 1.0)), fresh, int(stats['samples']))
    for query, stats in query_stats.items():
        if query not in query_weights:
            fresh = raw_weight(float(stats['avg_reward']), int(stats['samples']))
            query_weights[query] = smoothed_weight(float(prev_weights.get(query, 1.0)), fresh, int(stats['samples']))

    ranked_queries = sorted(query_weights, key=lambda q: (-query_weights[q], -int(query_stats.get(q, {}).get('samples', 0)), q))

    preferred_features = []
    avoid_features = []
    for feat, stats in sorted(message_stats.items(), key=lambda kv: kv[1]['avg_reward'], reverse=True):
        if stats['samples'] >= 5 and stats['avg_reward'] >= 1.5:
            preferred_features.append(feat)
        if stats['samples'] >= 5 and stats['avg_reward'] < 0:
            avoid_features.append(feat)

    # Learn useful time windows only from actual run-report outcomes; advisory, never a hard restriction.
    time_raw: dict[str, dict[str, float]] = {}
    for record in runs:
        f = fields(record)
        raw_time = str(f.get('Run Time') or '')
        try:
            dt = datetime.fromisoformat(raw_time.replace('Z', '+00:00')).astimezone(ZoneInfo('Europe/Berlin'))
        except Exception:
            continue
        reward = (
            3.0 * float(f.get('Positive Replies') or 0)
            + 1.5 * float(f.get('Replies Received') or 0)
            + 0.5 * float(f.get('Connection Requests') or 0)
        )
        accumulate(time_raw, f'{dt.weekday()}:{dt.hour:02d}', reward)
    time_stats = finalize(time_raw)
    preferred_time_slots = [
        slot for slot, stats in sorted(time_stats.items(), key=lambda kv: kv[1]['avg_reward'], reverse=True)
        if stats['samples'] >= 3 and stats['avg_reward'] > 0
    ][:5]

    run_count = int(previous.get('learning_run_count') or 0) + 1
    config = {
        'agent': 'AGENT_8L_LINKEDIN_LEARNING_OPTIMIZER',
        'generated_at': now,
        'learning_run_count': run_count,
        'mode': 'SAFE_OUTCOME_LEARNING',
        'principle': 'Learn priorities from measured LinkedIn/Airtable outcomes; never rewrite executable code or bypass safety rules.',
        'exploration_share': 0.15,
        'minimum_samples_before_downweight': 5,
        'query_weights': query_weights,
        'recommended_queries': ranked_queries[:8],
        'preferred_message_features': preferred_features[:5],
        'avoid_message_features': avoid_features[:5],
        'preferred_time_slots_berlin': preferred_time_slots,
        'title_segment_stats': title_stats,
        'source_stats': source_stats,
        'message_feature_stats': message_stats,
        'variant_stats': variant_stats,
        'query_stats': query_stats,
        'guardrails': {
            'do_not_contact_never_overridden': True,
            'daily_contact_limits_never_increased': True,
            'captcha_or_security_bypass': False,
            'new_linkedin_account': False,
            'autonomous_code_rewrite': False,
            'prompt_rewrite': False,
        },
    }

    state = {
        'agent': 'AGENT_8L_LINKEDIN_LEARNING_OPTIMIZER',
        'last_run_at': now,
        'status': 'OK',
        'learning_run_count': run_count,
        'people_evaluated': evaluated,
        'interactions_observed': len(interactions),
        'run_reports_observed': len(runs),
        'query_attribution_count': attributed_queries,
        'learned_queries': len(query_stats),
        'learned_message_features': len(message_stats),
        'recommended_query': ranked_queries[0] if ranked_queries else BASE_SEARCH_QUERIES[0],
        'exploration_share': 0.15,
        'errors': 0,
    }

    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    STATE.write_text(json.dumps(state, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    LATEST.write_text(json.dumps({'state': state, 'learning': config}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(state, ensure_ascii=False))


if __name__ == '__main__':
    main()
