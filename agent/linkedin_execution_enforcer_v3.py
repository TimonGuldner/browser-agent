from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import agent.linkedin_execution_enforcer as base
import agent.linkedin_execution_enforcer_v2 as v2

QUEUED_STALE_MINUTES = 45
RUNNING_STALE_MINUTES = 30
QUOTA_ROLES = {'lead', 'engagement', 'outreach', 'dm_outreach', 'inbox'}


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _patch_job(job_id: str, payload: dict) -> None:
    import urllib.request
    req = urllib.request.Request(
        f'{base.SUPABASE_URL}/rest/v1/agent_jobs?id=eq.{job_id}',
        data=json.dumps(payload).encode('utf-8'),
        method='PATCH',
        headers={**base.supabase_headers(), 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def recover_stale_quota_jobs() -> dict:
    now = datetime.now(timezone.utc)
    query = 'status=in.(queued,running)&select=id,status,created_at,updated_at,locked_at,input&limit=200'
    rows = base.get_json(f'{base.SUPABASE_URL}/rest/v1/agent_jobs?{query}', base.supabase_headers())
    rows = rows if isinstance(rows, list) else []
    recovered = []
    healthy = []
    for row in rows:
        inp = row.get('input') or {}
        role = str(inp.get('department_role') or inp.get('agent_role') or '').strip().lower()
        if role not in QUOTA_ROLES:
            continue
        status = str(row.get('status') or '').lower()
        anchor = _parse(row.get('locked_at') if status == 'running' else row.get('created_at'))
        threshold = RUNNING_STALE_MINUTES if status == 'running' else QUEUED_STALE_MINUTES
        age = (now - anchor).total_seconds() / 60 if anchor else threshold + 1
        if age <= threshold:
            healthy.append(str(row.get('id')))
            continue
        _patch_job(str(row['id']), {
            'status': 'failed',
            'error': f'Agent 8 quota recovery: stale {status} {role} job made no timely verified progress; replaced so daily target enforcement can continue.',
            'updated_at': now.isoformat(),
        })
        recovered.append({'job_id': str(row['id']), 'role': role, 'old_status': status, 'age_minutes': round(age, 1)})
    return {'stale_recovered': recovered, 'healthy_active_jobs': healthy}


def main() -> None:
    recovery = recover_stale_quota_jobs()
    print(json.dumps({'quota_recovery': recovery}, ensure_ascii=False))
    # V2 re-measures Airtable evidence after stale cleanup. Missing quotas now create
    # fresh executable jobs instead of being suppressed by dead queued/running rows.
    v2.main()


if __name__ == '__main__':
    main()
