from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from agent import intelligence, llm_router
from agent.compliance_gate import email_gate

BASE_ID = os.getenv("AIRTABLE_SALES_BASE_ID", "appuPKnVyLsbWbxMR")
TABLE_ID = os.getenv("AIRTABLE_SALES_TABLE_ID", "tblF4ghkYFzkeQwsT")
TOKEN = (os.getenv("AIRTABLE_PAT") or os.getenv("AIRTABLE_TOKEN") or "").strip()
LIMIT = int(os.getenv("EMAIL_COPY_LIMIT", os.getenv("EMAIL_DAILY_LIMIT", "20")))


def headers():
    if not TOKEN:
        raise RuntimeError("AIRTABLE token missing")
    return {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def request(url, method="GET", data=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data else None, method=method, headers=headers())
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
        return json.loads(raw) if raw else {}


def candidates():
    # Approval is upstream source-of-truth. Copy generation must not require or invent
    # a second authorization field.
    formula = "AND({Email Send Approved}=1,{Email}!='',{Email Sent At}='',OR({Outreach Subject}='',{Outreach Draft}=''))"
    params = urllib.parse.urlencode({"pageSize": min(100, LIMIT), "filterByFormula": formula})
    return request(f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}?{params}").get("records", [])


def draft(fields):
    context = {k: fields.get(k) for k in ("Company", "Industry", "City", "Website", "Primary Pitch", "Why Now", "Proof 1", "Proof 2", "Proof 3")}
    result, _ = intelligence.draft_email({
        **context,
        "instructions": "Natural German, body <=90 words. Ask permission to send useful observations. No pressure.",
    })
    return str(result.get("subject") or "").strip(), str(result.get("body") or "").strip()


def main():
    if not llm_router.configured_slots(llm_router.TASK_PERSONALIZATION):
        print(json.dumps({"agent": "EMAIL_COPY_AGENT", "status": "blocked_no_llm"}))
        return
    prepared, errors, blocked = 0, [], []
    for row in candidates()[:LIMIT]:
        f = row.get("fields") or {}
        gate = email_gate(f, require_copy=False)
        if not gate.allowed:
            blocked.append({"record": row.get("id"), "reason": gate.reason_code})
            continue
        try:
            subject, body = draft(f)
            if not subject or not body:
                raise RuntimeError("empty copy")
            request(
                f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}/{row['id']}",
                "PATCH",
                {"fields": {"Outreach Subject": subject, "Outreach Draft": body, "Email Send Status": "READY_TO_SEND", "Email Send Error": ""}},
            )
            prepared += 1
        except Exception as exc:
            errors.append({"record": row.get("id"), "error": str(exc)[:300]})
    print(json.dumps({"agent": "EMAIL_COPY_AGENT", "status": "complete", "prepared": prepared, "blocked": blocked, "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
