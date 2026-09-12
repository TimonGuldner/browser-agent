from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

TASK = Path('tasks/linkedin_department_head.json')
OUT = Path('results/linkedin_department_latest.json')
STATE = Path('results/linkedin_department_state.json')
DIRECTIVE_URL = 'https://raw.githubusercontent.com/TimonGuldner/locenix-lead-research-agent/main/results/linkedin_directive.json'
AIRTABLE_BASE_ID = os.getenv('AIRTABLE_BASE_ID', 'appN6ox7fjGFyXZhL').strip()
AIRTABLE_PAT = os.getenv('AIRTABLE_PAT', '').strip()
SUPABASE_URL = os.getenv('SUPABASE_URL', '').strip().rstrip('/')
SUPABASE_KEY = os.getenv('SUPABASE_SERVICE_ROLE_KEY', '').strip()
TABLES = {
    'people': 'tbloK0jz2X6ffr0D2',
    'queue': 'tblnWGZEjrRD5suj4',
    'run_reports': 'tblntmY8DXjSJWq8t',
}


def load(path: Path, default: Any = None):
    if default is None:
        default = {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def get_json(url: str, headers: dict[str, str] | None = None, timeout: int = 20, default: Any = None):
    if default is None:
        default = {}
    try:
        req = urllib.request.Request(url, headers=headers or {'User-Agent': 'locenix-linkedin-department-head'})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode('utf-8'))
    except Exception:
        return default


def airtable_records(table_id: str, max_records: int = 500) -> list[dict[str, Any]]:
    if not AIRTABLE_PAT:
        return []
    records: list[dict[str, Any]] = []
    offset = ''
    while len(records) < max_records:
        params = {'pageSize': '100'}
        if offset:
            params['offset'] = offset
        url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_id}?{urllib.parse.urlencode(params)}"
        payload = get_json(url, {'Authorization': f'Bearer {AIRTABLE_PAT}', 'User-Agent': 'locenix-agent8'}, default={})
        if not isinstance(payload, dict):
            break
        records.extend(payload.get('records') or [])
        offset = str(payload.get('offset') or '')
        if not offset:
            break
    return records[:max_records]


def supabase_rows(table: str, query: str) -> list[dict[str, Any]]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return []
    url = f"{SUPABASE_URL}/rest/v1/{table}?{query}"
    rows = get_json(url, {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}', 'User-Agent': 'locenix-agent8'}, default=[])
    return rows if isinstance(rows, list) else []


def truthy(v: Any) -> bool:
    return v is True or str(v).strip().lower() in {'true', 'yes', '1', 'paid', 'active'}


def iso_day(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def field(row: dict[str, Any], name: str, default: Any = None):
    return (row.get('fields') or {}).get(name, default)


def metrics_from_sources():
    people = airtable_records(TABLES['people'])
    queue = airtable_records(TABLES['queue'])
    runs = airtable_records(TABLES['run_reports'], 200)
    jobs = supabase_rows('agent_jobs', 'select=id,status,mode,priority,created_at,updated_at,result&order=created_at.desc&limit=200')

    negative = {'NOT_INTERESTED', 'NO_RESPONSE', 'DO_NOT_CONTACT'}
    qualified_status = {'READY_TO_CONTACT', 'CONNECTION_SENT', 'CONNECTED', 'CONVERSATION_STARTED', 'DISCOVERY', 'INTERESTED', 'CHECK_OFFERED', 'CHECK_LINK_SENT', 'CHECK_STARTED', 'REPORT_VIEWED', 'EARLY_ACCESS_INTEREST', 'FOLLOW_UP', 'NOT_NOW'}
    active_status = {'CONVERSATION_STARTED', 'DISCOVERY', 'INTERESTED', 'CHECK_OFFERED', 'CHECK_LINK_SENT', 'CHECK_STARTED', 'REPORT_VIEWED', 'EARLY_ACCESS_INTEREST', 'FOLLOW_UP', 'NOT_NOW'}
    positive_status = {'INTERESTED', 'CHECK_OFFERED', 'CHECK_LINK_SENT', 'CHECK_STARTED', 'REPORT_VIEWED', 'EARLY_ACCESS_INTEREST'}
    today = datetime.now().date()

    def status(r): return str(field(r, 'Contact Status', '')).upper()
    leads = len(people) if people else 'UNKNOWN'
    qualified = sum(1 for r in people if status(r) in qualified_status or (isinstance(field(r, 'Lead Quality Score'), (int, float)) and field(r, 'Lead Quality Score') >= 70)) if people else 'UNKNOWN'
    active = sum(1 for r in people if status(r) in active_status and status(r) not in negative) if people else 'UNKNOWN'
    replies = sum(1 for r in people if str(field(r, 'DM Status', '')).upper() == 'REPLIED' or bool(field(r, 'Last Message From Lead'))) if people else 'UNKNOWN'
    positive = sum(1 for r in people if status(r) in positive_status or str(field(r, 'Interest', '')).upper() in {'MEDIUM', 'HIGH'}) if people else 'UNKNOWN'
    open_followups = sum(1 for r in people if status(r) not in negative and (field(r, 'Follow-up Date') or status(r) in {'FOLLOW_UP', 'NOT_NOW'})) if people else 'UNKNOWN'
    overdue = sum(1 for r in people if status(r) not in negative and (iso_day(field(r, 'Follow-up Date')) is not None and iso_day(field(r, 'Follow-up Date')) <= today)) if people else 'UNKNOWN'
    offered = sum(1 for r in people if truthy(field(r, 'Scanner Offered')) or status(r) in {'CHECK_OFFERED', 'CHECK_LINK_SENT', 'CHECK_STARTED', 'REPORT_VIEWED'}) if people else 'UNKNOWN'
    accepted = sum(1 for r in people if status(r) in {'CHECK_STARTED', 'REPORT_VIEWED', 'EARLY_ACCESS_INTEREST'}) if people else 'UNKNOWN'
    trials = sum(1 for r in people if truthy(field(r, 'Trial')) or truthy(field(r, 'Trial Active')) or str(field(r, 'Trial Status', '')).upper() in {'ACTIVE', 'STARTED'}) if people else 'UNKNOWN'
    paid = sum(1 for r in people if truthy(field(r, 'Paid')) or truthy(field(r, 'Paid Customer')) or (isinstance(field(r, 'MRR'), (int, float)) and field(r, 'MRR') > 0)) if people else 'UNKNOWN'
    backlog = sum(1 for r in queue if not truthy(field(r, 'Executed')) and str(field(r, 'Status', '')).lower() not in {'skipped', 'failed', 'executed'}) if queue else 0
    failed_jobs = sum(1 for j in jobs if str(j.get('status', '')).lower() == 'failed') if jobs else 'UNKNOWN'
    queued_jobs = sum(1 for j in jobs if str(j.get('status', '')).lower() == 'queued') if jobs else 'UNKNOWN'

    risk_terms = ('captcha', 'checkpoint', 'security', 'verification', 'rate limit', '2fa')
    risk_hits = []
    for j in jobs:
        blob = json.dumps(j.get('result') or {}, ensure_ascii=False).lower()
        if any(t in blob for t in risk_terms):
            risk_hits.append(str(j.get('id')))
    account_risk = bool(risk_hits) if jobs else 'UNKNOWN'

    role_health = {r: 'UNKNOWN' for r in ('Inbox', 'Growth', 'Lead', 'Content')}
    if runs:
        for role in role_health:
            matching = [r for r in runs if str(field(r, 'Agent Role', '')).lower() == role.lower()]
            if matching:
                latest = matching[0]
                rs = str(field(latest, 'Run Status', '')).lower()
                role_health[role] = 'HEALTHY' if rs in {'completed', 'success', 'successful'} else ('BLOCKED' if rs in {'failed', 'blocked'} else 'ATTENTION')

    return {
        'leads': leads,
        'qualified_leads': qualified,
        'active_conversations': active,
        'replies': replies,
        'positive_signals': positive,
        'open_followups': open_followups,
        'overdue_followups': overdue,
        'visibility_checks_offered': offered,
        'visibility_checks_accepted': accepted,
        'trials': trials,
        'paid_customers': paid,
        'queue_backlog': backlog,
        'failed_jobs': failed_jobs,
        'queued_jobs': queued_jobs,
        'account_risk': account_risk,
        'agent_health': {**role_health, 'Browser Worker': 'HEALTHY' if jobs else 'UNKNOWN'},
        'source_health': {'airtable': 'AVAILABLE' if people or queue or runs else 'UNKNOWN', 'supabase': 'AVAILABLE' if jobs else 'UNKNOWN'},
        'risk_job_ids': risk_hits[:5],
    }


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def main():
    cfg = load(TASK)
    if not cfg.get('enabled'):
        print(json.dumps({'status': 'disabled'}))
        return

    metrics = metrics_from_sources()
    directive = get_json(DIRECTIVE_URL, default={})
    priorities = []
    if metrics['account_risk'] is True:
        priorities.append(('SYSTEM_SAFETY', 'Pause risky LinkedIn actions and escalate the observed account/security signal.', 'Browser Worker'))
    if (num(metrics['positive_signals']) or 0) > 0:
        priorities.append(('POSITIVE_REPLIES', 'Prioritize positive replies and active qualified conversations before new prospecting.', 'Conversation / Inbox Agent'))
    if (num(metrics['active_conversations']) or 0) > 0:
        priorities.append(('ACTIVE_CONVERSATIONS', 'Work active conversations before generating additional outreach volume.', 'Conversation Agent'))
    if (num(metrics['overdue_followups']) or 0) > 0:
        priorities.append(('FOLLOWUPS', 'Clear overdue qualified follow-ups before new lead research.', 'Inbox / Follow-up Agent'))
    if (num(metrics['qualified_leads']) or 0) > 0:
        priorities.append(('QUALIFIED_LEADS', 'Process existing qualified leads before increasing discovery volume.', 'Lead / Conversation Agent'))
    if not priorities:
        priorities.append(('GROWTH', 'No observed downstream bottleneck; keep conservative qualified lead generation active.', 'Lead Agent'))

    priorities = priorities[: int(cfg.get('max_priorities', 3))]
    status = 'BLOCKED' if priorities[0][0] == 'SYSTEM_SAFETY' else ('ATTENTION' if priorities[0][0] in {'FOLLOWUPS'} or (num(metrics['failed_jobs']) or 0) > 0 else 'HEALTHY')
    now = datetime.now(timezone.utc).isoformat()

    def p(i):
        if i >= len(priorities): return None
        typ, action, owner = priorities[i]
        return {'type': typ, 'action': action, 'owner': owner}

    directive_status = 'NONE'
    if isinstance(directive, dict) and directive.get('owner_agent') == 'AGENT_8_LINKEDIN_DEPARTMENT_HEAD':
        directive_status = 'RECEIVED'

    report = {
        'agent': 'AGENT_8_LINKEDIN_DEPARTMENT_HEAD',
        'reports_to': 'AGENT_0_LOCENIX_CEO',
        'last_run_at': now,
        'department_status': status,
        'department_health': status,
        **metrics,
        'biggest_bottleneck': priorities[0][0],
        'priority_1': p(0),
        'priority_2': p(1),
        'priority_3': p(2),
        'current_experiment': 'UNKNOWN',
        'main_learning': 'Operational LinkedIn metrics are now read directly from Airtable and Supabase; missing fields remain UNKNOWN.',
        'received_ceo_directive': directive if directive_status == 'RECEIVED' else None,
        'directive_status': directive_status,
        'human_decision_required': metrics['account_risk'] is True,
        'ceo_escalation_required': metrics['account_risk'] is True,
        'ceo_message': priorities[0][1] if metrics['account_risk'] is True else 'No CEO intervention required from currently observed evidence.',
        'guardrails': cfg.get('rules', {}),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    state_keys = (
        'last_run_at','department_status','department_health','leads','qualified_leads','active_conversations','replies','positive_signals',
        'open_followups','overdue_followups','visibility_checks_offered','visibility_checks_accepted','trials','paid_customers','queue_backlog',
        'failed_jobs','queued_jobs','account_risk','agent_health','biggest_bottleneck','priority_1','priority_2','priority_3','current_experiment',
        'main_learning','directive_status','received_ceo_directive','human_decision_required','ceo_escalation_required','ceo_message'
    )
    STATE.write_text(json.dumps({k: report.get(k, 'UNKNOWN') for k in state_keys}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))

if __name__ == '__main__':
    main()
