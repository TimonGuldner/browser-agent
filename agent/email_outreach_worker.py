from __future__ import annotations

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

AIRTABLE_BASE_ID = os.environ["AIRTABLE_SALES_BASE_ID"]
AIRTABLE_TABLE_ID = os.environ.get("AIRTABLE_SALES_TABLE_ID", "tblF4ghkYFzkeQws5T")
# Backward-compatible correction for the actual production table ID.
if AIRTABLE_TABLE_ID == "tblF4ghkYFzkeQws5T":
    AIRTABLE_TABLE_ID = "tblF4ghkYFzkeQwsT"
AIRTABLE_TOKEN = os.environ["AIRTABLE_TOKEN"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Timon Guldner <hello@locenix.com>")
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "hello@locenix.com")
DAILY_LIMIT = int(os.environ.get("EMAIL_DAILY_LIMIT", "20"))


def airtable_headers():
    return {"Authorization": f"Bearer {AIRTABLE_TOKEN}", "Content-Type": "application/json"}


def resend_headers():
    return {"Authorization": f"Bearer {RESEND_API_KEY}", "Content-Type": "application/json"}


def get_json(url, headers):
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def patch_record(record_id, fields):
    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}/{record_id}"
    req = urllib.request.Request(url, data=json.dumps({"fields": fields}).encode(), method="PATCH", headers=airtable_headers())
    with urllib.request.urlopen(req, timeout=30):
        pass


def send_resend(to, subject, text):
    payload = {
        "from": EMAIL_FROM,
        "to": [to],
        "reply_to": [EMAIL_REPLY_TO],
        "subject": subject,
        "text": text,
    }
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=json.dumps(payload).encode(), method="POST", headers=resend_headers(),
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode())


def list_candidates():
    formula = "AND({Email Send Approved}=1,{Email Send Status}='READY_TO_SEND',{Email Legal Basis}!='',{Email}!='',{Outreach Subject}!='',{Outreach Draft}!='')"
    params = urllib.parse.urlencode({"pageSize": 100, "filterByFormula": formula})
    return get_json(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}?{params}", airtable_headers()).get("records", [])


def all_sent_emails() -> set[str]:
    # Any previous provider submission is kept out of automatic retries, including bounces.
    # A bounced/failed address must be investigated rather than automatically resent.
    formula = "{Email Sent At}!=''"
    params = urllib.parse.urlencode({"pageSize": 100, "filterByFormula": formula})
    rows = get_json(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}?{params}", airtable_headers()).get("records", [])
    return {
        str((r.get("fields") or {}).get("Email") or "").strip().lower()
        for r in rows if (r.get("fields") or {}).get("Email")
    }


def sent_today_count() -> int:
    # Daily quota counts only technically confirmed records that are still SENT.
    # FAILED/bounced records may retain Sent At for audit but must not satisfy the KPI.
    today = datetime.now(timezone.utc).date().isoformat()
    formula = (
        f"AND({{Email Sent At}}!='',IS_SAME({{Email Sent At}},'{today}','day'),"
        "{Email Send Status}='SENT',{Email Message ID}!='')"
    )
    params = urllib.parse.urlencode({"pageSize": 100, "filterByFormula": formula})
    rows = get_json(f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}?{params}", airtable_headers()).get("records", [])
    return len({
        str((r.get("fields") or {}).get("Email") or "").strip().lower()
        for r in rows if (r.get("fields") or {}).get("Email")
    })


def main():
    sent_today = sent_today_count()
    remaining = max(0, DAILY_LIMIT - sent_today)
    if remaining == 0:
        print(json.dumps({"status": "quota_reached", "sent_today": sent_today}))
        return

    historically_sent = all_sent_emails()
    seen = set()
    sent = []
    errors = []

    for rec in list_candidates():
        if len(sent) >= remaining:
            break
        f = rec.get("fields") or {}
        email = str(f.get("Email") or "").strip().lower()
        company = str(f.get("Company") or "").strip().lower()
        key = email or company
        if not key or key in seen or email in historically_sent:
            continue
        seen.add(key)

        try:
            out = send_resend(email, str(f["Outreach Subject"]), str(f["Outreach Draft"]))
            message_id = str(out.get("id") or "")
            if not message_id:
                raise RuntimeError(f"No Resend id returned: {out}")
            patch_record(rec["id"], {
                "Email Send Status": "SENT",
                "Email Message ID": message_id,
                "Email Sent At": datetime.now(timezone.utc).isoformat(),
                "Email Send Error": "",
            })
            historically_sent.add(email)
            sent.append({"record": rec["id"], "email": email, "message_id": message_id})
        except Exception as exc:
            try:
                patch_record(rec["id"], {"Email Send Status": "FAILED", "Email Send Error": str(exc)[:900]})
            except Exception:
                pass
            errors.append({"record": rec.get("id"), "error": str(exc)})

    print(json.dumps({"status": "completed", "sent_before": sent_today, "sent_now": len(sent), "sent": sent, "errors": errors}, ensure_ascii=False))


if __name__ == "__main__":
    main()
