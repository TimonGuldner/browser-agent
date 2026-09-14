from __future__ import annotations

"""CFO hard-budget hooks used before any paid model/tool call."""

import json
import os
import urllib.error
import urllib.request
from decimal import Decimal
from typing import Any, Mapping


class BudgetBlocked(RuntimeError):
    pass


def _rpc(name: str, payload: Mapping[str, Any]) -> Any:
    url = os.getenv("SUPABASE_URL", "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise BudgetBlocked("CFO_GUARD_UNAVAILABLE: Supabase service-role configuration missing")
    req = urllib.request.Request(
        f"{url}/rest/v1/rpc/{name}", data=json.dumps(dict(payload), default=str).encode(), method="POST",
        headers={"apikey": key, "Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as response:
            body = response.read().decode()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:1200]
        raise BudgetBlocked(f"CFO_GUARD_ERROR: HTTP {exc.code}: {detail}") from exc


def authorize_and_book_estimate(*, provider: str, service: str, amount_eur: Decimal | str,
                                task_type: str, model_tier: str = "small",
                                difficult_decision: bool = False, metadata: Mapping[str, Any] | None = None) -> str:
    """Reserve and conservatively book a per-call upper bound before execution.

    Until provider invoices expose exact EUR cost, booked values are explicitly
    marked upper_bound_booking. This fails closed and never reports free usage.
    """
    amount = Decimal(str(amount_eur))
    auth = _rpc("company_authorize_spend", {
        "p_amount_eur": str(amount), "p_category": "llm", "p_provider": provider,
        "p_service": service, "p_reason": f"preflight:{task_type}",
        "p_run_id": os.getenv("LOCENIX_RUN_ID") or None,
        "p_job_id": os.getenv("LOCENIX_TASK_ID") or None,
        "p_agent_id": os.getenv("LOCENIX_AGENT_ID", "CFO_GUARD"),
        "p_department": os.getenv("LOCENIX_DEPARTMENT", "CFO"),
        "p_model_tier": model_tier, "p_difficult_decision": difficult_decision,
        "p_metadata": {"accounting_basis": "upper_bound_booking", **dict(metadata or {})},
    })
    if not auth or not auth.get("allowed"):
        raise BudgetBlocked(f"CFO_BUDGET_BLOCKED: {(auth or {}).get('reason', 'UNKNOWN')}")
    reservation = str(auth["reservation_id"])
    dedupe = f"llm:{reservation}"
    _rpc("company_record_cost", {
        "p_reservation_id": reservation, "p_amount_eur": str(amount), "p_dedupe_key": dedupe,
        "p_units": {"accounting_basis": "upper_bound_booking", "task_type": task_type},
    })
    return reservation
