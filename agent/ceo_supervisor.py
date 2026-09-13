from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

SUPABASE_URL = os.environ['SUPABASE_URL'].rstrip('/')
SUPABASE_KEY = os.environ['SUPABASE_SERVICE_ROLE_KEY']
AIRTABLE_PAT = os.environ['AIRTABLE_PAT']
LINKEDIN_BASE = os.getenv('AIRTABLE_BASE_ID', 'appN6ox7fjGFyXZhL')
SALES_BASE = os.getenv('AIRTABLE_SALES_BASE_ID', 'appuPKnVyLsbWbxMR')
CEO_OBJECTIVES = 'tbl6DNzaFn8lOYzwF'
PEOPLE = 'tbloK0jz2X6ffr0D2'
INTERACTIONS = 'tbltGdYCuYsEZEghp'
SALES_LEADS = 'tblF4ghkYFzkeQwsT'

OBJECTIVES = {
    'Täglich 20 neue qualifizierte Leads recherchieren': 'qualified_leads',
    'Täglich 10 LinkedIn-Vernetzungsanfragen technisch bestätigen': 'connections',
    'Täglich bis zu 5 sinnvolle LinkedIn-DMs oder qualifizierte Follow-ups ausführen': 'dms',
    'Täglich 3 strategische LinkedIn-Kommentare veröffentlichen': 'comments',
    'Täglich 20 eindeutige freigegebene E-Mails tatsächlich versenden': 'emails',
}
TARGETS = {'qualified_leads': 20, 'connections': 10, 'dms': 5, 'comments': 3, 'emails': 20}


def headers() -> dict[str, str]:
    return {'Authorization': f'Bearer {AIRTABLE_PAT}', 'Content-Type': 'application/json'}


def supabase_headers() -> dict[str, str]:
    return {'apikey': SUPABASE_KEY, 'Authorization': f'Bearer {SUPABASE_KEY}', 'Content-Type': 'application/json'}


def get_json(url: str, h: dict[str, str]) -> object:
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode('utf-8'))


def patch_json(url: str, payload: object, h: dict[str, str]) -> object:
    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), method='PATCH', headers=h)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode('utf-8')
        return json.loads(body) if body else {}


def airtable_all(base: str, table: str, *, fields: list[str] | None = None) -> list[dict]:
    out: list[dict] = []
    offset = ''
    while True:
        params: list[tuple[str, str]] = [('pageSize', '100')]
        for field in fields or []:
            params.append(('fields[]', field))
        if offset:
            params.append(('offset', offset))
        data = get_json(f'https://api.airtable.com/v0/{base}/{table}?{urllib.parse.urlencode(params)}', headers())
        if not isinstance(data, dict):
            break
        out.extend(data.get('records') or [])
        offset = str(data.get('offset') or '')
        if not offset:
            break
    return out


def canonical_linkedin(url: str) -> str:
    return (url or '').split('?')[0].split(',NAME_SEARCH')[0].rstrip('/').lower()


def today_metrics() -> dict[str, int]:
    today = datetime.now(ZoneInfo('Europe/Berlin')).date().isoformat()
    people = airtable_all(LINKEDIN_BASE, PEOPLE, fields=['LinkedIn URL', 'Do Not Contact', 'Contact Status'])
    unique_today: set[str] = set()
    for r in people:
        if str(r.get('createdTime') or '')[:10] != today:
            continue
        f = r.get('fields') or {}
        if bool(f.get('Do Not Contact')) or str(f.get('Contact Status') or '').upper() == 'DO_NOT_CONTACT':
            continue
        key = canonical_linkedin(str(f.get('LinkedIn URL') or ''))
        if key:
            unique_today.add(key)

    interactions = airtable_all(LINKEDIN_BASE, INTERACTIONS, fields=['Date', 'Interaction Type', 'Sent?'])
    connections = dms = comments = 0
    for r in interactions:
        f = r.get('fields') or {}
        if str(f.get('Date') or '')[:10] != today or not bool(f.get('Sent?')):
            continue
        kind = str(f.get('Interaction Type') or '')
        if kind in {'Connection Request', 'LinkedIn Connection Request'}:
            connections += 1
        elif kind == 'DM':
            dms += 1
        elif kind == 'Comment':
            comments += 1

    sales = airtable_all(SALES_BASE, SALES_LEADS, fields=['Email', 'Email Send Status', 'Email Message ID', 'Email Sent At'])
    sent_emails: set[str] = set()
    for r in sales:
        f = r.get('fields') or {}
        if str(f.get('Email Sent At') or '')[:10] != today:
            continue
        if str(f.get('Email Send Status') or '').upper() != 'SENT' or not str(f.get('Email Message ID') or '').strip():
            continue
        email = str(f.get('Email') or '').strip().lower()
        if email:
            sent_emails.add(email)

    return {'qualified_leads': len(unique_today), 'connections': connections, 'dms': dms, 'comments': comments, 'emails': len(sent_emails)}


def _dt(value: object) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except Exception:
        return None


def recent_job_health() -> dict:
    since = (datetime.now(timezone.utc) - timedelta(hours=6)).isoformat()
    q = urllib.parse.urlencode({'select': 'id,status,created_at,updated_at,locked_at,input,error,result,priority','updated_at': f'gte.{since}','order': 'updated_at.desc','limit': '100'})
    rows = get_json(f'{SUPABASE_URL}/rest/v1/agent_jobs?{q}', supabase_headers())
    rows = rows if isinstance(rows, list) else []
    latest_credit_failure: datetime | None = None
    latest_llm_success: datetime | None = None
    security = False
    stale_running = []
    failed = 0
    now = datetime.now(timezone.utc)
    for row in rows:
        status = str(row.get('status') or '').lower()
        if status == 'failed':
            failed += 1
        result = row.get('result') or {}
        text = (str(row.get('error') or '') + ' ' + json.dumps(result, ensure_ascii=False)).lower()
        updated = _dt(row.get('updated_at')) or now
        if any(x in text for x in ('credit_balance_exhausted', 'insufficient_quota', 'no credits remaining')):
            if latest_credit_failure is None or updated > latest_credit_failure:
                latest_credit_failure = updated
        provider = str(result.get('llm_provider') or '').lower() if isinstance(result, dict) else ''
        llm_skipped = bool(result.get('llm_skipped')) if isinstance(result, dict) else False
        if status == 'completed' and provider and not llm_skipped:
            if latest_llm_success is None or updated > latest_llm_success:
                latest_llm_success = updated
        if any(x in text for x in ('captcha required', 'checkpoint required', 'human verification required', '2fa required', 'account restricted')):
            security = True
        if status == 'running':
            locked = _dt(row.get('locked_at'))
            if locked and (now - locked).total_seconds() > 1800:
                stale_running.append(str(row.get('id')))

    alternative_configured = any(os.getenv(k, '').strip() for k in ('BROWSER_USE_API_KEY', 'GOOGLE_API_KEY', 'ANTHROPIC_API_KEY'))
    llm_credit_blocked = bool(
        latest_credit_failure
        and not alternative_configured
        and (latest_llm_success is None or latest_llm_success <= latest_credit_failure)
    )
    no_llm_provider = not any(os.getenv(k, '').strip() for k in ('BROWSER_USE_API_KEY','GOOGLE_API_KEY','ANTHROPIC_API_KEY','OPENAI_API_KEY'))
    email_provider_missing = not bool(os.getenv('RESEND_API_KEY', '').strip())
    return {
        'failed_last_6h': failed,
        'blocked_llm_credits': llm_credit_blocked,
        'blocked_no_llm_provider': no_llm_provider,
        'alternative_llm_provider_configured': alternative_configured,
        'blocked_email_provider_secret': email_provider_missing,
        'blocked_linkedin_security': security,
        'stale_running_jobs': stale_running,
    }


def update_objectives(metrics: dict[str, int], health: dict) -> int:
    rows = airtable_all(LINKEDIN_BASE, CEO_OBJECTIVES, fields=['Objective', 'Current Value', 'Target Value', 'Status', 'Next Action', 'Result / Learning'])
    updates = []
    for r in rows:
        f = r.get('fields') or {}
        name = str(f.get('Objective') or '')
        metric_key = OBJECTIVES.get(name)
        if not metric_key:
            continue
        current = int(metrics.get(metric_key) or 0)
        target = int(f.get('Target Value') or TARGETS[metric_key])
        blocker = ''
        is_blocked = False
        if current < target and metric_key == 'emails' and health.get('blocked_email_provider_secret'):
            blocker = 'BLOCKED_PROVIDER_SECRET: RESEND_API_KEY is missing from the GitHub Actions environment. Agent 0 can measure the email gap but the GitHub worker cannot send it autonomously.'
            is_blocked = True
        elif current < target and metric_key in {'connections', 'dms', 'comments'} and health.get('blocked_no_llm_provider'):
            blocker = 'BLOCKED_LLM_PROVIDER: no usable inference-provider secret is configured for LLM-dependent LinkedIn execution.'
            is_blocked = True
        elif current < target and metric_key in {'connections', 'dms', 'comments'} and health.get('blocked_llm_credits'):
            blocker = 'BLOCKED_LLM_CREDITS: OpenAI returned credit_balance_exhausted and no alternative Browser Use/Google/Anthropic provider is configured.'
            is_blocked = True
        elif current < target and health.get('blocked_linkedin_security') and metric_key in {'qualified_leads','connections','dms','comments'}:
            blocker = 'BLOCKED_PLATFORM: explicit LinkedIn security verification detected.'
            is_blocked = True
        elif current < target:
            blocker = f'Gap remains: {current}/{target}. Agent 0 requires continued closed-loop execution and re-measurement.'
        status = 'DONE' if current >= target else 'BLOCKED' if is_blocked else 'IN_PROGRESS'
        updates.append({'id': r['id'], 'fields': {
            'Current Value': current, 'Status': status,
            'Next Action': blocker or 'Target technically verified for today.',
            'Result / Learning': f'Agent 0 technical measurement {datetime.now(ZoneInfo("Europe/Berlin")).isoformat()}: {current}/{target}. Only confirmed evidence counted.',
        }})
    if updates:
        patch_json(f'https://api.airtable.com/v0/{LINKEDIN_BASE}/{CEO_OBJECTIVES}', {'records': updates, 'typecast': False}, headers())
    return len(updates)


def main() -> None:
    metrics = today_metrics()
    health = recent_job_health()
    gaps = {k: max(0, TARGETS[k] - metrics.get(k, 0)) for k in TARGETS}
    updated = update_objectives(metrics, health)
    hard_block = health.get('blocked_llm_credits') or health.get('blocked_no_llm_provider') or health.get('blocked_linkedin_security') or health.get('blocked_email_provider_secret')
    status = 'ON_TARGET' if not any(gaps.values()) else 'BLOCKED' if hard_block else 'ACTION_REQUIRED'
    report = {
        'agent': 'AGENT_0_LOCENIX_CEO','status': status,
        'checked_at': datetime.now(ZoneInfo('Europe/Berlin')).isoformat(),
        'targets': TARGETS,'actual': metrics,'gaps': gaps,'health': health,
        'objectives_updated': updated,
        'rule': 'Only technically confirmed evidence counts. Re-measure every control run; gaps remain actionable until target is met or a real external blocker is documented.',
    }
    print(json.dumps(report, ensure_ascii=False))


if __name__ == '__main__':
    main()
