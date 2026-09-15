import os
from datetime import datetime, timedelta, timezone
from typing import Any

from browser_use import Agent as BrowserUseAgent

from agent.airtable_tools import build_airtable_tools
from agent.extended_airtable_tools import add_extended_airtable_tools
from agent.cost_gate import evaluate_inbox_gate, mark_inbox_llm_completed, probe_linkedin_message_badge
from agent import local_worker
from agent import profile_patch
from agent import lead_v3
from agent import llm_router
from agent import cost_control

MONTHLY_OPENAI_BUDGET_USD = max(0.50, float(os.getenv("MONTHLY_LLM_BUDGET_USD", "10")))

ROLE_POLICIES: dict[str, dict[str, Any]] = {
    "inbox": {"max_steps": 30,"max_history_items": 12,"run_budget_usd": 0.12,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 2600,"max_clickable_elements_length": 16000,"use_judge": True,"enable_planning": True},
    "growth": {"max_steps": 28,"max_history_items": 8,"run_budget_usd": 0.15,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 3000,"max_clickable_elements_length": 14000,"use_judge": False,"enable_planning": False},
    "lead": {"max_steps": 30,"max_history_items": 6,"run_budget_usd": 0.12,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 2600,"max_clickable_elements_length": 12000,"use_judge": False,"enable_planning": False},
    "content": {"max_steps": 24,"max_history_items": 8,"run_budget_usd": 0.12,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 3200,"max_clickable_elements_length": 12000,"use_judge": False,"enable_planning": False},
}
DEFAULT_POLICY = {"max_steps": 24,"max_history_items": 8,"run_budget_usd": 0.12,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 2600,"max_clickable_elements_length": 12000,"use_judge": False,"enable_planning": False}

_ACTIVE_SLOT: llm_router.ProviderSlot | None = None
_ACTIVE_TASK_TYPE = llm_router.TASK_BROWSER_REASONING

AIRTABLE_RULES = """

AIRTABLE CRM RULES:
- The Airtable base "LOCENIX LinkedIn Growth OS" is the operational source of truth.
- Before strategic decisions, load the shared growth context so the four roles use the same Daily Growth Brief, Winning Topics, Demand Signals, Content Engine, Content Opportunities, Growth Metrics and run logs.
- Before contacting or researching a person, search Airtable first to avoid duplicates and check Do Not Contact.
- For scheduled work, load the Daily Growth Queue before deciding what is due.
- Load Brand & Profile before drafting outbound messages so positioning and tone stay current.
- After every real LinkedIn action, update the relevant People record and log the Interaction.
- Mark a Daily Growth Queue item Executed only if the external action actually happened.
- Never contact a record marked Do Not Contact.
- Use a stable run key for Daily Growth Logs so retries update rather than duplicate the run.
- If Airtable and the browser disagree, preserve both observations in the CRM and do not invent a successful action.
- Never set Content Engine status to Published without a technically confirmed LinkedIn URL.

COST-EFFICIENT EXECUTION RULES:
- Quality and the Master Prompt remain binding. Save tokens by avoiding redundant work, not by weakening qualification, reasoning or safety.
- Reuse the current tab and current Airtable context instead of re-reading the same records repeatedly.
- Prefer direct navigation and batched actions when the target is already known.
- Keep final reports concise; do not narrate routine browser steps.
- Stop immediately when the run has no meaningful next action.
- Never manufacture activity merely to fill a quota.
- For Lead, research/CRM work comes before outreach. Deterministic research jobs receive the exact remaining target and use zero LLM calls.
- Do not repeatedly reopen the same Sales Navigator result or re-read the same Airtable context in one run.
- Keep only the minimum browser state needed for the current person. Do not carry large prior DOM snapshots forward.
- An LLM response is never proof of an external action. Only technically confirmed LinkedIn evidence may update action KPIs.
"""


def _policy_for_current_role() -> dict[str, Any]:
    role = os.getenv("LOCENIX_AGENT_ROLE", "").strip().lower()
    return ROLE_POLICIES.get(role, DEFAULT_POLICY)


def routed_llm(*args, **kwargs):
    if _ACTIVE_SLOT is None:
        raise RuntimeError("BLOCKED_LLM_PROVIDER: no active routed provider slot")
    policy = _policy_for_current_role()
    return llm_router.create_browser_llm(
        _ACTIVE_SLOT,
        reasoning_effort=str(policy["reasoning_effort"]),
        max_completion_tokens=int(policy["max_completion_tokens"]),
    )


class AirtableEnabledAgent(BrowserUseAgent):
    def __init__(self, *args, **kwargs):
        policy = _policy_for_current_role()
        kwargs.setdefault("llm_timeout", 110)
        kwargs.setdefault("step_timeout", 180)
        kwargs.setdefault("flash_mode", bool(policy["flash_mode"]))
        kwargs.setdefault("use_thinking", bool(policy["use_thinking"]))
        kwargs.setdefault("max_history_items", int(policy["max_history_items"]))
        kwargs.setdefault("message_compaction", True)
        kwargs.setdefault("max_clickable_elements_length", int(policy["max_clickable_elements_length"]))
        kwargs.setdefault("use_judge", bool(policy["use_judge"]))
        kwargs.setdefault("enable_planning", bool(policy["enable_planning"]))
        if os.getenv("AIRTABLE_PAT", "").strip():
            if kwargs.get("tools") is None:
                tools = build_airtable_tools()
                kwargs["tools"] = add_extended_airtable_tools(tools)
            if isinstance(kwargs.get("task"), str):
                kwargs["task"] = kwargs["task"] + AIRTABLE_RULES
        super().__init__(*args, **kwargs)


def _monthly_openai_cost(db) -> float:
    try:
        value = db.rpc("agent_monthly_llm_cost", {}).execute().data
        return float(value or 0)
    except Exception:
        return 0.0


def _provider_failure_text(db, job_id: str, exc: Exception | None = None) -> str:
    parts: list[str] = [str(exc or "")]
    try:
        row = db.table("agent_jobs").select("status,error,result").eq("id", job_id).single().execute().data or {}
        parts.append(str(row.get("error") or ""))
        result = row.get("result") or {}
        parts.append(str(result.get("errors") or ""))
        parts.append(str(result.get("final_result") or ""))
    except Exception:
        pass
    return " ".join(parts)


def _persist_router_metadata(db, job_id: str, task_type: str, attempts: list[dict[str, Any]], active: llm_router.ProviderSlot | None) -> None:
    try:
        row = db.table("agent_jobs").select("result").eq("id", job_id).single().execute().data or {}
        result = row.get("result") or {}
        result.update({
            "llm_task_type": task_type,
            "llm_provider": active.provider if active else None,
            "llm_model": active.model if active else None,
            "llm_key_slot": active.key_slot if active else None,
            "provider_attempts": attempts,
            "provider_failover_used": len(attempts) > 1,
            "provider_failover_reason": attempts[-2].get("error_class") if len(attempts) > 1 else None,
            "estimated_cost_usd": None,
        })
        local_worker.update_job(db, job_id, result=result)
    except Exception:
        pass


_original_run_agent_job = local_worker.run_agent_job


async def cost_optimized_run_agent_job(db, job: dict[str, Any]) -> None:
    global _ACTIVE_SLOT, _ACTIVE_TASK_TYPE
    job_id = str(job["id"])
    input_data = job.get("input") or {}
    role = str(input_data.get("agent_role") or "").strip().lower()
    department_role = str(input_data.get("department_role") or "").strip().lower()
    scheduled = bool(input_data.get("scheduled"))
    pipeline_phase = str(input_data.get("pipeline_phase") or "").strip().lower()
    policy = ROLE_POLICIES.get(role, DEFAULT_POLICY)

    if bool(input_data.get("cost_gate_probe_only")):
        probe = await probe_linkedin_message_badge(db)
        local_worker.update_job(db, job_id, status="completed", result={"probe": probe, "llm_skipped": True, "estimated_cost_usd": 0.0})
        return

    task_type = llm_router.task_type_for_role(role, department_role, pipeline_phase)
    if task_type == llm_router.TASK_DETERMINISTIC:
        await lead_v3.run_deterministic_research(db, job)
        return

    # Do not turn an expected CFO decision into a failed browser job. Keep the
    # canonical task queued for the next daily budget window without consuming
    # an attempt; the transactional spend guard remains authoritative later.
    if not bool(input_data.get("essential_ai")):
        budget_block = cost_control.nonessential_budget_block()
        if budget_block:
            tomorrow = (datetime.now(timezone.utc) + timedelta(days=1)).date()
            defer_until = datetime.combine(tomorrow, datetime.min.time(), tzinfo=timezone.utc) + timedelta(minutes=5)
            local_worker.update_job(
                db, job_id, status="queued", assigned_agent_id=None,
                locked_at=None, locked_by=None, error=None,
                available_at=defer_until.isoformat(),
                result={
                    "budget_deferred": True,
                    "reason": budget_block["reason"],
                    "retry_after": defer_until.isoformat(),
                    "llm_skipped": True,
                    "estimated_cost_usd": 0.0,
                },
            )
            local_worker.add_event(
                db, job_id, "cost_gate.deferred",
                "Nonessential AI browser task deferred to the next budget window",
                {"reason": budget_block["reason"], "retry_after": defer_until.isoformat()},
            )
            return

    if scheduled and role == "inbox":
        gate = await evaluate_inbox_gate(db)
        if gate.get("terminal_blocker"):
            local_worker.update_job(db, job_id, status="failed", error="LinkedIn requires legitimate human verification.", result={"llm_skipped": True, "cost_gate": gate, "estimated_cost_usd": 0.0})
            local_worker.add_event(db, job_id, "cost_gate.blocked", "Inbox gate found a LinkedIn verification blocker", gate)
            return
        if not gate.get("run_llm"):
            local_worker.update_job(db, job_id, status="completed", result={"llm_skipped": True, "no_change": True, "cost_gate": gate, "estimated_cost_usd": 0.0})
            local_worker.add_event(db, job_id, "cost_gate.no_change", "No inbox AI call was needed", gate)
            return

    os.environ["LOCENIX_AGENT_ROLE"] = role
    os.environ["LOCENIX_TASK_ID"] = job_id
    if job.get("run_id"):
        os.environ["LOCENIX_RUN_ID"] = str(job["run_id"])
    os.environ["LOCENIX_DEPARTMENT"] = str(job.get("department") or os.getenv("LOCENIX_DEPARTMENT", "DISTRIBUTION"))
    bounded_job = dict(job)
    bounded_job["max_steps"] = min(int(job.get("max_steps") or policy["max_steps"]), int(policy["max_steps"]))

    slots = llm_router.configured_slots(task_type)
    if not slots:
        raise RuntimeError("BLOCKED_LLM_PROVIDER: no configured provider slot is available")

    openai_cost = _monthly_openai_cost(db)
    if openai_cost >= MONTHLY_OPENAI_BUDGET_USD:
        slots = [slot for slot in slots if slot.provider != "openai"]
    if not slots:
        raise RuntimeError("BLOCKED_LLM_PROVIDER: only OpenAI was configured and its monthly safety budget is exhausted")

    attempts: list[dict[str, Any]] = []
    last_exc: Exception | None = None
    successful_slot: llm_router.ProviderSlot | None = None
    _ACTIVE_TASK_TYPE = task_type

    for index, slot in enumerate(slots):
        _ACTIVE_SLOT = slot
        llm_router.activate_slot(slot)
        local_worker.update_job(db, job_id, status="running", error=None)
        exc: Exception | None = None
        try:
            await _original_run_agent_job(db, bounded_job)
        except Exception as caught:
            exc = caught
            last_exc = caught

        failure_text = _provider_failure_text(db, job_id, exc)
        try:
            state = db.table("agent_jobs").select("status").eq("id", job_id).single().execute().data or {}
            status = str(state.get("status") or "")
        except Exception:
            status = "failed" if exc else "unknown"

        failover_eligible = llm_router.should_failover(failure_text)
        attempts.append({
            "provider": slot.provider,
            "model": slot.model,
            "key_slot": slot.key_slot,
            "status": status,
            "error_class": llm_router.provider_health(failure_text) if status != "completed" else None,
            "failover_eligible": failover_eligible,
        })

        if exc is None and status == "completed":
            successful_slot = slot
            break

        has_next = index + 1 < len(slots)
        if not has_next or not failover_eligible:
            if exc is not None:
                raise exc
            break

        # Only provider/infrastructure errors are retried. The worker never treats a
        # merely incomplete LinkedIn action as a reason to replay the whole job.
        next_slot = slots[index + 1]
        local_worker.add_event(
            db, job_id, "llm.provider_failover",
            f"LLM provider slot {slot.key_slot} failed; retrying with {next_slot.key_slot}.",
            {"from": slot.key_slot, "to": next_slot.key_slot, "error_class": llm_router.provider_health(failure_text)},
        )
        local_worker.update_job(db, job_id, status="running", error=None)

    active = successful_slot or _ACTIVE_SLOT
    _persist_router_metadata(db, job_id, task_type, attempts, active)

    if successful_slot is None and last_exc is not None:
        raise last_exc

    if scheduled and role == "inbox":
        try:
            state = db.table("agent_jobs").select("status").eq("id", job_id).single().execute().data
            if (state or {}).get("status") == "completed":
                mark_inbox_llm_completed(db)
        except Exception:
            pass


local_worker.Agent = AirtableEnabledAgent
local_worker.ChatOllama = routed_llm
local_worker.ensure_ollama = lambda: None
local_worker.OLLAMA_MODEL = "central-llm-router"
local_worker.run_agent_job = cost_optimized_run_agent_job
local_worker.pack_profile = profile_patch.pack_profile
local_worker.unpack_profile = profile_patch.unpack_profile


def main() -> None:
    local_worker.main()


if __name__ == "__main__":
    main()
