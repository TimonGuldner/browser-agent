from __future__ import annotations

import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import agent.linkedin_execution_enforcer as base
import agent.linkedin_execution_enforcer_v2 as v2

QUEUED_STALE_MINUTES = 30
RUNNING_STALE_MINUTES = 30
QUOTA_ROLES = {'lead', 'engagement', 'outreach', 'dm_outreach', 'inbox'}
HARD_DAILY_TARGETS = {
    'qualified_leads_found': 20,
    'engagement_actions': 3,
    'connection_requests': 10,
    'direct_messages': 5,
}
TRANSIENT_HTTP_CODES = {408, 425, 429, 500, 502, 503, 504}


def _parse(value: object) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(timezone.utc)
    except Exception:
        return None


def _with_transient_retry(label: str, fn, attempts: int = 4):
    delay = 1.0
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except urllib.error.HTTPError as exc:
            last_error = exc
            if exc.code not in TRANSIENT_HTTP_CODES or attempt >= attempts:
                raise
        except (urllib.error.URLError, TimeoutError) as exc:
            last_error = exc
            if attempt >= attempts:
                raise
        print(json.dumps({
            'warning': 'transient_supabase_error',
            'operation': label,
            'attempt': attempt,
            'next_retry_seconds': delay,
            'error': str(last_error),
        }, ensure_ascii=False))
        time.sleep(delay)
        delay = min(delay * 2, 8.0)
    if last_error:
        raise last_error
    raise RuntimeError(f'{label} failed without an exception')


def _patch_job(job_id: str, payload: dict) -> None:
    import urllib.request

    def _request():
        req = urllib.request.Request(
            f'{base.SUPABASE_URL}/rest/v1/agent_jobs?id=eq.{job_id}',
            data=json.dumps(payload).encode('utf-8'), method='PATCH',
            headers={**base.supabase_headers(), 'Content-Type': 'application/json', 'Prefer': 'return=minimal'},
        )
        with urllib.request.urlopen(req, timeout=30):
            pass

    _with_transient_retry('patch_agent_job', _request)


def _enqueue_high_priority(role: str, *, department_role: str | None = None, phase: str | None = None,
                           target: int | None = None, priority_boost: int = 0, task_suffix: str = '') -> str:
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
        'status': 'queued', 'task': task, 'mode': 'autonomous',
        'priority': max(1, int(priority) + int(priority_boost)),
        'max_steps': max_steps, 'input': inp,
    }
    rows = _with_transient_retry(
        f'enqueue_{department_role or role}',
        lambda: base.post_json(
            f'{base.SUPABASE_URL}/rest/v1/agent_jobs', payload,
            {**base.supabase_headers(), 'Prefer': 'return=representation'},
        ),
    )
    if not isinstance(rows, list) or not rows:
        raise RuntimeError(f'Could not enqueue {department_role or role}')
    return str(rows[0]['id'])


def recover_stale_quota_jobs() -> dict:
    now = datetime.now(timezone.utc)
    query = 'status=in.(queued,running)&select=id,status,created_at,updated_at,locked_at,input&limit=200'
    try:
        rows = _with_transient_retry(
            'load_active_quota_jobs',
            lambda: base.get_json(f'{base.SUPABASE_URL}/rest/v1/agent_jobs?{query}', base.supabase_headers()),
        )
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        # A watchdog read must never prevent the actual quota-enforcement pass from running.
        print(json.dumps({
            'warning': 'quota_watchdog_read_failed_after_retries',
            'error': str(exc),
            'action': 'continue_without_stale_recovery',
        }, ensure_ascii=False))
        return {'stale_recovered': [], 'healthy_active_jobs': [], 'watchdog_degraded': True, 'error': str(exc)}

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
        try:
            _patch_job(str(row['id']), {
                'status': 'failed',
                'error': f'RETRY_REQUIRED: Agent 8 quota watchdog replaced stale {status} {role} job after {round(age, 1)} minutes without timely verified progress.',
                'updated_at': now.isoformat(),
            })
            recovered.append({'job_id': str(row['id']), 'role': role, 'old_status': status, 'age_minutes': round(age, 1)})
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
            print(json.dumps({
                'warning': 'stale_job_patch_failed_after_retries',
                'job_id': str(row.get('id')),
                'role': role,
                'error': str(exc),
                'action': 'continue_enforcement',
            }, ensure_ascii=False))
    return {'stale_recovered': recovered, 'healthy_active_jobs': healthy, 'watchdog_degraded': False}


def _enqueue_fixed(role: str, *, department_role: str | None = None, phase: str | None = None,
                   target: int | None = None, priority_boost: int = 0, task_suffix: str = '') -> str:
    dept = str(department_role or '').lower()
    if dept == 'lead':
        # Deterministic Playwright V3 is now repaired and must remain zero-LLM.
        phase = 'research_v3'
        task_suffix = v2.LEAD_RESEARCH_PROMPT
    elif dept == 'engagement':
        task_suffix = v2.ENGAGEMENT_PROMPT
    elif dept == 'outreach':
        task_suffix = v2.OUTREACH_PROMPT
    elif dept == 'dm_outreach':
        task_suffix = v2.DM_PROMPT
    return _enqueue_high_priority(
        role, department_role=department_role, phase=phase, target=target,
        priority_boost=priority_boost, task_suffix=task_suffix,
    )


def _late_day_enforce() -> dict:
    """Keep closing today's gaps after the legacy 20:00 business-hours cutoff.

    The CEO may continue corrective work until 23:00 Berlin time. Safety/platform
    gates inside the actual workers still have precedence.
    """
    metrics = v2.airtable_today_v2()
    targets = {**HARD_DAILY_TARGETS, 'reply_actions': max(metrics['replies_received'], metrics['positive_replies'])}
    gaps = base.gaps(metrics, targets)
    jobs = base.existing_active_jobs()
    active = base.active_department_roles(jobs)
    created: list[dict[str, str]] = []

    if gaps['reply_actions'] > 0 and 'inbox' not in active:
        created.append({'role': 'inbox', 'job_id': _enqueue_fixed('inbox', department_role='inbox', priority_boost=30)})
        active.add('inbox')
    if gaps['qualified_leads_found'] > 0 and 'lead' not in active:
        created.append({'role': 'lead', 'job_id': _enqueue_fixed(
            'lead', department_role='lead', phase='research_v3', target=min(20, gaps['qualified_leads_found']), priority_boost=20)})
        active.add('lead')
    if gaps['connection_requests'] > 0 and 'outreach' not in active:
        created.append({'role': 'outreach', 'job_id': _enqueue_fixed(
            'growth', department_role='outreach', target=gaps['connection_requests'], priority_boost=15)})
        active.add('outreach')
    if gaps['direct_messages'] > 0 and 'dm_outreach' not in active:
        created.append({'role': 'dm_outreach', 'job_id': _enqueue_fixed(
            'growth', department_role='dm_outreach', target=gaps['direct_messages'], priority_boost=12)})
        active.add('dm_outreach')
    if gaps['engagement_actions'] > 0 and 'engagement' not in active:
        created.append({'role': 'engagement', 'job_id': _enqueue_fixed(
            'growth', department_role='engagement', target=gaps['engagement_actions'], priority_boost=10)})
        active.add('engagement')

    result = {
        'status': 'late_day_enforcement',
        'checked_at': datetime.now(ZoneInfo('Europe/Berlin')).isoformat(),
        'daily_metrics': metrics, 'daily_targets': targets, 'gaps': gaps,
        'created_jobs': created, 'active_roles_after_check': sorted(active),
        'funnel_order': ['DISCOVERED','QUALIFIED','ENGAGE','ENGAGED','CONNECTION_READY','CONNECTION_SENT','CONNECTED','CONVERSATION_STARTED','INTERESTED','CHECK_OFFERED','CHECK_ACCEPTED','TRIAL','PAID'],
    }
    base.save(result)
    return result


def main() -> None:
    recovery = recover_stale_quota_jobs()
    print(json.dumps({'quota_recovery': recovery}, ensure_ascii=False))
    base.DAILY_TARGETS = dict(HARD_DAILY_TARGETS)

    # Replace V2's legacy enqueue wrapper so lead recovery uses deterministic research
    # and all escalation priorities follow the global larger-number-first invariant.
    v2._original_enqueue = _enqueue_high_priority
    v2.enqueue_v2 = _enqueue_fixed

    hour = datetime.now(ZoneInfo('Europe/Berlin')).hour
    if 8 <= hour < 20:
        v2.main()
    elif 20 <= hour < 23:
        print(json.dumps(_late_day_enforce(), ensure_ascii=False))
    else:
        metrics = v2.airtable_today_v2()
        result = {'status':'outside_corrective_window','daily_metrics':metrics,'daily_targets':HARD_DAILY_TARGETS,'checked_at':datetime.now(ZoneInfo('Europe/Berlin')).isoformat()}
        base.save(result)
        print(json.dumps(result, ensure_ascii=False))


if __name__ == '__main__':
    main()
