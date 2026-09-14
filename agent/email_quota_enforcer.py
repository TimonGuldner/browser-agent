from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from agent import llm_router

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


def patch_record(record_id: str, fields: dict):
    url = f"https://api.airtable.com/v0/{BASE_ID}/{TABLE_ID}/{record_id}"
    req = urllib.request.Request(url, data=json.dumps({"fields": fields}).encode(), method="PATCH", headers=headers())
    with urllib.request.urlopen(req, timeout=30):
        pass


def all_rows() -> list[dict]:
    rows: list[dict] = []
    offset = ""
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
    seen: set[str] = set()
    for row in rows:
        f = row.get("fields") or {}
        if str(f.get("Email Sent At") or "")[:10] != today:
            continue
        if str(f.get("Email Send Status") or "").upper() != "SENT" or not str(f.get("Email Message ID") or "").strip():
            continue
        email = str(f.get("Email") or "").strip().lower()
        if email:
            seen.add(email)
    return len(seen)


def historically_sent(rows: list[dict]) -> set[str]:
    return {
        str((r.get("fields") or {}).get("Email") or "").strip().lower()
        for r in rows
        if (r.get("fields") or {}).get("Email Sent At") and (r.get("fields") or {}).get("Email")
    }


def eligible(f: dict, sent_before: set[str]) -> bool:
    email = str(f.get("Email") or "").strip().lower()
    if not email or email in sent_before:
        return False
    if not bool(f.get("Email Send Approved")):
        return False
    if not str(f.get("Email Legal Basis") or "").strip():
        return False
    if bool(f.get("Email Do Not Contact")) or bool(f.get("Intent Do Not Contact")):
        return False
    return True


def make_draft(f: dict) -> tuple[str, str] | None:
    if str(f.get("Outreach Subject") or "").strip() and str(f.get("Outreach Draft") or "").strip():
        return str(f["Outreach Subject"]), str(f["Outreach Draft"])
    if not llm_router.configured_slots(llm_router.TASK_PERSONALIZATION):
        return None
    context = {
        "company": f.get("Company"), "industry": f.get("Industry"), "city": f.get("City"),
        "website": f.get("Website"), "primary_pitch": f.get("Primary Pitch"), "why_now": f.get("Why Now"),
        "proof_1": f.get("Proof 1"), "proof_2": f.get("Proof 2"), "proof_3": f.get("Proof 3"),
    }
    prompt = f"""Create a short, natural German B2B outreach email for LOCENIX, a Google Business Profile/local SEO tool. Use only the supplied facts; do not invent claims. No fake personalization, no pressure, no gendering. Offer a free Local Visibility Check as the low-friction next step. Return JSON only with keys subject and body. Keep body under 110 words. Context: {json.dumps(context, ensure_ascii=False)}"""
    text, _ = llm_router.text_complete(prompt, task_type=llm_router.TASK_PERSONALIZATION, max_output_tokens=450)
    cleaned = text.strip().strip("`")
    if cleaned.startswith("json"):
        cleaned = cleaned[4:].lstrip()
    data = json.loads(cleaned)
    subject = str(data.get("subject") or "").strip()
    body = str(data.get("body") or "").strip()
    if not subject or not body:
        return None
    return subject, body


def main():
    rows = all_rows()
    sent = sent_today(rows)
    gap = max(0, DAILY_TARGET - sent)
    sent_before = historically_sent(rows)
    prepared = 0
    already_ready = 0
    draft_errors: list[dict] = []
    eligible_count = 0

    if gap == 0:
        print(json.dumps({"agent": "AGENT_0_EMAIL_ENFORCER", "status": "quota_reached", "sent_today": sent, "target": DAILY_TARGET}))
        return

    for row in rows:
        if prepared + already_ready >= gap:
            break
        f = row.get("fields") or {}
        if not eligible(f, sent_before):
            continue
        eligible_count += 1
        status = str(f.get("Email Send Status") or "").upper()
        subject = str(f.get("Outreach Subject") or "").strip()
        body = str(f.get("Outreach Draft") or "").strip()
        if status == "READY_TO_SEND" and subject and body:
            already_ready += 1
            continue
        try:
            draft = make_draft(f)
            if not draft:
                continue
            subject, body = draft
            patch_record(row["id"], {
                "Outreach Subject": subject,
                "Outreach Draft": body,
                "Email Send Status": "READY_TO_SEND",
                "Email Send Error": "",
            })
            prepared += 1
        except Exception as exc:
            draft_errors.append({"record": row.get("id"), "error": str(exc)[:500]})

    remaining_after_prep = max(0, gap - prepared - already_ready)
    blocker = None
    if remaining_after_prep > 0:
        blocker = (
            "INSUFFICIENT_APPROVED_SENDABLE_LEADS: Agent 0 exhausted records that already have "
            "Email, explicit Email Send Approved, documented Email Legal Basis, and no prior send. "
            "It will not invent approval or a legal basis."
        )
    print(json.dumps({
        "agent": "AGENT_0_EMAIL_ENFORCER", "status": "prepared" if not blocker else "needs_more_approved_leads",
        "target": DAILY_TARGET, "sent_today": sent, "gap_before": gap, "already_ready": already_ready,
        "newly_prepared": prepared, "eligible_seen": eligible_count,
        "remaining_after_prep": remaining_after_prep, "blocker": blocker, "errors": draft_errors,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
