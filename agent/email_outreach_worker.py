from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from agent.compliance_gate import email_gate

AIRTABLE_BASE_ID = os.environ["AIRTABLE_SALES_BASE_ID"]
AIRTABLE_TABLE_ID = os.environ.get("AIRTABLE_SALES_TABLE_ID", "tblF4ghkYFzkeQwsT")
AIRTABLE_TOKEN = (os.getenv("AIRTABLE_TOKEN") or os.getenv("AIRTABLE_PAT") or "").strip()
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
EMAIL_FROM = os.environ.get("EMAIL_FROM", "Timon Guldner <hello@locenix.com>")
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "hello@locenix.com")
DAILY_LIMIT = int(os.environ.get("EMAIL_DAILY_LIMIT", "20"))


def airtable_headers():
    if not AIRTABLE_TOKEN:
        raise RuntimeError("AIRTABLE_TOKEN/AIRTABLE_PAT missing")
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


def send_resend(to, subject, text, *, idempotency_key=None):
    payload = {"from": EMAIL_FROM, "to": [to], "reply_to": [EMAIL_REPLY_TO], "subject": subject, "text": text}
    req = urllib.request.Request("https://api.resend.com/emails", data=json.dumps(payload).encode(), method="POST", headers={**resend_headers(), **({"Idempotency-Key": idempotency_key} if idempotency_key else {})})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        raise RuntimeError(f"RESEND_HTTP_{exc.code}: {body or exc.reason}") from exc


def list_all(formula: str) -> list[dict]:
    rows: list[dict] = []
    offset = ""
    while True:
        params: list[tuple[str, str]] = [("pageSize", "100"), ("filterByFormula", formula)]
        if offset:
            params.append(("offset", offset))
        payload = get_json(
            f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_TABLE_ID}?{urllib.parse.urlencode(params)}",
            airtable_headers(),
        )
        rows.extend(payload.get("records") or [])
        offset = str(payload.get("offset") or "")
        if not offset:
            return rows


def list_candidates() -> list[dict]:
    formula = "AND({Email Send Approved}=1,{Email Send Status}='READY_TO_SEND',{Email}!='',{Outreach Subject}!='',{Outreach Draft}!='')"
    return list_all(formula)


def all_sent_emails() -> set[str]:
    rows = list_all("{Email Sent At}!=''")
    return {
        str((r.get("fields") or {}).get("Email") or "").strip().lower()
        for r in rows
        if str((r.get("fields") or {}).get("Email") or "").strip()
    }


def sent_today_count() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    formula = f"AND({{Email Sent At}}!='',IS_SAME({{Email Sent At}},'{today}','day'),{{Email Send Status}}='SENT',{{Email Message ID}}!='')"
    rows = list_all(formula)
    return len({
        str((r.get("fields") or {}).get("Email") or "").strip().lower()
        for r in rows
        if str((r.get("fields") or {}).get("Email") or "").strip()
    })


def main():
    sent_today = sent_today_count()
    remaining = max(0, DAILY_LIMIT - sent_today)
    if remaining == 0:
        print(json.dumps({"status": "quota_reached", "sent_today": sent_today}))
        return

    candidates = list_candidates()
    historically_sent = all_sent_emails()
    seen: set[str] = set()
    sent: list[dict] = []
    errors: list[dict] = []
    blocked: list[dict] = []
    duplicate_history_skipped = 0
    duplicate_batch_skipped = 0
    eligible_unique = 0

    for rec in candidates:
        if len(sent) >= remaining:
            break
        f = rec.get("fields") or {}
        gate = email_gate(f)
        if not gate.allowed:
            blocked.append({"record": rec.get("id"), "reason": gate.reason_code})
            continue
        email = str(f.get("Email") or "").strip().lower()
        company = str(f.get("Company") or "").strip().lower()
        key = email or company
        if not key:
            continue
        if email in historically_sent:
            duplicate_history_skipped += 1
            continue
        if key in seen:
            duplicate_batch_skipped += 1
            continue
        seen.add(key)
        eligible_unique += 1
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

    print(json.dumps({
        "status": "completed",
        "sent_before": sent_today,
        "candidate_records": len(candidates),
        "historically_sent_unique": len(historically_sent) - len(sent),
        "duplicate_history_skipped": duplicate_history_skipped,
        "duplicate_batch_skipped": duplicate_batch_skipped,
        "eligible_unique": eligible_unique,
        "sent_now": len(sent),
        "sent": sent,
        "blocked": blocked,
        "errors": errors,
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
