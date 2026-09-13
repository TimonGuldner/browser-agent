from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

AIRTABLE_BASE_ID = os.environ["AIRTABLE_SALES_BASE_ID"]
AIRTABLE_TABLE_ID = os.environ.get("AIRTABLE_SALES_TABLE_ID", "tblF4ghkYFzkeQwsT")
AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
DAILY_LIMIT = int(os.environ.get("EMAIL_DAILY_LIMIT", "20"))


def airtable_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def resend_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}


def get_json(url: str, headers: dict[str, str]) -> dict:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def patch_record(record_id: str, fields: dict) -> None:
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record_id}"
    req = urllib.request.Request(
        url,
        data=json.dumps({"fields": fields}).encode("utf-8"),
        method="PATCH",
        headers=airtable_headers(),
    )
    with urllib.request.urlopen(req, timeout=30):
        pass


def send_resend(to: str, subject: str, text: str) -> dict:
    payload = {"to": [to], "subject": subject, "text": text}
    req = urllib.request.Request(
        "https://api.resend.com/emails",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers=resend_headers(),
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def list_candidates() -> list[dict]:
    formula = "AND({Email Send Approved}=1,{Email Send Status}='READY_TO_SEND',{Email Legal Basis}!='',{Email}!='',{Outreach Subject}!='',{Outreach Draft}!='')"
    params = urllib.parse.urlencode({"pageSize": 100, "filterByFormula": formula})
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}?{params}"
    data = get_json(url, airtable_headers())
    return data.get("records", [])


def already_sent_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    formula = f"AND({{Email Sent At}}!='',IS_SAME({{Email Sent At}},'{today}','day'))"
    params = urllib.parse.urlencode({"pageSize": 100, "filterByFormula": formula})
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}?{params}"
    data = get_json(url, airtable_headers())
    unique = set()
    for rec in data.get("records", []):
        email = str((rec.get("fields") or {}).get("Email") or "").strip().lower()
        if email:
            unique.add(email)
    return len(unique)


def main() -> None:
    sent_today = already_sent_today()
    remaining = max(0, DAILY_LIMIT - sent_today)
    if remaining == 0:
        print(json.dumps({"status": "quota_reached", "sent_today": sent_today}))
        return

    candidates = list_candidates()
    seen: set[str] = set()
    sent = []
    skipped = []

    for rec in candidates:
        if len(sent) >= remaining:
            break
        fields = rec.get("fields") or {}
        email = str(fields.get("Email") or "").strip().lower()
        company = str(fields.get("Company") or "").strip().lower()
        dedupe_key = email or company
        if not dedupe_key or dedupe_key in seen:
            skipped.append({"record": rec.get("id"), "reason": "duplicate_or_missing_key"})
            continue
        seen.add(dedupe_key)

        if fields.get("Email Message ID") or fields.get("Email Sent At"):
            skipped.append({"record": rec.get("id"), "reason": "already_sent"})
            continue

        try:
            response = send_resend(email, str(fields["Outreach Subject"]), str(fields["Outreach Draft"]))
            email_id = str(response.get("id") or "")
            if not email_id:
                raise RuntimeError(f"Resend returned no id: {response}")
            now = datetime.now(timezone.utc).isoformat()
            patch_record(rec["id"], {
                "Email Send Status": "SENT",
                "Email Message ID": email_id,
                "Email Sent At": now,
                "Email Send Error": "",
            })
            sent.append({"record": rec["id"], "email": email, "message_id": email_id})
        except Exception as exc:
            patch_record(rec["id"], {"Email Send Error": str(exc)[:900]})
            skipped.append({"record": rec.get("id"), "reason": "send_error", "error": str(exc)})

    print(json.dumps({"status": "completed", "sent_before": sent_today, "sent_now": len(sent), "sent": sent, "skipped": skipped}, ensure_ascii=False))


if __name__ == "__main__":
    main()
