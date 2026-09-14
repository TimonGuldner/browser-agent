from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(frozen=True)
class Priority:
    tier: str
    score: int
    reason: str


def score(fields: dict) -> Priority:
    """Deterministic priority only; never invent purchase probability."""
    state = str(fields.get("Lead State") or fields.get("Visibility Status") or "").upper()
    intent = str(fields.get("Intent") or fields.get("Intent Status") or "").upper()
    do_not_contact = bool(fields.get("Email Do Not Contact") or fields.get("Intent Do Not Contact")) or state in {"DO_NOT_CONTACT", "NOT_INTERESTED", "INVALID"}
    if do_not_contact:
        return Priority("BLOCKED", 0, "do_not_contact_or_terminal")
    if state == "PAID":
        return Priority("P0_REVENUE_NOW", 100, "paid_customer")
    if state == "TRIAL":
        return Priority("P0_REVENUE_NOW", 98, "active_trial")
    if state in {"CHECK_REQUESTED", "CHECK_COMPLETED"}:
        return Priority("P0_REVENUE_NOW", 95, "visibility_check_intent")
    if state == "INTERESTED" or intent in {"POSITIVE", "INTERESTED", "HIGH"}:
        return Priority("P0_REVENUE_NOW", 92, "positive_intent")
    if state == "REPLIED":
        return Priority("P1_ACTIVE_CONVERSATION", 82, "active_reply")
    if state in {"CONTACTED", "CHECK_OFFERED"}:
        return Priority("P1_ACTIVE_CONVERSATION", 75, "open_conversation_or_followup")
    if state in {"OUTREACH_READY", "QUALIFIED"}:
        return Priority("P2_QUALIFIED_OUTREACH", 60, "qualified_outreach")
    if state in {"DISCOVERED", "ENRICHED"}:
        return Priority("P3_PIPELINE_CREATION", 40, "pipeline_creation")
    return Priority("P4_GROWTH_SUPPORT", 20, "unclassified_support_work")


def priority_payload(fields: dict) -> dict:
    p = score(fields)
    return {"tier": p.tier, "score": p.score, "reason": p.reason, "scored_at": datetime.now(timezone.utc).isoformat(), "deterministic": True}
