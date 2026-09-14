from __future__ import annotations

from dataclasses import dataclass

@dataclass(frozen=True)
class Gate:
    allowed: bool
    reason_code: str

def email_gate(f:dict)->Gate:
    if f.get("Email Do Not Contact") or f.get("Intent Do Not Contact"): return Gate(False,"DO_NOT_CONTACT")
    if str(f.get("Lead State") or "").upper() in {"NOT_INTERESTED","DO_NOT_CONTACT","INVALID"}: return Gate(False,"TERMINAL_STATE")
    if not str(f.get("Email") or "").strip(): return Gate(False,"NO_EMAIL")
    if not bool(f.get("Email Send Approved")): return Gate(False,"APPROVAL_REQUIRED")
    if not str(f.get("Email Legal Basis") or "").strip(): return Gate(False,"LEGAL_BASIS_REQUIRED")
    if f.get("Email Sent At"): return Gate(False,"ALREADY_CONTACTED")
    if not str(f.get("Outreach Subject") or "").strip() or not str(f.get("Outreach Draft") or "").strip(): return Gate(False,"COPY_REQUIRED")
    if str(f.get("Email Send Status") or "").upper()!="READY_TO_SEND": return Gate(False,"NOT_READY")
    return Gate(True,"ALLOWED")
