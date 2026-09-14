from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

BASE_ID = os.getenv("AIRTABLE_SALES_BASE_ID", "appuPKnVyLsbWbxMR")
TABLE_ID = os.getenv("AIRTABLE_SALES_TABLE_ID", "tblF4ghkYFzkeQwsT")
TOKEN = (os.getenv("AIRTABLE_PAT") or os.getenv("AIRTABLE_TOKEN") or "").strip()
DAILY_TARGET = int(os.getenv("EMAIL_DAILY_LIMIT", "20"))


def headers():
    if not TOKEN:
        raise RuntimeError("AIRTABLE_PAT/AIRTABLE_TOKEN missing")
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def get_json(url: str):
    req = urllib.request.Request(url, headers=headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def all_rows() -> list[dict]:
    rows, offset = [], ""
    while True:
        params = [("pageSize", "100")]
        if offset:
            params.append(("offset", offset))
        payload = get_json(f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}?{urllib.parse.urlencode(params)}")
        rows.extend(payload.get("records") or [])
        offset = str(payload.get("offset") or "")
        if not offset:
            return rows


def sent_today(rows: list[dict]) -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    seen = set()
    for row in rows:
        f = row.get("fields") or {}
        if str(f.get("Email Sent At") or "")[:10] == today and str(f.get("Email Send Status") or "").upper() == "SENT" and str(f.get("Email Message ID") or "").strip():
            email = str(f.get("Email") or "").strip().lower()
            if email:
                seen.add(email)
    return len(seen)


def main():
    rows = all_rows()
    sent = sent_today(rows)
    gap = max(0, DAILY_TARGET - sent)
    historically_sent = {
        str((r.get("fields") or {}).get("Email") or "").strip().lower()
        for r in rows
        if (r.get("fields") or {}).get("Email Sent At") and str((r.get("fields") or {}).get("Email") or "").strip()
    }
    ready_emails: set[str] = set()
    copy_emails: set[str] = set()
    approved_unique: set[str] = set()

    for row in rows:
        f = row.get("fields") or {}
        email = str(f.get("Email") or "").strip().lower()
        if not email or not f.get("Email Send Approved"):
            continue
        if f.get("Email Do Not Contact") or f.get("Intent Do Not Contact") or f.get("Email Sent At") or email in historically_sent:
            continue
        approved_unique.add(email)
        subject = str(f.get("Outreach Subject") or "").strip()
        body = str(f.get("Outreach Draft") or "").strip()
        if str(f.get("Email Send Status") or "").upper() == "READY_TO_SEND" and subject and body:
            ready_emails.add(email)
        elif not subject or not body:
            copy_emails.add(email)

    print(json.dumps({
        "agent": "AGENT_0_EMAIL_CONTROLLER",
        "role": "measure_and_delegate_only",
        "target": DAILY_TARGET,
        "sent_today": sent,
        "gap": gap,
        "approved_unique_unsent": len(approved_unique),
        "ready_to_send_unique": len(ready_emails),
        "approved_needing_copy_unique": len(copy_emails),
        "historically_sent_unique": len(historically_sent),
        "delegation_required": gap > len(ready_emails),
        "delegate_to": "EMAIL_SENDER_WORKER" if ready_emails else ("EMAIL_COPY_AGENT" if copy_emails else "LEAD_EMAIL_PIPELINE"),
        "guardrail": "Email Send Approved is upstream authorization; DNC, prior-contact and technical readiness remain hard stops. Counts are unique by email."
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
