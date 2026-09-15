from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class Gate:
    allowed: bool
    reason_code: str


def email_gate(f: dict, *, require_copy: bool = True) -> Gate:
    """Deterministic technical send gate.

    Email Send Approved is the source-of-truth business authorization produced by
    the upstream review/research process. We do not invent or infer that approval.
    DNC, terminal states, duplicate/history and missing technical requirements
    remain hard stops.
    """
    if f.get("Email Do Not Contact") or f.get("Intent Do Not Contact") or f.get("Do Not Contact"):
        return Gate(False, "DO_NOT_CONTACT")
    if str(f.get("Lead State") or "").upper() in {"NOT_INTERESTED", "DO_NOT_CONTACT", "INVALID"}:
        return Gate(False, "TERMINAL_STATE")
    if not str(f.get("Email") or "").strip():
        return Gate(False, "NO_EMAIL")
    if not bool(f.get("Email Send Approved")):
        return Gate(False, "APPROVAL_REQUIRED")
    # Run 3 requires a documented route, not a researched address or checkbox alone.
    from agent.growth_execution import permitted_route
    permitted, reason = permitted_route(f)
    if not permitted:
        return Gate(False, reason)
    if f.get("Email Sent At"):
        return Gate(False, "ALREADY_CONTACTED")
    if require_copy:
        if not str(f.get("Outreach Subject") or "").strip() or not str(f.get("Outreach Draft") or "").strip():
            return Gate(False, "COPY_REQUIRED")
        if str(f.get("Email Send Status") or "").upper() != "READY_TO_SEND":
            return Gate(False, "NOT_READY")
    return Gate(True, "ALLOWED")
