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
TARGET = int(os.getenv("EMAIL_DAILY_LIMIT", "20"))


def headers():
    if not TOKEN: raise RuntimeError("AIRTABLE token missing")
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def rows():
    out, offset = [], ""
    while True:
        p = [("pageSize", "100")]
        if offset: p.append(("offset", offset))
        req = urllib.request.Request(f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}?{urllib.parse.urlencode(p)}", headers=headers())
        with urllib.request.urlopen(req, timeout=30) as r: data = json.loads(r.read())
        out += data.get("records", [])
        offset = data.get("offset", "")
        if not offset: return out


def main():
    today = datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()
    data = rows()
    sent = set(); ready = 0; needs_copy = 0; qualified = 0
    for r in data:
        f = r.get("fields") or {}
        email = str(f.get("Email") or "").strip().lower()
        if str(f.get("Email Sent At") or "")[:10] == today and str(f.get("Email Send Status") or "").upper() == "SENT" and f.get("Email Message ID") and email:
            sent.add(email)
        if f.get("Email Do Not Contact") or f.get("Intent Do Not Contact") or f.get("Email Sent At"):
            continue
        if email and f.get("Email Send Approved") and str(f.get("Email Legal Basis") or "").strip():
            subject, body = str(f.get("Outreach Subject") or "").strip(), str(f.get("Outreach Draft") or "").strip()
            if str(f.get("Email Send Status") or "").upper() == "READY_TO_SEND" and subject and body: ready += 1
            elif not subject or not body: needs_copy += 1
        if str(f.get("Deep QA Decision") or "").upper() in {"PASS", "APPROVED", "QUALIFIED"} or str(f.get("Sales Tier") or "").upper() in {"A", "B"}:
            qualified += 1
    gap = max(0, TARGET-len(sent))
    if gap == 0: action = "TARGET_REACHED"
    elif ready: action = "EMAIL_SENDER_WORKER"
    elif needs_copy: action = "EMAIL_COPY_AGENT"
    else: action = "RESEARCH_ENRICHMENT_PIPELINE"
    print(json.dumps({"department":"sales","status":"TARGET_REACHED" if not gap else "ACTION_REQUIRED","target":{"email_sends":TARGET},"actual":{"email_sends":len(sent),"qualified_inventory":qualified},"gap":{"email_sends":gap},"capacity":{"ready_to_send":ready,"approved_needing_copy":needs_copy},"next_action":action,"rule":"Sales Head coordinates workers; Agent 0 does not perform sales operations."}, ensure_ascii=False))

if __name__ == "__main__": main()
