from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import agent.linkedin_execution_enforcer as base
import agent.linkedin_execution_enforcer_v2 as v2

QUEUED_STALE_MINUTES = 30
RUNNING_STALE_MINUTES = 30
QUOTA_ROLES = {'lead', 'engagement', 'outreach', 'dm_outreach', 'inbox'}

# One priority convention everywhere: larger number = earlier execution.
# These are the binding daily LinkedIn targets requested by the CEO layer.
HARD_DAILY_TARGETS = {
    'qualified_leads_found': 20,
    'engagement_actions': 3,
    'connection_requests': 10,
    'direct_messages': 5,
}


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


def _enqueue_high_priority(
    role: str,
    *,
    department_role: str | None = None,
    phase: str | None = None,
    target: int | None = None,
    priority_boost: int = 0,
    task_suffix: str = '',
) -> str:
    task, max_steps, priority = base.template(role)
    learned = base.learning_prompt(role)
    if learned:
        task += learned
    if task_suffix:
        task += task_suffix
    inp = {'agent_role': role, 'scheduled': False, 'source': 'agent8-execution-enforcer'}
    learning = base.load_learning()
    if learning:
        inp['learning_run_count'] = int(learning.get('learning_run_count') or 0)
        inp['learning_exploration_share'] = float(learning.get('exploration_share') or 0.15)
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
        'priority': max(1, int(priority) + int(priority_boost)),
        'max_steps': max_steps,
        'input': inp,
    }
    rows = base.post_json(
        f'{base.SUPABASE_URL}/rest/v1/agent_jobs',
        payload,
        {**base.supabase_headers(), 'Prefer': 'return=representation'},
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f'Could not enqueue {department_role or role}')
    return str(rows[0]['id'])


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
            'error': f'RETRY_REQUIRED: Agent 8 quota watchdog replaced stale {status} {role} job after {round(age, 1)} minutes without timely verified progress.',
            'updated_at': now.isoformat(),
        })
        recovered.append({'job_id': str(row['id']), 'role': role, 'old_status': status, 'age_minutes': round(age, 1)})
    return {'stale_recovered': recovered, 'healthy_active_jobs': healthy}


def main() -> None:
    recovery = recover_stale_quota_jobs()
    print(json.dumps({'quota_recovery': recovery}, ensure_ascii=False))

    # Inject corrected policy into the V2 evidence-based implementation.
    base.DAILY_TARGETS = dict(HARD_DAILY_TARGETS)
    v2._original_enqueue = _enqueue_high_priority

    # V2 re-measures real Airtable evidence after stale cleanup and creates only the
    # still-missing executable quota jobs. It uses technically confirmed Interactions.
    v2.main()


if __name__ == '__main__':
    main()
