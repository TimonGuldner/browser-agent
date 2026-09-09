from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from browser_use import ActionResult, Tools

from agent.airtable_tools import AirtableClient

BERLIN = ZoneInfo("Europe/Berlin")

TABLES = {
    "content": "tblHoBZejp4plB80m",
    "opportunities": "tblbDiGu7EeQxnbx2",
    "metrics": "tblL6eYnihy7hWocv",
    "run_reports": "tblntmY8DXjSJWq8t",
    "brief": "tbl9ZiFsU4iP8kbR1",
    "winning": "tblLnFm3RiW7yiUyU",
    "demand": "tblw3UzKtsVJXDB8D",
    "logs": "tbl93bos6yH0LUhDl",
}

CONTENT_STATUS = {"Idea", "Research", "Draft", "Ready", "Scheduled", "Published", "Analyzed", "Repurpose"}
CONTENT_TYPE = {"Text Post", "Carousel", "Image Post", "Poll", "Document Post", "Company Post"}
CONTENT_PILLAR = {"Local SEO Education", "Google Business Profile", "Mini Audits / Teardowns", "Build LOCENIX", "Founder / Business Insights"}
CONTENT_OBJECTIVE = {"Reach", "Authority", "Engagement", "Connections", "Website Traffic", "Waitlist", "Lead Generation"}
VISUAL_TYPE = {"Checklist Graphic", "Mini Audit Visual", "Build in Public Graphic", "Quote / Statement Graphic", "Carousel", "No Image"}
TARGET_AUDIENCE = {"Agencies", "Webdesigners", "SEO Freelancers", "Local Businesses", "SaaS Founders", "Marketing Professionals"}


def _today() -> str:
    return datetime.now(BERLIN).date().isoformat()


def _now() -> str:
    return datetime.now(BERLIN).isoformat()


def _compact(record: dict[str, Any], fields: list[str]) -> dict[str, Any]:
    values = record.get("fields") or {}
    return {"record_id": record.get("id"), **{k: values.get(k) for k in fields if k in values}}


def _records(table: str, fields: list[str], limit: int = 10) -> list[dict[str, Any]]:
    client = AirtableClient()
    try:
        rows = client.list_records(TABLES[table], max_records=max(1, min(limit, 30)))
        return [_compact(row, fields) for row in rows]
    finally:
        client.close()


def add_extended_airtable_tools(tools: Tools) -> Tools:
    @tools.action(description="Load the shared Daily Growth Brief, Winning Topics, Demand Signals, recent Daily Growth Logs, Content Engine, Content Opportunities, Growth Metrics and Lead Run Reports. Use this as the cross-agent context before strategic decisions.")
    def airtable_load_shared_growth_context(max_per_table: int = 8) -> ActionResult:
        limit = max(1, min(max_per_table, 15))
        payload = {
            "daily_growth_brief": _records("brief", ["Brief Date", "Focus Segment", "Core Pain Theme", "Target Roles", "Winning / Demand Signal", "Content Angle", "Sales Navigator Search Pattern", "Message Experiment Variable", "Relationship Strategy", "Primary Funnel Goal", "Evidence", "Updated By", "Updated At"], limit),
            "winning_topics": _records("winning", ["Winning Topic", "Observed Date", "Signal Source", "Target Segment", "Demand Signal Strength", "Evidence", "Recommended Angle", "Follow-up Questions", "Status"], limit),
            "demand_signals": _records("demand", ["Demand Signal", "Observed Date", "Signal Source", "Source Person", "Source URL", "Target Segment", "Pain Theme", "Objection Theme", "Demand Signal Strength", "Evidence", "Recommended Next Agent", "Next Action"], limit),
            "daily_growth_logs": _records("logs", ["Run Key", "Run Date", "Agent Role", "Run Status", "Summary", "Actions / Metrics", "Main Learning", "Blocker", "Next Action", "Run Time"], limit),
            "content_engine": _records("content", ["Content Title", "Status", "Content Type", "Content Pillar", "Hook", "Final Post", "CTA", "Publish Date", "LinkedIn URL", "Objective", "Target Audience", "AI Learnings", "Visual Type", "Image Prompt"], limit),
            "content_opportunities": _records("opportunities", ["Opportunity", "Post URL", "Post Text / Summary", "Topic", "Why Relevant", "Recommended Comment", "Priority", "Status", "Found Date", "Commented Date", "Result", "Person"], limit),
            "growth_metrics": _records("metrics", ["Date", "Connections", "New Connections", "Followers", "New Followers", "Profile Views", "Posts Published", "Comments Made", "Comments Received", "DMs Sent", "DMs Received", "Website Visits", "LOCENIX Leads", "Trials", "Notes"], limit),
            "lead_run_reports": _records("run_reports", ["Run ID", "Run Time", "Run Type", "Qualified Leads Found", "New Contacts", "Connection Requests", "Replies Received", "Positive Replies", "Active Conversations", "Scanner Offered", "Scanner Link Sent", "Scanner Starts", "Scanner Conversions", "Pain Points", "Learnings", "Next Run Adjustment", "Run Status", "Blocker"], limit),
        }
        return ActionResult(extracted_content=f"Shared LOCENIX growth context: {payload}")

    @tools.action(description="Create or update today's one shared Daily LinkedIn Growth Brief. Updates today's existing brief instead of creating a duplicate.")
    def airtable_upsert_daily_growth_brief(
        focus_segment: str,
        core_pain_theme: str,
        target_roles: str,
        winning_demand_signal: str,
        content_angle: str,
        sales_navigator_search_pattern: str,
        message_experiment_variable: str,
        relationship_strategy: str,
        primary_funnel_goal: str,
        evidence: str,
        updated_by: str,
    ) -> ActionResult:
        day = _today()
        fields = {
            "Brief Date": day,
            "Focus Segment": focus_segment,
            "Core Pain Theme": core_pain_theme,
            "Target Roles": target_roles,
            "Winning / Demand Signal": winning_demand_signal,
            "Content Angle": content_angle,
            "Sales Navigator Search Pattern": sales_navigator_search_pattern,
            "Message Experiment Variable": message_experiment_variable,
            "Relationship Strategy": relationship_strategy,
            "Primary Funnel Goal": primary_funnel_goal,
            "Evidence": evidence,
            "Updated By": updated_by,
            "Updated At": _now(),
        }
        client = AirtableClient()
        try:
            rows = client.list_records(TABLES["brief"], max_records=100)
            existing = next((r for r in rows if (r.get("fields") or {}).get("Brief Date") == day), None)
            if existing:
                saved = client.update_record(TABLES["brief"], existing["id"], fields)
                action = "updated"
            else:
                saved = client.create_record(TABLES["brief"], fields)
                action = "created"
            return ActionResult(extracted_content=f"Daily Growth Brief {action}: record_id={saved.get('id')} date={day}")
        finally:
            client.close()

    @tools.action(description="Log one evidence-backed demand signal. Do not use a Like alone as a strong demand signal and do not invent evidence.")
    def airtable_log_demand_signal(
        demand_signal: str,
        signal_source: str,
        target_segment: str,
        pain_theme: str,
        strength: int,
        evidence: str,
        recommended_next_agent: str,
        next_action: str,
        source_person: str = "",
        source_url: str = "",
        objection_theme: str = "",
    ) -> ActionResult:
        if recommended_next_agent not in {"Inbox", "Lead", "Growth", "Content", "None"}:
            return ActionResult(error="Invalid recommended_next_agent")
        fields: dict[str, Any] = {
            "Demand Signal": demand_signal,
            "Observed Date": _today(),
            "Signal Source": signal_source,
            "Target Segment": target_segment,
            "Pain Theme": pain_theme,
            "Demand Signal Strength": max(1, min(5, int(strength))),
            "Evidence": evidence,
            "Recommended Next Agent": recommended_next_agent,
            "Next Action": next_action,
        }
        if source_person:
            fields["Source Person"] = source_person
        if source_url:
            fields["Source URL"] = source_url
        if objection_theme:
            fields["Objection Theme"] = objection_theme
        client = AirtableClient()
        try:
            saved = client.create_record(TABLES["demand"], fields)
            return ActionResult(extracted_content=f"Demand signal logged: record_id={saved.get('id')}")
        finally:
            client.close()

    @tools.action(description="Log or update a Winning Topic only when there is real evidence. Avoid duplicate topic records for the same active topic.")
    def airtable_upsert_winning_topic(
        winning_topic: str,
        signal_source: str,
        target_segment: str,
        strength: int,
        evidence: str,
        recommended_angle: str,
        status: str,
        follow_up_questions: str = "",
    ) -> ActionResult:
        if status not in {"Possible Signal", "Needs More Observations", "Winning", "Used", "Archived"}:
            return ActionResult(error="Invalid winning topic status")
        client = AirtableClient()
        try:
            rows = client.list_records(TABLES["winning"], max_records=200)
            existing = next((r for r in rows if str((r.get("fields") or {}).get("Winning Topic", "")).strip().lower() == winning_topic.strip().lower() and (r.get("fields") or {}).get("Status") != "Archived"), None)
            fields: dict[str, Any] = {
                "Winning Topic": winning_topic,
                "Observed Date": _today(),
                "Signal Source": signal_source,
                "Target Segment": target_segment,
                "Demand Signal Strength": max(1, min(5, int(strength))),
                "Evidence": evidence,
                "Recommended Angle": recommended_angle,
                "Status": status,
            }
            if follow_up_questions:
                fields["Follow-up Questions"] = follow_up_questions
            if existing:
                saved = client.update_record(TABLES["winning"], existing["id"], fields)
                action = "updated"
            else:
                saved = client.create_record(TABLES["winning"], fields)
                action = "created"
            return ActionResult(extracted_content=f"Winning Topic {action}: record_id={saved.get('id')}")
        finally:
            client.close()

    @tools.action(description="Write one audit/hand-off entry for an agent run. Use a stable run_key; if it already exists the record is updated instead of duplicated.")
    def airtable_log_daily_growth_run(
        run_key: str,
        agent_role: str,
        run_status: str,
        summary: str,
        actions_metrics: str,
        main_learning: str,
        next_action: str,
        blocker: str = "",
    ) -> ActionResult:
        if agent_role not in {"Inbox", "Growth", "Lead", "Content"}:
            return ActionResult(error="Invalid agent_role")
        if run_status not in {"Completed", "Partial", "Blocked", "Failed"}:
            return ActionResult(error="Invalid run_status")
        client = AirtableClient()
        try:
            rows = client.list_records(TABLES["logs"], max_records=300)
            existing = next((r for r in rows if str((r.get("fields") or {}).get("Run Key", "")) == run_key), None)
            fields: dict[str, Any] = {
                "Run Key": run_key,
                "Run Date": _today(),
                "Agent Role": agent_role,
                "Run Status": run_status,
                "Summary": summary,
                "Actions / Metrics": actions_metrics,
                "Main Learning": main_learning,
                "Next Action": next_action,
                "Run Time": _now(),
            }
            if blocker:
                fields["Blocker"] = blocker
            if existing:
                saved = client.update_record(TABLES["logs"], existing["id"], fields)
                action = "updated"
            else:
                saved = client.create_record(TABLES["logs"], fields)
                action = "created"
            return ActionResult(extracted_content=f"Daily Growth Log {action}: record_id={saved.get('id')}")
        finally:
            client.close()

    @tools.action(description="Create a new Content Engine record or update an existing one. Use record_id for an existing post. Never set Published unless LinkedIn publication was technically confirmed. This tool does not create an image.")
    def airtable_save_content(
        content_title: str,
        status: str,
        content_type: str,
        content_pillar: str,
        hook: str,
        final_post: str,
        cta: str,
        objective: str,
        target_audience: str,
        visual_type: str,
        image_prompt: str,
        ai_learnings: str,
        record_id: str = "",
        linkedin_url: str = "",
        publish_date: str = "",
    ) -> ActionResult:
        if status not in CONTENT_STATUS or content_type not in CONTENT_TYPE or content_pillar not in CONTENT_PILLAR or objective not in CONTENT_OBJECTIVE or visual_type not in VISUAL_TYPE:
            return ActionResult(error="Invalid Content Engine select value")
        audiences = [x.strip() for x in target_audience.split(",") if x.strip()]
        if not audiences or any(x not in TARGET_AUDIENCE for x in audiences):
            return ActionResult(error=f"target_audience must be comma-separated values from {sorted(TARGET_AUDIENCE)}")
        if status == "Published" and not linkedin_url:
            return ActionResult(error="Published requires a technically confirmed LinkedIn URL")
        fields: dict[str, Any] = {
            "Content Title": content_title,
            "Status": status,
            "Content Type": content_type,
            "Content Pillar": content_pillar,
            "Hook": hook,
            "Final Post": final_post,
            "CTA": cta,
            "Objective": objective,
            "Target Audience": audiences,
            "Image Needed": visual_type != "No Image",
            "Visual Type": visual_type,
            "Image Prompt": image_prompt,
            "AI Learnings": ai_learnings,
            "Personal / Company": "Personal",
        }
        if linkedin_url:
            fields["LinkedIn URL"] = linkedin_url
        if publish_date:
            fields["Publish Date"] = publish_date
        client = AirtableClient()
        try:
            if record_id:
                saved = client.update_record(TABLES["content"], record_id, fields)
                action = "updated"
            else:
                saved = client.create_record(TABLES["content"], fields)
                action = "created"
            return ActionResult(extracted_content=f"Content Engine {action}: record_id={saved.get('id')} status={status}")
        finally:
            client.close()

    return tools
