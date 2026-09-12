from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASK = Path('tasks/linkedin_department_head.json')
OUT = Path('results/linkedin_department_latest.json')
STATE = Path('results/linkedin_department_state.json')


def load(path: Path, default: Any = None):
    if default is None:
        default = {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except Exception:
        return default


def unknown(v):
    return 'UNKNOWN' if v is None else v


def scan_json_files():
    """Conservative evidence scan. Missing metrics stay UNKNOWN, never become zero."""
    evidence = {}
    for root in (Path('results'), Path('state')):
        if not root.exists():
            continue
        for p in root.glob('*.json'):
            evidence[str(p)] = load(p)
    return evidence


def first_metric(evidence, keys):
    for obj in evidence.values():
        if not isinstance(obj, dict):
            continue
        stack = [obj]
        while stack:
            cur = stack.pop()
            if isinstance(cur, dict):
                for k, v in cur.items():
                    if k in keys and v is not None:
                        return v
                    if isinstance(v, (dict, list)):
                        stack.append(v)
            elif isinstance(cur, list):
                stack.extend(cur)
    return 'UNKNOWN'


def num(v):
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def main():
    cfg = load(TASK)
    if not cfg.get('enabled'):
        print(json.dumps({'status': 'disabled'}))
        return

    evidence = scan_json_files()
    metrics = {
        'leads': first_metric(evidence, {'leads', 'lead_count', 'new_leads'}),
        'qualified_leads': first_metric(evidence, {'qualified_leads', 'qualified_count'}),
        'active_conversations': first_metric(evidence, {'active_conversations', 'open_conversations'}),
        'replies': first_metric(evidence, {'replies', 'reply_count'}),
        'positive_signals': first_metric(evidence, {'positive_signals', 'positive_replies'}),
        'open_followups': first_metric(evidence, {'open_followups', 'open_follow_ups'}),
        'overdue_followups': first_metric(evidence, {'overdue_followups', 'due_followups', 'due_follow_ups'}),
        'visibility_checks_offered': first_metric(evidence, {'visibility_checks_offered'}),
        'visibility_checks_accepted': first_metric(evidence, {'visibility_checks_accepted'}),
        'trials': first_metric(evidence, {'trials'}),
        'paid_customers': first_metric(evidence, {'paid_customers'}),
    }

    priorities = []
    # We do not infer account risk from absence of data. Only explicit observed flags count.
    account_risk = first_metric(evidence, {'account_risk', 'linkedin_account_risk', 'security_checkpoint'})
    if account_risk not in ('UNKNOWN', False, 0, '', None):
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
        priorities.append(('EVIDENCE', 'Collect current LinkedIn funnel evidence; do not manufacture activity while core metrics are unavailable.', 'LinkedIn Department'))

    priorities = priorities[: int(cfg.get('max_priorities', 3))]
    status = 'BLOCKED' if priorities[0][0] == 'SYSTEM_SAFETY' else ('ATTENTION' if priorities[0][0] in {'FOLLOWUPS', 'EVIDENCE'} else 'OPPORTUNITY')
    now = datetime.now(timezone.utc).isoformat()

    def p(i):
        if i >= len(priorities): return None
        typ, action, owner = priorities[i]
        return {'type': typ, 'action': action, 'owner': owner}

    report = {
        'agent': 'AGENT_8_LINKEDIN_DEPARTMENT_HEAD',
        'reports_to': 'AGENT_0_LOCENIX_CEO',
        'last_run_at': now,
        'department_status': status,
        'department_health': status,
        **metrics,
        'system_health': 'RISK_OBSERVED' if priorities[0][0] == 'SYSTEM_SAFETY' else 'NO_OBSERVED_BLOCKER',
        'biggest_bottleneck': priorities[0][0],
        'priority_1': p(0),
        'priority_2': p(1),
        'priority_3': p(2),
        'main_learning': 'UNKNOWN',
        'risks': [priorities[0][1]] if priorities[0][0] == 'SYSTEM_SAFETY' else [],
        'proposed_agents': [],
        'human_decision_required': priorities[0][0] == 'SYSTEM_SAFETY',
        'ceo_escalation_required': priorities[0][0] == 'SYSTEM_SAFETY',
        'ceo_message': priorities[0][1] if priorities[0][0] == 'SYSTEM_SAFETY' else 'No CEO intervention required from currently observed repository evidence.',
        'guardrails': cfg.get('rules', {}),
        'evidence_files_seen': sorted(evidence.keys()),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    STATE.write_text(json.dumps({k: report[k] for k in (
        'last_run_at','department_status','department_health','biggest_bottleneck','priority_1',
        'leads','qualified_leads','active_conversations','replies','positive_signals','open_followups',
        'overdue_followups','visibility_checks_offered','visibility_checks_accepted','trials','paid_customers',
        'human_decision_required','ceo_escalation_required','ceo_message')}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(report, ensure_ascii=False))

if __name__ == '__main__':
    main()
