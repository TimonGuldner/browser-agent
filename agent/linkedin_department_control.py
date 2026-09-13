from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

DEPARTMENT = Path('results/linkedin_department_state.json')
EXECUTION = Path('results/linkedin_execution_state.json')
STATE = Path('results/linkedin_control_state.json')
LATEST = Path('results/linkedin_control_latest.json')


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save(path: Path, obj: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def expected_by_now(targets: dict, now: datetime) -> dict[str, int]:
    hour = now.hour + now.minute / 60
    if hour >= 18:
        fraction = 1.0
    else:
        fraction = max(0.0, min(1.0, (hour - 8.0) / 10.0))
    expected = {}
    for key, value in targets.items():
        try:
            target = max(0, int(value))
        except Exception:
            continue
        if target == 0:
            expected[key] = 0
        elif fraction <= 0:
            expected[key] = 0
        else:
            expected[key] = min(target, int(math.ceil(target * fraction)))
    return expected


def main() -> None:
    department = load(DEPARTMENT)
    execution = load(EXECUTION)
    now_utc = datetime.now(timezone.utc)
    now_berlin = datetime.now(ZoneInfo('Europe/Berlin'))
    now = now_utc.isoformat()

    if execution.get('status') == 'outside_business_hours':
        status = 'OUTSIDE_BUSINESS_HOURS'
        targets = execution.get('daily_targets') or {}
        actual = {}
        gaps = {}
        expected = {}
        pacing_gaps = {}
        action = 'No new external LinkedIn work is started outside the configured business-hours window.'
        attention = False
    else:
        targets = execution.get('daily_targets') or {}
        actual = execution.get('daily_metrics') or {}
        gaps = execution.get('gaps') or {
            key: max(0, int(value) - int(actual.get(key) or 0))
            for key, value in targets.items()
        }
        expected = expected_by_now(targets, now_berlin)
        pacing_gaps = {
            key: max(0, int(expected_value) - int(actual.get(key) or 0))
            for key, expected_value in expected.items()
        }
        created = execution.get('created_jobs') or []
        has_gap = any(int(v or 0) > 0 for v in gaps.values())
        behind_pace = any(int(v or 0) > 0 for v in pacing_gaps.values())
        blocked = str(department.get('department_status', '')).upper() == 'BLOCKED' or department.get('account_risk') is True
        if blocked:
            status = 'BLOCKED'
            action = 'Stop risky external work and resolve the LinkedIn account/security blocker.'
            attention = True
        elif not has_gap:
            status = 'ON_TARGET'
            action = 'Daily LinkedIn targets are reached. Prioritize active conversations and conversion quality over extra volume.'
            attention = False
        elif behind_pace:
            status = 'ATTENTION'
            action = f'LinkedIn is behind today\'s required pace. Close pacing gaps now: {pacing_gaps}. Agent 8 must assign executable work and re-measure.'
            attention = True
        elif created or execution.get('active_roles_after_check'):
            status = 'WORKING'
            action = 'Agent 8 is on pace and has active work assigned against remaining daily gaps; re-measure on the next control run.'
            attention = False
        else:
            status = 'ATTENTION'
            action = 'Daily funnel gaps exist but Agent 8 produced no execution evidence; CEO must require corrective action.'
            attention = True

    control = {
        'controller': 'AGENT_8_LINKEDIN_DEPARTMENT_HEAD',
        'reports_to': 'AGENT_0_LOCENIX_CEO',
        'generated_at': now,
        'control_status': status,
        'north_star': 'PAID_CUSTOMERS_FROM_LINKEDIN',
        'targets': targets,
        'actual': actual,
        'gaps': gaps,
        'expected_by_now': expected,
        'pacing_gaps': pacing_gaps,
        'created_jobs': execution.get('created_jobs') or [],
        'active_roles': execution.get('active_roles_after_check') or [],
        'department_status': department.get('department_status', 'UNKNOWN'),
        'biggest_bottleneck': department.get('biggest_bottleneck', 'UNKNOWN'),
        'priority_1': department.get('priority_1'),
        'funnel_order': execution.get('funnel_order') or [],
        'ceo_attention_required': attention,
        'corrective_action': action,
        'management_rule': 'TARGET -> ACTUAL -> EXPECTED-BY-NOW -> GAP -> ACTION -> RE-MEASURE. Hard daily targets are measured by executed evidence, not workflow health.',
    }
    save(STATE, control)
    save(LATEST, {'control': control, 'department': department, 'execution': execution})
    print(json.dumps(control, ensure_ascii=False))


if __name__ == '__main__':
    main()
