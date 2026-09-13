from __future__ import annotations

import json
import urllib.parse
from datetime import datetime
from zoneinfo import ZoneInfo

import agent.linkedin_execution_enforcer as base

PEOPLE = 'tbloK0jz2X6ffr0D2'
INTERACTIONS = 'tbltGdYCuYsEZEghp'

LEAD_RESEARCH_PROMPT = r'''

HARD DAILY LEAD RESEARCH OVERRIDE — AGENT 8 / CEO QUOTA
This job exists because today's verified new-lead target is not yet met.
- Do NOT treat an empty Daily Growth Queue as a reason to stop. The queue is not the source of prospecting candidates.
- Use LinkedIn Sales Navigator directly and research the requested number of NEW suitable DACH decision-maker profiles.
- Use the current learning/recommended query as a starting hypothesis, but if a search produces no candidates, immediately try another relevant current query/segment instead of ending the run.
- For each candidate, verify role/fit in LinkedIn/Sales Navigator, then search Airtable People by name or LinkedIn URL BEFORE creating anything.
- Only new, relevant profiles count. Create each accepted new person with airtable_create_person and a concise evidence-based why_suitable/personalization note. No invented facts.
- Do not send connection requests or DMs in this research-only quota job; separate outbound jobs own those actions.
- Continue until target_new_profiles is reached or a real LinkedIn security/capability blocker prevents further research.
- Never bypass CAPTCHA, checkpoint, 2FA, warnings or rate limits.
'''

ENGAGEMENT_PROMPT = r'''

HARD DAILY ENGAGEMENT OVERRIDE — AGENT 8 / CEO QUOTA
This job exists because today's verified LinkedIn comment/engagement target is not yet met.
- An empty Daily Growth Queue is NOT a blocker and is NOT a reason to finish with zero actions.
- Find suitable recent German/DACH posts directly on LinkedIn from qualified target people or through a focused search around Local SEO, Google Business Profile, Google Maps, local customer acquisition, SaaS or relevant local-business topics.
- Before acting on a person, use Airtable People search to check duplicates, Do Not Contact, ownership and recent interaction history.
- Publish only a genuinely useful, specific, natural comment. No generic praise, pitch, link, LOCENIX mention, company-name stuffing, gendering or invented facts.
- A missing Airtable Content Opportunity creation tool is NOT a blocker. After a technically confirmed comment, log the executed evidence with airtable_log_interaction using Interaction Type=Comment, sent=true, exact text and the matched People record. If an existing Content Opportunity is available, use it as context, but it is not a prerequisite for a valid action.
- Only technically confirmed LinkedIn actions count. Do not manufacture activity to hit quota.
- Continue until the requested target_actions is reached or a genuine security/capability blocker occurs.
- Never bypass CAPTCHA, checkpoint, 2FA, warnings or rate limits.
'''

OUTREACH_PROMPT = r'''

HARD DAILY CONNECTION-REQUEST OVERRIDE — AGENT 8 / CEO QUOTA
This job exists because today's verified connection-request target is not yet met.
- An empty Daily Growth Queue is NOT a blocker. Source suitable candidates from Airtable People (RESEARCHED/READY_TO_CONTACT) and/or current Sales Navigator results.
- Work one person at a time. Before each request, search Airtable People by exact name/LinkedIn URL and verify Do Not Contact=false, no prior/pending request, no duplicate, suitable role/fit and no conflicting Owner Agent.
- Send a connection request only when the profile is genuinely suitable. Use no note unless a short individual note clearly improves context; never use a generic pitch.
- After LinkedIn technically confirms the request, update the People record to CONNECTION_SENT, Connection Request Sent=true and log an Interaction with Interaction Type=Connection Request, sent=true.
- Never count or log an action as sent without technical confirmation. Never retry an uncertain send state.
- Continue until target_actions is reached or a real security/capability blocker occurs.
- Never bypass CAPTCHA, checkpoint, 2FA, warnings or rate limits.
'''

DM_PROMPT = r'''

HARD DAILY INDIVIDUAL-DM OVERRIDE — AGENT 8 / CEO QUOTA
This job exists because today's verified direct-message target is not yet met.
- An empty Daily Growth Queue is NOT a blocker. Find suitable messageable candidates through Airtable People and current LinkedIn/Sales Navigator context.
- Only message a suitable person who is already connected or legitimately messageable through an open profile/InMail path allowed by the account.
- Before each message search Airtable People by exact name/LinkedIn URL and verify Do Not Contact=false, no duplicate first-contact DM, no conflicting owner, and current conversation history.
- Each message must be individual, short and based on observed context. No bulk copy, invented facts, company-name stuffing, gendering, link dump, or asking for an email address.
- If there is an inbound reply, stop prospecting that person and hand it to Inbox instead.
- After LinkedIn technically confirms the DM, update the People record appropriately and log an Interaction with Interaction Type=DM, sent=true and the exact message.
- Continue until target_actions is reached or a real security/capability blocker occurs.
- Never bypass CAPTCHA, checkpoint, 2FA, warnings or rate limits.
'''


def _all_people_today(today: str) -> int:
    url = f'https://api.airtable.com/v0/{base.AIRTABLE_BASE_ID}/{PEOPLE}?pageSize=100'
    count = 0
    offset = ''
    while True:
        current = url + (f'&offset={urllib.parse.quote(offset)}' if offset else '')
        data = base.get_json(current, base.airtable_headers())
        for rec in (data or {}).get('records', []):
            created = str(rec.get('createdTime') or '')[:10]
            f = rec.get('fields') or {}
            if created != today:
                continue
            if bool(f.get('Do Not Contact')) or str(f.get('Contact Status') or '').upper() == 'DO_NOT_CONTACT':
                continue
            if not str(f.get('LinkedIn URL') or '').strip():
                continue
            count += 1
        offset = str((data or {}).get('offset') or '')
        if not offset:
            break
    return count


def airtable_today_v2() -> dict[str, int]:
    today = datetime.now(ZoneInfo('Europe/Berlin')).date().isoformat()
    totals = {
        'qualified_leads_found': 0,
        'engagement_actions': 0,
        'connection_requests': 0,
        'direct_messages': 0,
        'reply_actions': 0,
        'replies_received': 0,
        'positive_replies': 0,
        'active_conversations': 0,
    }

    # Keep legacy run reports as evidence, but never depend on them exclusively.
    formula = urllib.parse.quote(f"IS_SAME({{Run Time}}, '{today}', 'day')")
    report_url = f'https://api.airtable.com/v0/{base.AIRTABLE_BASE_ID}/{base.RUN_REPORTS}?pageSize=100&filterByFormula={formula}'
    report_data = base.get_json(report_url, base.airtable_headers())
    report_qualified = 0
    report_connections = 0
    for rec in (report_data or {}).get('records', []):
        f = rec.get('fields') or {}
        report_qualified += int(f.get('Qualified Leads Found') or 0)
        report_connections += int(f.get('Connection Requests') or 0)
        totals['replies_received'] += int(f.get('Replies Received') or 0)
        totals['positive_replies'] += int(f.get('Positive Replies') or 0)
        totals['active_conversations'] = max(totals['active_conversations'], int(f.get('Active Conversations') or 0))

    # New People records are direct evidence that the Sales Navigator research target moved.
    try:
        people_created = _all_people_today(today)
    except Exception:
        people_created = 0
    totals['qualified_leads_found'] = max(report_qualified, people_created)

    # Executed Interactions are the primary proof for comments, requests, DMs and replies.
    interaction_formula = urllib.parse.quote(f"AND(IS_SAME({{Date}}, '{today}', 'day'), {{Sent?}}=1)")
    interaction_url = f'https://api.airtable.com/v0/{base.AIRTABLE_BASE_ID}/{INTERACTIONS}?pageSize=100&filterByFormula={interaction_formula}'
    interaction_data = base.get_json(interaction_url, base.airtable_headers())
    comment_count = 0
    connection_count = 0
    for rec in (interaction_data or {}).get('records', []):
        kind = str((rec.get('fields') or {}).get('Interaction Type') or '').strip()
        if kind == 'Comment':
            comment_count += 1
        elif kind in {'Connection Request', 'LinkedIn Connection Request'}:
            connection_count += 1
        elif kind == 'DM':
            totals['direct_messages'] += 1
        elif kind in {'DM Reply', 'Reply'}:
            totals['reply_actions'] += 1
    totals['engagement_actions'] = comment_count
    totals['connection_requests'] = max(report_connections, connection_count)
    return totals


_original_enqueue = base.enqueue


def enqueue_v2(role: str, *, department_role: str | None = None, phase: str | None = None, target: int | None = None, priority_boost: int = 0, task_suffix: str = '') -> str:
    dept = str(department_role or '').lower()
    if dept == 'lead':
        # research_v3 currently returns zero candidates on the live Sales Navigator UI.
        # Route CEO quota recovery through the normal browser agent while keeping it research-only.
        phase = 'outreach'
        task_suffix = LEAD_RESEARCH_PROMPT
    elif dept == 'engagement':
        task_suffix = ENGAGEMENT_PROMPT
    elif dept == 'outreach':
        task_suffix = OUTREACH_PROMPT
    elif dept == 'dm_outreach':
        task_suffix = DM_PROMPT
    return _original_enqueue(
        role,
        department_role=department_role,
        phase=phase,
        target=target,
        priority_boost=priority_boost,
        task_suffix=task_suffix,
    )


def main() -> None:
    base.airtable_today = airtable_today_v2
    base.enqueue = enqueue_v2
    base.main()


if __name__ == '__main__':
    main()
