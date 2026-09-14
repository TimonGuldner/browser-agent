from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

BASE_ID = os.getenv("AIRTABLE_SALES_BASE_ID", "appuPKnVyLsbWbxMR")
TABLE_ID = os.getenv("AIRTABLE_SALES_TABLE_ID", "tblF4ghkYFzkeQwsT")
TOKEN = (os.getenv("AIRTABLE_PAT") or os.getenv("AIRTABLE_TOKEN") or "").strip()
EMAIL_TARGET = int(os.getenv("EMAIL_DAILY_LIMIT", "20"))
LEAD_TARGET = int(os.getenv("QUALIFIED_LEADS_DAILY_TARGET", "20"))
SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()


def headers():
    if not TOKEN:
        raise RuntimeError("AIRTABLE token missing")
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def rows():
    out, offset = [], ""
    while True:
        p = [("pageSize", "100")]
        if offset:
            p.append(("offset", offset))
        req = urllib.request.Request(
            f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}?{urllib.parse.urlencode(p)}",
            headers=headers(),
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.loads(r.read())
        out += data.get("records", [])
        offset = data.get("offset", "")
        if not offset:
            return out


def supabase_headers():
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("Supabase configuration missing")
    return {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }


def active_sales_roles() -> set[str]:
    if not SUPABASE_URL or not SUPABASE_KEY:
        return set()
    url = f"{SUPABASE_URL}/rest/v1/agent_jobs?status=in.(queued,running)&select=input&limit=200"
    req = urllib.request.Request(url, headers=supabase_headers())
    with urllib.request.urlopen(req, timeout=30) as r:
        data = json.loads(r.read())
    roles = set()
    for row in data if isinstance(data, list) else []:
        inp = row.get("input") or {}
        role = str(inp.get("department_role") or inp.get("agent_role") or "").strip().lower()
        if role:
            roles.add(role)
    return roles


def enqueue(role: str, task: str, *, target: int, priority: int, phase: str) -> str:
    payload = {
        "status": "queued",
        "task": task,
        "mode": "autonomous",
        "priority": priority,
        "max_steps": 35,
        "input": {
            "agent_role": role,
            "department_role": role,
            "pipeline_phase": phase,
            "target_new_profiles": target,
            "target_actions": target,
            "scheduled": False,
            "source": "sales-head",
            "owner": "SALES_HEAD",
        },
    }
    req = urllib.request.Request(
        f"{SUPABASE_URL}/rest/v1/agent_jobs",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=supabase_headers(),
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        result = json.loads(r.read())
    if not isinstance(result, list) or not result:
        raise RuntimeError(f"Could not enqueue {role}")
    return str(result[0]["id"])


def main():
    today = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    data = rows()
    sent = set()
    ready = 0
    needs_copy = 0
    qualified_today = 0
    qualified_inventory = 0

    for r in data:
        f = r.get("fields") or {}
        email = str(f.get("Email") or "").strip().lower()
        created = str(r.get("createdTime") or "")[:10]
        is_qualified = (
            str(f.get("Deep QA Decision") or "").upper() in {"PASS", "APPROVED", "QUALIFIED"}
            or str(f.get("Sales Tier") or "").upper() in {"A", "B"}
        )
        if is_qualified:
            qualified_inventory += 1
            if created == today:
                qualified_today += 1
        if (
            str(f.get("Email Sent At") or "")[:10] == today
            and str(f.get("Email Send Status") or "").upper() == "SENT"
            and f.get("Email Message ID")
            and email
        ):
            sent.add(email)
        if f.get("Email Do Not Contact") or f.get("Intent Do Not Contact") or f.get("Email Sent At"):
            continue
        if email and f.get("Email Send Approved") and str(f.get("Email Legal Basis") or "").strip():
            subject = str(f.get("Outreach Subject") or "").strip()
            body = str(f.get("Outreach Draft") or "").strip()
            if str(f.get("Email Send Status") or "").upper() == "READY_TO_SEND" and subject and body:
                ready += 1
            elif not subject or not body:
                needs_copy += 1

    email_gap = max(0, EMAIL_TARGET - len(sent))
    lead_gap = max(0, LEAD_TARGET - qualified_today)
    active = active_sales_roles()
    delegated = []

    # Sales Head owns the bottleneck. Create pipeline inventory first when today's
    # qualified-lead target is missing. Existing active jobs make this idempotent.
    if lead_gap and "lead" not in active:
        job_id = enqueue(
            "lead",
            "Deterministically research new LOCENIX prospects for local-service businesses. Verify real business/profile data, deduplicate against Airtable, enrich useful contact/company data where available, and persist only verified records. Do not send outreach and do not invent facts. Stop after the requested number of new profiles or when deterministic sources are exhausted.",
            target=min(20, lead_gap),
            priority=90,
            phase="research_v3",
        )
        delegated.append({"pipeline": "research", "role": "lead", "job_id": job_id, "target": min(20, lead_gap)})
        active.add("lead")

    if email_gap == 0:
        next_action = "EMAIL_TARGET_REACHED"
    elif ready:
        next_action = "EMAIL_SENDER_WORKER"
    elif needs_copy:
        next_action = "EMAIL_COPY_AGENT"
    else:
        next_action = "RESEARCH_ENRICHMENT_PIPELINE"

    status = "TARGET_REACHED" if email_gap == 0 and lead_gap == 0 else "ACTION_REQUIRED"
    print(json.dumps({
        "department": "sales",
        "status": status,
        "target": {"email_sends": EMAIL_TARGET, "qualified_leads": LEAD_TARGET},
        "actual": {
            "email_sends": len(sent),
            "qualified_leads_today": qualified_today,
            "qualified_inventory": qualified_inventory,
        },
        "gap": {"email_sends": email_gap, "qualified_leads": lead_gap},
        "capacity": {"ready_to_send": ready, "approved_needing_copy": needs_copy},
        "active_roles": sorted(active),
        "delegated_jobs": delegated,
        "next_action": next_action,
        "rule": "Sales Head coordinates workers; Agent 0 does not perform sales operations.",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
