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


def lead_key(f: dict) -> str:
    email = str(f.get("Email") or "").strip().lower()
    lead_id = str(f.get("Lead ID") or "").strip().lower()
    website = str(f.get("Website") or "").strip().lower().rstrip("/")
    company = str(f.get("Company") or "").strip().lower()
    return email or lead_id or website or company


def main():
    today = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    data = rows()
    sent: set[str] = set()
    historically_sent: set[str] = {
        str((r.get("fields") or {}).get("Email") or "").strip().lower()
        for r in data
        if (r.get("fields") or {}).get("Email Sent At") and str((r.get("fields") or {}).get("Email") or "").strip()
    }
    ready: set[str] = set()
    needs_copy: set[str] = set()
    qualified_today_keys: set[str] = set()
    qualified_inventory_keys: set[str] = set()

    for r in data:
        f = r.get("fields") or {}
        email = str(f.get("Email") or "").strip().lower()
        created = str(r.get("createdTime") or "")[:10]
        qa = str(f.get("Deep QA Decision") or "").upper()
        tier = str(f.get("Sales Tier") or "").upper()
        is_qualified = qa in {"PASS", "APPROVED", "QUALIFIED", "FINAL_A", "FINAL_B"} or tier in {"A", "B"}
        key = lead_key(f)
        if is_qualified and key:
            qualified_inventory_keys.add(key)
            if created == today:
                qualified_today_keys.add(key)
        if (
            str(f.get("Email Sent At") or "")[:10] == today
            and str(f.get("Email Send Status") or "").upper() == "SENT"
            and f.get("Email Message ID")
            and email
        ):
            sent.add(email)
        if f.get("Email Do Not Contact") or f.get("Intent Do Not Contact") or f.get("Email Sent At") or email in historically_sent:
            continue
        if email and f.get("Email Send Approved"):
            subject = str(f.get("Outreach Subject") or "").strip()
            body = str(f.get("Outreach Draft") or "").strip()
            if str(f.get("Email Send Status") or "").upper() == "READY_TO_SEND" and subject and body:
                ready.add(email)
            elif not subject or not body:
                needs_copy.add(email)

    email_gap = max(0, EMAIL_TARGET - len(sent))
    lead_gap = max(0, LEAD_TARGET - len(qualified_today_keys))
    active = active_sales_roles()

    if email_gap == 0:
        next_action = "EMAIL_TARGET_REACHED"
    elif ready:
        next_action = "EMAIL_SENDER_WORKER"
    elif needs_copy:
        next_action = "EMAIL_COPY_AGENT"
    else:
        next_action = "SALES_LEAD_ENRICHMENT_REQUIRED"

    # research_v3 creates LinkedIn People records, not canonical Sales Leads. Do not
    # pretend that job closes the Sales qualified-lead gap; LinkedIn orchestration owns it.
    status = "TARGET_REACHED" if email_gap == 0 and lead_gap == 0 else "ACTION_REQUIRED"
    print(json.dumps({
        "department": "sales",
        "status": status,
        "target": {"email_sends": EMAIL_TARGET, "qualified_leads": LEAD_TARGET},
        "actual": {
            "email_sends": len(sent),
            "qualified_leads_today_unique": len(qualified_today_keys),
            "qualified_inventory_unique": len(qualified_inventory_keys),
        },
        "gap": {"email_sends": email_gap, "qualified_leads": lead_gap},
        "capacity": {"ready_to_send_unique": len(ready), "approved_needing_copy_unique": len(needs_copy)},
        "active_roles": sorted(active),
        "delegated_jobs": [],
        "next_action": next_action,
        "data_quality": {"raw_records": len(data), "unique_qualified_inventory": len(qualified_inventory_keys)},
        "rule": "Sales Head coordinates workers; counts are deduplicated. LinkedIn research is not counted as canonical Sales pipeline creation.",
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
