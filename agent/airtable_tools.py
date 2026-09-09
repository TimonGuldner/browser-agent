import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from browser_use import ActionResult, Tools

AIRTABLE_PAT = os.getenv("AIRTABLE_PAT", "").strip()
AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appN6ox7fjGFyXZhL").strip()
AIRTABLE_API_BASE = "https://api.airtable.com/v0"

TABLES = {
    "brand": "tblZrS1SpVkoIFSAG",
    "people": "tbloK0jz2X6ffr0D2",
    "interactions": "tbltGdYCuYsEZEghp",
    "queue": "tblnWGZEjrRD5suj4",
    "run_reports": "tblntmY8DXjSJWq8t",
}

VALID_CONTACT_STATUS = {
    "NEW",
    "RESEARCHED",
    "READY_TO_CONTACT",
    "CONNECTION_SENT",
    "CONNECTED",
    "CONVERSATION_STARTED",
    "DISCOVERY",
    "INTERESTED",
    "CHECK_OFFERED",
    "CHECK_LINK_SENT",
    "CHECK_STARTED",
    "REPORT_VIEWED",
    "EARLY_ACCESS_INTEREST",
    "FOLLOW_UP",
    "NOT_NOW",
    "NOT_INTERESTED",
    "NO_RESPONSE",
    "DO_NOT_CONTACT",
}
VALID_DM_STATUS = {"NOT_SENT", "SENT", "REPLIED", "FOLLOW_UP_SENT", "CLOSED"}
VALID_INTEREST = {"UNKNOWN", "LOW", "MEDIUM", "HIGH"}
VALID_LOCAL_SEO_NEED = {"UNKNOWN", "NONE", "POSSIBLE", "LIKELY", "CONFIRMED"}
VALID_QUEUE_STATUS = {"Planned", "Ready", "Needs Approval", "Approved", "Executed", "Skipped", "Failed"}
VALID_INTERACTION_TYPE = {
    "Comment",
    "Reply",
    "Connection Request",
    "Connection Accepted",
    "DM",
    "DM Reply",
    "Post Mention",
    "Profile Visit",
    "Follow-up",
    "LinkedIn Connection Request",
}


def _require_token() -> str:
    if not AIRTABLE_PAT:
        raise RuntimeError("AIRTABLE_PAT is not configured in GitHub Actions secrets")
    return AIRTABLE_PAT


class AirtableClient:
    def __init__(self) -> None:
        token = _require_token()
        self.client = httpx.Client(
            base_url=AIRTABLE_API_BASE,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            timeout=30,
        )

    def close(self) -> None:
        self.client.close()

    def _path(self, table_id: str, record_id: str | None = None) -> str:
        path = f"/{AIRTABLE_BASE_ID}/{table_id}"
        return f"{path}/{record_id}" if record_id else path

    def list_records(self, table_id: str, *, max_records: int = 100, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        query = dict(params or {})
        query["pageSize"] = min(100, max_records)
        offset: str | None = None
        while len(records) < max_records:
            if offset:
                query["offset"] = offset
            response = self.client.get(self._path(table_id), params=query)
            response.raise_for_status()
            payload = response.json()
            records.extend(payload.get("records", []))
            offset = payload.get("offset")
            if not offset:
                break
        return records[:max_records]

    def get_record(self, table_id: str, record_id: str) -> dict[str, Any]:
        response = self.client.get(self._path(table_id, record_id))
        response.raise_for_status()
        return response.json()

    def create_record(self, table_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        response = self.client.post(self._path(table_id), json={"fields": fields})
        response.raise_for_status()
        return response.json()

    def update_record(self, table_id: str, record_id: str, fields: dict[str, Any]) -> dict[str, Any]:
        response = self.client.patch(self._path(table_id, record_id), json={"fields": fields})
        response.raise_for_status()
        return response.json()


def _compact(record: dict[str, Any], wanted: list[str]) -> dict[str, Any]:
    fields = record.get("fields") or {}
    return {"record_id": record.get("id"), **{name: fields.get(name) for name in wanted if name in fields}}


def _today_berlin() -> str:
    return datetime.now(ZoneInfo("Europe/Berlin")).date().isoformat()


def _validate(value: str | None, allowed: set[str], field_name: str) -> str | None:
    if value is None or value == "":
        return None
    if value not in allowed:
        raise ValueError(f"Invalid {field_name}: {value}. Allowed: {sorted(allowed)}")
    return value


def build_airtable_tools() -> Tools:
    tools = Tools()

    @tools.action(
        description=(
            "Load the LOCENIX Brand & Profile record from Airtable. Use this before writing messages so tone, positioning, "
            "target audience and CTA match the current source of truth."
        )
    )
    def airtable_load_brand_profile() -> ActionResult:
        client = AirtableClient()
        try:
            rows = client.list_records(TABLES["brand"], max_records=3)
            wanted = [
                "Profile Name",
                "LinkedIn URL",
                "Brand",
                "Website",
                "Positioning",
                "Target Audience",
                "Main Promise",
                "Founder Positioning",
                "Tone of Voice",
                "Main CTA",
                "AI Instructions",
            ]
            result = [_compact(row, wanted) for row in rows]
            return ActionResult(extracted_content=f"LOCENIX brand profile: {result}")
        finally:
            client.close()

    @tools.action(
        description=(
            "Load today's LOCENIX Daily Growth Queue from Airtable. Returns queue record IDs, action type, priority, draft, "
            "approval status, linked person and execution state. Use this to decide what work is actually due."
        )
    )
    def airtable_load_daily_queue(date: str = "") -> ActionResult:
        day = date or _today_berlin()
        client = AirtableClient()
        try:
            formula = f"AND({{Date}}='{day}',NOT({{Executed}}))"
            rows = client.list_records(TABLES["queue"], max_records=100, params={"filterByFormula": formula})
            wanted = ["Task", "Date", "Action Type", "Priority", "Suggested Action", "Draft", "Status", "Needs Approval", "Executed", "Result", "Person"]
            result = [_compact(row, wanted) for row in rows]
            return ActionResult(extracted_content=f"Daily Growth Queue for {day}: {result}")
        finally:
            client.close()

    @tools.action(
        description=(
            "Search the LOCENIX People CRM in Airtable by person name, company or LinkedIn URL. Always use this before "
            "contacting someone to avoid duplicates and respect Do Not Contact."
        )
    )
    def airtable_search_people(query: str) -> ActionResult:
        needle = query.strip().lower()
        if not needle:
            return ActionResult(error="Search query must not be empty")
        client = AirtableClient()
        try:
            rows = client.list_records(TABLES["people"], max_records=500)
            scored: list[tuple[int, dict[str, Any]]] = []
            for row in rows:
                fields = row.get("fields") or {}
                haystack = " | ".join(
                    str(fields.get(name, ""))
                    for name in ("Full Name", "First Name", "Last Name", "Company", "LinkedIn URL", "Lead ID")
                ).lower()
                if needle in haystack:
                    score = 100 if needle == str(fields.get("LinkedIn URL", "")).lower() else 50
                    if needle == str(fields.get("Full Name", "")).lower():
                        score += 40
                    scored.append((score, row))
            scored.sort(key=lambda item: item[0], reverse=True)
            wanted = [
                "Full Name",
                "LinkedIn URL",
                "Job Title",
                "Company",
                "Contact Status",
                "DM Status",
                "Last Message From Us",
                "Last Message From Lead",
                "Next Step",
                "Follow-up Date",
                "Interest",
                "Local SEO Need",
                "Scanner Offered",
                "Scanner Link Sent",
                "Conversation Summary",
                "Do Not Contact",
                "Lead Quality Score",
                "Notes",
            ]
            result = [_compact(row, wanted) for _, row in scored[:10]]
            return ActionResult(extracted_content=f"People CRM matches: {result}")
        finally:
            client.close()

    @tools.action(
        description=(
            "Create a new qualified LinkedIn lead in the LOCENIX People CRM. It checks the LinkedIn URL first and will not "
            "create a duplicate. Use only for an actually researched relevant person."
        )
    )
    def airtable_create_person(
        full_name: str,
        linkedin_url: str,
        job_title: str = "",
        company: str = "",
        company_website: str = "",
        location: str = "",
        why_suitable: str = "",
        personalization_note: str = "",
        lead_quality_score: int = 0,
    ) -> ActionResult:
        client = AirtableClient()
        try:
            existing = client.list_records(TABLES["people"], max_records=500)
            normalized = linkedin_url.strip().rstrip("/").lower()
            for row in existing:
                current = str((row.get("fields") or {}).get("LinkedIn URL", "")).strip().rstrip("/").lower()
                if current and current == normalized:
                    return ActionResult(extracted_content=f"Duplicate avoided. Existing Airtable record_id={row.get('id')}")
            lead_id = "li_" + __import__("hashlib").sha1(normalized.encode()).hexdigest()[:16]
            fields: dict[str, Any] = {
                "Full Name": full_name,
                "LinkedIn URL": linkedin_url,
                "Lead ID": lead_id,
                "Contact Status": "RESEARCHED",
                "Source": "LinkedIn Search",
            }
            optional = {
                "Job Title": job_title,
                "Company": company,
                "Company Website": company_website,
                "Location": location,
                "Why Suitable": why_suitable,
                "Personalization Note": personalization_note,
            }
            fields.update({k: v for k, v in optional.items() if v})
            if lead_quality_score:
                fields["Lead Quality Score"] = max(0, min(100, lead_quality_score))
            created = client.create_record(TABLES["people"], fields)
            return ActionResult(extracted_content=f"Created People record_id={created.get('id')} Lead ID={lead_id}")
        finally:
            client.close()

    @tools.action(
        description=(
            "Update one existing LOCENIX People CRM record after research, a connection request, DM, reply or follow-up. "
            "Only pass fields that actually changed. Never clear unrelated fields."
        )
    )
    def airtable_update_person(
        record_id: str,
        contact_status: str = "",
        dm_status: str = "",
        connection_request_sent: bool | None = None,
        last_message_from_us: str = "",
        last_message_from_lead: str = "",
        next_step: str = "",
        follow_up_date: str = "",
        interest: str = "",
        local_seo_need: str = "",
        scanner_offered: bool | None = None,
        scanner_link_sent: bool | None = None,
        conversation_summary: str = "",
        do_not_contact: bool | None = None,
        notes: str = "",
    ) -> ActionResult:
        fields: dict[str, Any] = {}
        if contact_status:
            fields["Contact Status"] = _validate(contact_status, VALID_CONTACT_STATUS, "contact_status")
        if dm_status:
            fields["DM Status"] = _validate(dm_status, VALID_DM_STATUS, "dm_status")
        if connection_request_sent is not None:
            fields["Connection Request Sent"] = connection_request_sent
        if last_message_from_us:
            fields["Last Message From Us"] = last_message_from_us
        if last_message_from_lead:
            fields["Last Message From Lead"] = last_message_from_lead
        if next_step:
            fields["Next Step"] = next_step
        if follow_up_date:
            fields["Follow-up Date"] = follow_up_date
        if interest:
            fields["Interest"] = _validate(interest, VALID_INTEREST, "interest")
        if local_seo_need:
            fields["Local SEO Need"] = _validate(local_seo_need, VALID_LOCAL_SEO_NEED, "local_seo_need")
        if scanner_offered is not None:
            fields["Scanner Offered"] = scanner_offered
        if scanner_link_sent is not None:
            fields["Scanner Link Sent"] = scanner_link_sent
        if conversation_summary:
            fields["Conversation Summary"] = conversation_summary
        if do_not_contact is not None:
            fields["Do Not Contact"] = do_not_contact
            if do_not_contact:
                fields["Contact Status"] = "DO_NOT_CONTACT"
        if notes:
            fields["Notes"] = notes
        if not fields:
            return ActionResult(error="No People fields were supplied to update")
        client = AirtableClient()
        try:
            updated = client.update_record(TABLES["people"], record_id, fields)
            return ActionResult(extracted_content=f"Updated People record_id={updated.get('id')} fields={list(fields.keys())}")
        finally:
            client.close()

    @tools.action(
        description=(
            "Mark one Daily Growth Queue item after it was processed. Use the queue record ID returned by the queue tool. "
            "Set Executed true only when the external action really happened."
        )
    )
    def airtable_update_queue_item(
        record_id: str,
        status: str,
        executed: bool,
        result: str = "",
        draft: str = "",
        needs_approval: bool | None = None,
    ) -> ActionResult:
        _validate(status, VALID_QUEUE_STATUS, "queue status")
        fields: dict[str, Any] = {"Status": status, "Executed": executed}
        if result:
            fields["Result"] = result
        if draft:
            fields["Draft"] = draft
        if needs_approval is not None:
            fields["Needs Approval"] = needs_approval
        client = AirtableClient()
        try:
            updated = client.update_record(TABLES["queue"], record_id, fields)
            return ActionResult(extracted_content=f"Updated queue record_id={updated.get('id')} status={status} executed={executed}")
        finally:
            client.close()

    @tools.action(
        description=(
            "Write a chronological interaction to the LOCENIX Interactions table after a real LinkedIn interaction or when "
            "an incoming message was observed. Link it to the People record ID."
        )
    )
    def airtable_log_interaction(
        person_record_id: str,
        interaction: str,
        interaction_type: str,
        our_response: str = "",
        their_response: str = "",
        sent: bool = False,
        response_received: bool = False,
        ai_summary: str = "",
        next_action: str = "",
        next_action_date: str = "",
    ) -> ActionResult:
        _validate(interaction_type, VALID_INTERACTION_TYPE, "interaction_type")
        fields: dict[str, Any] = {
            "Interaction": interaction,
            "Date": datetime.now(ZoneInfo("Europe/Berlin")).isoformat(),
            "Interaction Type": interaction_type,
            "Sent By": "LOCENIX LinkedIn Agent",
            "Sent?": sent,
            "Response Received?": response_received,
            "Person": [person_record_id],
        }
        optional = {
            "Our Response": our_response,
            "Their Response": their_response,
            "AI Summary": ai_summary,
            "Next Action": next_action,
            "Next Action Date": next_action_date,
        }
        fields.update({k: v for k, v in optional.items() if v})
        client = AirtableClient()
        try:
            created = client.create_record(TABLES["interactions"], fields)
            return ActionResult(extracted_content=f"Logged interaction record_id={created.get('id')}")
        finally:
            client.close()

    return tools
