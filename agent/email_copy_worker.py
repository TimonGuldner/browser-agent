from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request

from agent import llm_router

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
    formula = "AND({Email Send Approved}=1,{Email Legal Basis}!='',{Email}!='',{Email Sent At}='',OR({Outreach Subject}='',{Outreach Draft}=''))"
    params = urllib.parse.urlencode({"pageSize": min(100, LIMIT), "filterByFormula": formula})
    return request(f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}?{params}").get("records", [])


def draft(fields):
    context = {k: fields.get(k) for k in ("Company", "Industry", "City", "Website", "Primary Pitch", "Why Now", "Proof 1", "Proof 2", "Proof 3")}
    prompt = f'''Write a short natural German first-contact B2B email. This is the EMAIL COPY AGENT, not Agent 0. Use only verified supplied facts and never invent personalization. Avoid sales-pitch language, pressure, exaggerated claims and gendering. Do not claim something was noticed unless the supplied evidence proves it. The first message should feel like a normal human note and ask permission to send the useful Google Maps/local visibility observations; do not lead with product features or price. Return JSON only with subject and body, body <= 90 words. Context: {json.dumps(context, ensure_ascii=False)}'''
    text, _ = llm_router.text_complete(prompt, task_type=llm_router.TASK_PERSONALIZATION, max_output_tokens=400)
    cleaned = text.strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].lstrip()
    result = json.loads(cleaned)
    return str(result.get("subject") or "").strip(), str(result.get("body") or "").strip()


def main():
    if not llm_router.configured_slots(llm_router.TASK_PERSONALIZATION):
        print(json.dumps({"agent":"EMAIL_COPY_AGENT","status":"blocked_no_llm"}))
        return
    prepared, errors = 0, []
    for row in candidates()[:LIMIT]:
        f = row.get("fields") or {}
        if f.get("Email Do Not Contact") or f.get("Intent Do Not Contact"):
            continue
        try:
            subject, body = draft(f)
            if not subject or not body:
                raise RuntimeError("empty copy")
            request(f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}/{row['id']}", "PATCH", {"fields": {"Outreach Subject": subject, "Outreach Draft": body, "Email Send Status": "READY_TO_SEND", "Email Send Error": ""}})
            prepared += 1
        except Exception as exc:
            errors.append({"record": row.get("id"), "error": str(exc)[:300]})
    print(json.dumps({"agent":"EMAIL_COPY_AGENT","status":"complete","prepared":prepared,"errors":errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
