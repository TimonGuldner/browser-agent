from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
AIRTABLE_PAT = os.environ['AIRTABLE_PAT']
AIRTABLE_BASE_ID = os.environ.get('AIRTABLE_BASE_ID', 'appN6ox7fjGFyXZhL')
RUN_REPORTS = 'tblntmY8DXjSJWq8t'
CONTENT_OPPORTUNITIES = 'tblbDiGu7EeQxnbx2'
STATE = Path('results/linkedin_execution_state.json')

DAILY_TARGETS = {
    'qualified_leads_found': 10,
    'engagement_actions': 3,
    'connection_requests': 5,
}

ENGAGEMENT_PROMPT = r'''

DEDICATED ROLE: LINKEDIN ENGAGEMENT / COMMENT AGENT
You report to Agent 8, the LinkedIn Department Head. Your only purpose is to move suitable people from QUALIFIED/ENGAGE to ENGAGED through authentic relationship-building on LinkedIn.

STRICT SCOPE:
- Work only on relevant qualified people or high-priority Content Opportunities tied to the target audience.
- Before any external action, check Airtable People for duplicates, Do Not Contact, current Contact Status, Owner Agent and recent interactions.
- Do not act on a person owned by another operational agent. If Owner Agent is empty and the person is suitable for engagement, set Owner Agent to AGENT_8E_LINKEDIN_ENGAGEMENT before acting.
- Use a Content Opportunity as the auditable unit of work. If a suitable post is discovered outside an existing opportunity, create/update the Content Opportunity before acting so the post URL and context are traceable.
- Find a genuinely relevant recent post. If there is no suitable post, do nothing and record the reason. Quality beats quota.
- A comment must be 1-3 natural German sentences, specific to the post, helpful, and sound like Timon personally.
- Never use generic praise, sales pitches, links, LOCENIX promotion, company-name dropping, gendering, fake claims or repetitive templates.
- Likes are allowed only when contextually useful; never manufacture activity.
- Never send DMs or connection requests. Those belong to Outreach/Growth.
- Never bypass CAPTCHA, 2FA, checkpoints, rate limits or warnings.
- After a technically confirmed comment, update the Content Opportunity Commented Date to today and Result with the observed outcome; also log the Interaction, set Last Interaction, update Engagement Score from observed evidence, and set Next Step toward CONNECTION_READY when appropriate.
- Do not mark Commented Date unless the comment was technically confirmed on LinkedIn. Agent 8 measures daily engagement from these confirmed records.
- Once engagement work for a person is complete, release Owner Agent or hand it to OUTREACH_GROWTH via Next Step; do not leave stale ownership.
- Only count actions that actually happened on LinkedIn.
'''


def save(obj: dict) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def get_json(url: str, headers: dict[str, str]) -> object:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def post_json(url: str, payload: dict, headers: dict[str, str]) -> object:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        method='POST',
        headers={**headers, 'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode('utf-8')
        return json.loads(body) if body else {}


def airtable_headers() -> dict[str, str]:
    return {'Authorization': f'Bearer {AIRTABLE_PAT}'}


def airtable_today() -> dict[str, int]:
    today = datetime.now(ZoneInfo('Europe/Berlin')).date().isoformat()
    formula = urllib.parse.quote(f"IS_SAME({{Run Time}}, '{today}', 'day')")
    url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{RUN_REPORTS}?pageSize=100&filterByFormula={formula}'
    data = get_json(url, airtable_headers())
    totals = {
        'qualified_leads_found': 0,
        'engagement_actions': 0,
        'connection_requests': 0,
        'replies_received': 0,
        'positive_replies': 0,
        'active_conversations': 0,
    }
    for rec in (data or {}).get('records', []):
        f = rec.get('fields', {})
        totals['qualified_leads_found'] += int(f.get('Qualified Leads Found') or 0)
        totals['connection_requests'] += int(f.get('Connection Requests') or 0)
        totals['replies_received'] += int(f.get('Replies Received') or 0)
        totals['positive_replies'] += int(f.get('Positive Replies') or 0)
        totals['active_conversations'] = max(totals['active_conversations'], int(f.get('Active Conversations') or 0))

    comment_formula = urllib.parse.quote(f"IS_SAME({{Commented Date}}, '{today}', 'day')")
    comment_url = f'https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{CONTENT_OPPORTUNITIES}?pageSize=100&filterByFormula={comment_formula}'
    comment_data = get_json(comment_url, airtable_headers())
    totals['engagement_actions'] = len((comment_data or {}).get('records', []))
    return totals


def supabase_headers() -> dict[str, str]:
    return {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}'}


def existing_active_jobs() -> list[dict]:
    query = urllib.parse.urlencode({'status': 'in.(queued,running)', 'select': 'id,input,status', 'limit': '100'})
    rows = get_json(f'{SUPABASE_URL}/rest/v1/agent_jobs?{query}', supabase_headers())
    return rows if isinstance(rows, list) else []


def active_department_roles(jobs: list[dict]) -> set[str]:
    out = set()
    for row in jobs:
        inp = row.get('input') or {}
        department_role = str(inp.get('department_role') or '').strip().lower()
        runtime_role = str(inp.get('agent_role') or '').strip().lower()
        if department_role:
            out.add(department_role)
        elif runtime_role:
            out.add(runtime_role)
    return out


def template(role: str) -> tuple[str, int, int]:
    q = urllib.parse.urlencode({'role_key': f'eq.{role}', 'select': 'prompt,runtime_adapter,max_steps,priority,enabled', 'limit': '1'})
    rows = get_json(f'{SUPABASE_URL}/rest/v1/agent_templates?{q}', supabase_headers())
    row = rows[0] if isinstance(rows, list) and rows else {}
    if not row or row.get('enabled') is False:
        raise RuntimeError(f'No enabled agent template for role {role}')
    task = '\n\n'.join(x for x in (str(row.get('runtime_adapter') or '').strip(), str(row.get('prompt') or '').strip()) if x)
    return task, int(row.get('max_steps') or 30), int(row.get('priority') or 100)


def enqueue(
    role: str,
    *,
    department_role: str | None = None,
    phase: str | None = None,
    target: int | None = None,
    priority_boost: int = 0,
    task_suffix: str = '',
) -> str:
    task, max_steps, priority = template(role)
    if task_suffix:
        task = task + task_suffix
    inp = {'agent_role': role, 'scheduled': False, 'source': 'agent8-execution-enforcer'}
    if department_role:
        inp['department_role'] = department_role
    if phase:
        inp['pipeline_phase'] = phase
    if target is not None:
        inp['target_new_profiles'] = target
        inp['target_actions'] = target
    payload = {
        'status': 'queued',
        'task': task,
        'mode': 'autonomous',
        'priority': max(1, priority - priority_boost),
        'max_steps': max_steps,
        'input': inp,
    }
    rows = post_json(
        f'{SUPABASE_URL}/rest/v1/agent_jobs',
        payload,
        {**supabase_headers(), 'Prefer': 'return=representation'},
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f'Could not enqueue {department_role or role}')
    return str(rows[0]['id'])


def gaps(metrics: dict[str, int]) -> dict[str, int]:
    return {key: max(0, int(target) - int(metrics.get(key) or 0)) for key, target in DAILY_TARGETS.items()}


def main() -> None:
    now = datetime.now(ZoneInfo('Europe/Berlin'))
    if not (8 <= now.hour < 20):
        result = {
            'status': 'outside_business_hours',
            'checked_at': now.isoformat(),
            'daily_targets': DAILY_TARGETS,
        }
        save(result)
        print(json.dumps(result, ensure_ascii=False))
        return

    metrics = airtable_today()
    jobs = existing_active_jobs()
    active = active_department_roles(jobs)
    created: list[dict[str, str]] = []

    if (
        metrics['positive_replies'] > 0
        or metrics['active_conversations'] > 0
        or metrics['replies_received'] > 0
    ) and 'inbox' not in active:
        created.append({'role': 'inbox', 'job_id': enqueue('inbox', department_role='inbox', priority_boost=30)})
        active.add('inbox')

    if metrics['qualified_leads_found'] < DAILY_TARGETS['qualified_leads_found'] and 'lead' not in active:
        remaining = max(1, DAILY_TARGETS['qualified_leads_found'] - metrics['qualified_leads_found'])
        created.append({
            'role': 'lead',
            'job_id': enqueue('lead', department_role='lead', phase='research_v3', target=min(10, remaining), priority_boost=10),
        })
        active.add('lead')

    if metrics['engagement_actions'] < DAILY_TARGETS['engagement_actions'] and 'engagement' not in active:
        remaining = max(1, DAILY_TARGETS['engagement_actions'] - metrics['engagement_actions'])
        created.append({
            'role': 'engagement',
            'job_id': enqueue(
                'growth',
                department_role='engagement',
                target=min(3, remaining),
                priority_boost=8,
                task_suffix=ENGAGEMENT_PROMPT,
            ),
        })
        active.add('engagement')

    if metrics['connection_requests'] < DAILY_TARGETS['connection_requests'] and 'outreach' not in active:
        created.append({
            'role': 'outreach',
            'job_id': enqueue(
                'growth',
                department_role='outreach',
                target=DAILY_TARGETS['connection_requests'] - metrics['connection_requests'],
                priority_boost=5,
            ),
        })
        active.add('outreach')

    result = {
        'status': 'ok',
        'checked_at': now.isoformat(),
        'daily_metrics': metrics,
        'daily_targets': DAILY_TARGETS,
        'gaps': gaps(metrics),
        'created_jobs': created,
        'active_roles_after_check': sorted(active),
        'funnel_order': [
            'DISCOVERED', 'QUALIFIED', 'ENGAGE', 'ENGAGED', 'CONNECTION_READY',
            'CONNECTION_SENT', 'CONNECTED', 'CONVERSATION_STARTED', 'INTERESTED',
            'CHECK_OFFERED', 'CHECK_ACCEPTED', 'TRIAL', 'PAID',
        ],
        'rule': 'Agent 8 must close observed funnel gaps with role-specific executable work; one person may have only one operational owner and all CRM/safety guardrails remain binding.',
    }
    save(result)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
