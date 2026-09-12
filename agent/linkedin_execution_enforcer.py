from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
AIRTABLE_PAT = os.environ['AIRTABLE_PAT']
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID', 'appN6ox7fjGFyXZhL')
RUN_REPORTS = 'tblntmY8DXjSJWq8t'

DAILY_TARGETS = {
    'qualified_leads_found': 10,
    'connection_requests': 5,
}


def get_json(url: str, headers: dict[str, str]) -> object:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def post_json(url: str, payload: dict, headers: dict[str, str]) -> object:
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method='POST', headers={**headers, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode('utf-8')
        return json.loads(body) if body else {}


def airtable_today() -> dict[str, int]:
    today = datetime.now(ZoneInfo('Europe/Berlin')).date().isoformat()
    formula = urllib.parse.quote(f"IS_SAME({{Run Time}}, '{today}', 'day')")
    url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RUN_REPORTS}?pageSize=100&filterByFormula={formula}'
    data = get_json(url, {'Authorization': f'Bearer {AIRTABLE_PAT}'})
    totals = {'qualified_leads_found': 0, 'connection_requests': 0, 'replies_received': 0, 'positive_replies': 0, 'active_conversations': 0}
    for rec in (data or {}).get('records', []):
        f = rec.get('fields', {})
        totals['qualified_leads_found'] += int(f.get('Qualified Leads Found') or 0)
        totals['connection_requests'] += int(f.get('Connection Requests') or 0)
        totals['replies_received'] += int(f.get('Replies Received') or 0)
        totals['positive_replies'] += int(f.get('Positive Replies') or 0)
        totals['active_conversations'] = max(totals['active_conversations'], int(f.get('Active Conversations') or 0))
    return totals


def supabase_headers() -> dict[str, str]:
    return {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}


def existing_active_roles() -> set[str]:
    query = urllib.parse.urlencode({'status': 'in.(queued,running)', 'select': 'input,status', 'limit': '100'})
    rows = get_json(f'{SUPABASE_URL}/rest/v1/agent_jobs?{query}', supabase_headers())
    out = set()
    for row in rows if isinstance(rows, list) else []:
        role = str((row.get('input') or {}).get('agent_role') or '').lower()
        if role:
            out.add(role)
    return out


def template(role: str) -> tuple[str, int, int]:
    q = urllib.parse.urlencode({'role_key': f'eq.{role}', 'select': 'prompt,runtime_adapter,max_steps,priority,enabled', 'limit': '1'})
    rows = get_json(f'{SUPABASE_URL}/rest/v1/agent_templates?{q}', supabase_headers())
    row = rows[0] if isinstance(rows, list) and rows else {}
    if not row or row.get('enabled') is False:
        raise RuntimeError(f'No enabled agent template for role {role}')
    task = '\n\n'.join(x for x in (str(row.get('runtime_adapter') or '').strip(), str(row.get('prompt') or '').strip()) if x)
    return task, int(row.get('max_steps') or 30), int(row.get('priority') or 100)


def enqueue(role: str, *, phase: str | None = None, target: int | None = None, priority_boost: int = 0) -> str:
    task, max_steps, priority = template(role)
    inp = {'agent_role': role, 'scheduled': False, 'source': 'agent8-execution-enforcer'}
    if phase:
        inp['pipeline_phase'] = phase
    if target is not None:
        inp['target_new_profiles'] = target
    payload = {
        'status': 'queued',
        'task': task,
        'mode': 'autonomous',
        'priority': max(1, priority - priority_boost),
        'max_steps': max_steps,
        'input': inp,
    }
    rows = post_json(f'{SUPABASE_URL}/rest/v1/agent_jobs', payload, {**supabase_headers(), 'Prefer': 'return=representation'})
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f'Could not enqueue {role}')
    return str(rows[0]['id'])


def main() -> None:
    now = datetime.now(ZoneInfo('Europe/Berlin'))
    if not (8 <= now.hour < 20):
        print(json.dumps({'status': 'outside_business_hours'}))
        return

    metrics = airtable_today()
    active = existing_active_roles()
    created: list[dict[str, str]] = []

    # Existing conversations always come first. Inbox is allowed to decide that no message is needed.
    if (metrics['positive_replies'] > 0 or metrics['active_conversations'] > 0 or metrics['replies_received'] > 0) and 'inbox' not in active:
        created.append({'role': 'inbox', 'job_id': enqueue('inbox', priority_boost=30)})
        active.add('inbox')

    # Keep lead collection moving every business day until the daily target is met.
    if metrics['qualified_leads_found'] < DAILY_TARGETS['qualified_leads_found'] and 'lead' not in active:
        remaining = max(1, DAILY_TARGETS['qualified_leads_found'] - metrics['qualified_leads_found'])
        created.append({'role': 'lead', 'job_id': enqueue('lead', phase='research_v3', target=min(10, remaining), priority_boost=10)})
        active.add('lead')

    # Do not let a day end with zero outbound activity when qualified capacity exists.
    # Growth handles conservative relationship-building; existing safety and CRM rules stay binding.
    if metrics['connection_requests'] < DAILY_TARGETS['connection_requests'] and 'growth' not in active:
        created.append({'role': 'growth', 'job_id': enqueue('growth', priority_boost=5)})
        active.add('growth')

    print(json.dumps({'status': 'ok', 'daily_metrics': metrics, 'daily_targets': DAILY_TARGETS, 'created_jobs': created}, ensure_ascii=False))


if __name__ == '__main__':
    main()
