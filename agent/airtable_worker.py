import os
from typing import Any

from browser_use import Agent as BrowserUseAgent
from browser_use import ChatOpenAI as BrowserUseChatOpenAI

from agent.airtable_tools import build_airtable_tools
from agent.extended_airtable_tools import add_extended_airtable_tools
from agent.cost_gate import evaluate_inbox_gate, mark_inbox_llm_completed, probe_linkedin_message_badge
from agent import local_worker
from agent import profile_patch

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
MONTHLY_LLM_BUDGET_USD = max(0.50, float(os.getenv("MONTHLY_LLM_BUDGET_USD", "10")))

# The large saving comes from skipping unnecessary AI calls. Once AI is needed,
# every customer-facing/strategic role keeps planning, medium reasoning and judging.
ROLE_POLICIES: dict[str, dict[str, Any]] = {
    "inbox": {
        "max_steps": 30,
        "max_history_items": 16,
        "run_budget_usd": 0.25,
        "reasoning_effort": "medium",
        "flash_mode": False,
        "use_thinking": True,
        "max_completion_tokens": 3200,
    },
    "growth": {
        "max_steps": 40,
        "max_history_items": 20,
        "run_budget_usd": 0.75,
        "reasoning_effort": "medium",
        "flash_mode": False,
        "use_thinking": True,
        "max_completion_tokens": 4096,
    },
    "lead": {
        "max_steps": 40,
        "max_history_items": 20,
        "run_budget_usd": 0.75,
        "reasoning_effort": "medium",
        "flash_mode": False,
        "use_thinking": True,
        "max_completion_tokens": 4096,
    },
    "content": {
        "max_steps": 40,
        "max_history_items": 20,
        "run_budget_usd": 0.75,
        "reasoning_effort": "medium",
        "flash_mode": False,
        "use_thinking": True,
        "max_completion_tokens": 5000,
    },
}
DEFAULT_POLICY = {
    "max_steps": 30,
    "max_history_items": 16,
    "run_budget_usd": 0.50,
    "reasoning_effort": "medium",
    "flash_mode": False,
    "use_thinking": True,
    "max_completion_tokens": 3200,
}

# Official GPT-5.6 Luna token prices. These are used only for a local safety estimate.
INPUT_USD_PER_M = 0.20
CACHED_INPUT_USD_PER_M = 0.02
OUTPUT_USD_PER_M = 1.20

_USAGE = {"prompt_tokens": 0, "cached_prompt_tokens": 0, "completion_tokens": 0}
_CURRENT_RUN_BUDGET_USD = float(DEFAULT_POLICY["run_budget_usd"])

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
"""


def _policy_for_current_role() -> dict[str, Any]:
    role = os.getenv("LOCENIX_AGENT_ROLE", "").strip().lower()
    return ROLE_POLICIES.get(role, DEFAULT_POLICY)


def _reset_usage(run_budget_usd: float) -> None:
    global _CURRENT_RUN_BUDGET_USD
    _USAGE.update({"prompt_tokens": 0, "cached_prompt_tokens": 0, "completion_tokens": 0})
    _CURRENT_RUN_BUDGET_USD = run_budget_usd


def _estimated_cost_usd() -> float:
    prompt = int(_USAGE["prompt_tokens"])
    cached = min(prompt, int(_USAGE["cached_prompt_tokens"]))
    uncached = max(0, prompt - cached)
    return (
        uncached * INPUT_USD_PER_M
        + cached * CACHED_INPUT_USD_PER_M
        + int(_USAGE["completion_tokens"]) * OUTPUT_USD_PER_M
    ) / 1_000_000


class MeteredChatOpenAI(BrowserUseChatOpenAI):
    """ChatOpenAI with per-run emergency budget enforcement and token metering."""

    async def ainvoke(self, messages, output_format=None, **kwargs):
        if _estimated_cost_usd() >= _CURRENT_RUN_BUDGET_USD:
            raise RuntimeError(
                f"LOCENIX per-run LLM safety budget reached (${_CURRENT_RUN_BUDGET_USD:.2f})."
            )
        response = await super().ainvoke(messages, output_format=output_format, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            _USAGE["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            _USAGE["cached_prompt_tokens"] += int(getattr(usage, "prompt_cached_tokens", 0) or 0)
            _USAGE["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
        return response


def openai_llm(*args, **kwargs):
    """Use GPT-5.6 Luna while keeping the proven local Chromium worker."""
    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "OPENAI_API_KEY is not configured. Add it as a GitHub Actions repository secret before enabling scheduled AI jobs."
        )
    policy = _policy_for_current_role()
    return MeteredChatOpenAI(
        model=OPENAI_MODEL,
        api_key=api_key,
        temperature=None,
        frequency_penalty=None,
        reasoning_effort=str(policy["reasoning_effort"]),
        reasoning_models=[OPENAI_MODEL],
        max_completion_tokens=int(policy["max_completion_tokens"]),
        timeout=100,
        max_retries=2,
    )


class AirtableEnabledAgent(BrowserUseAgent):
    """Browser Use Agent with full LOCENIX CRM context and role-aware quality settings."""

    def __init__(self, *args, **kwargs):
        policy = _policy_for_current_role()
        kwargs.setdefault("llm_timeout", 110)
        kwargs.setdefault("step_timeout", 180)
        kwargs.setdefault("flash_mode", bool(policy["flash_mode"]))
        kwargs.setdefault("use_thinking", bool(policy["use_thinking"]))
        kwargs.setdefault("max_history_items", int(policy["max_history_items"]))
        kwargs.setdefault("message_compaction", True)
        kwargs.setdefault("max_clickable_elements_length", 28000)
        kwargs.setdefault("use_judge", True)
        kwargs.setdefault("enable_planning", not bool(policy["flash_mode"]))
        if os.getenv("AIRTABLE_PAT", "").strip():
            if kwargs.get("tools") is None:
                tools = build_airtable_tools()
                kwargs["tools"] = add_extended_airtable_tools(tools)
            if isinstance(kwargs.get("task"), str):
                kwargs["task"] = kwargs["task"] + AIRTABLE_RULES
        super().__init__(*args, **kwargs)


def _monthly_cost(db) -> float:
    try:
        value = db.rpc("agent_monthly_llm_cost", {}).execute().data
        return float(value or 0)
    except Exception as exc:
        raise RuntimeError("Could not read the monthly LOCENIX LLM cost ledger from Supabase.") from exc


def _persist_usage(db, job_id: str, role: str) -> None:
    try:
        row = db.table("agent_jobs").select("result").eq("id", job_id).single().execute().data
        result = (row or {}).get("result") or {}
        policy = ROLE_POLICIES.get(role, DEFAULT_POLICY)
        result["llm_model"] = OPENAI_MODEL
        result["llm_usage"] = dict(_USAGE)
        result["estimated_llm_cost_usd"] = round(_estimated_cost_usd(), 6)
        result["cost_optimization"] = {
            "vision": False,
            "message_compaction": True,
            "prompt_cache_friendly": True,
            "flash_mode": bool(policy["flash_mode"]),
            "reasoning_effort": policy["reasoning_effort"],
            "quality_planning_preserved": not bool(policy["flash_mode"]),
        }
        local_worker.update_job(db, job_id, result=result)
    except Exception:
        pass


_original_run_agent_job = local_worker.run_agent_job


async def cost_optimized_run_agent_job(db, job: dict[str, Any]) -> None:
    """Skip empty inbox runs, enforce budgets, then delegate real work to Browser Use + Luna."""
    job_id = str(job["id"])
    input_data = job.get("input") or {}
    role = str(input_data.get("agent_role") or "").strip().lower()
    scheduled = bool(input_data.get("scheduled"))
    policy = ROLE_POLICIES.get(role, DEFAULT_POLICY)

    # Deterministic QA hook: inspects only the top-level Messaging nav, never a conversation and never the LLM.
    if bool(input_data.get("cost_gate_probe_only")):
        probe = await probe_linkedin_message_badge(db)
        local_worker.update_job(
            db,
            job_id,
            status="completed",
            result={"probe": probe, "llm_skipped": True, "estimated_llm_cost_usd": 0.0},
        )
        return

    if scheduled and role == "inbox":
        gate = await evaluate_inbox_gate(db)
        if gate.get("terminal_blocker"):
            local_worker.update_job(
                db,
                job_id,
                status="failed",
                error="LinkedIn requires legitimate human verification.",
                result={"llm_skipped": True, "cost_gate": gate, "estimated_llm_cost_usd": 0.0},
            )
            local_worker.add_event(db, job_id, "cost_gate.blocked", "Inbox gate found a LinkedIn verification blocker", gate)
            return
        if not gate.get("run_llm"):
            local_worker.update_job(
                db,
                job_id,
                status="completed",
                result={
                    "llm_skipped": True,
                    "no_change": True,
                    "cost_gate": gate,
                    "llm_model": OPENAI_MODEL,
                    "estimated_llm_cost_usd": 0.0,
                },
            )
            local_worker.add_event(db, job_id, "cost_gate.no_change", "No inbox AI call was needed", gate)
            return

    month_cost = _monthly_cost(db)
    if month_cost >= MONTHLY_LLM_BUDGET_USD:
        local_worker.update_job(
            db,
            job_id,
            status="failed",
            error=f"Monthly LLM budget of ${MONTHLY_LLM_BUDGET_USD:.2f} reached.",
            result={
                "budget_blocked": True,
                "month_cost_usd": round(month_cost, 4),
                "monthly_budget_usd": MONTHLY_LLM_BUDGET_USD,
                "estimated_llm_cost_usd": 0.0,
            },
        )
        local_worker.add_event(db, job_id, "cost_budget.blocked", "Monthly LLM budget reached")
        return

    os.environ["LOCENIX_AGENT_ROLE"] = role
    _reset_usage(float(policy["run_budget_usd"]))
    bounded_job = dict(job)
    bounded_job["max_steps"] = min(int(job.get("max_steps") or policy["max_steps"]), int(policy["max_steps"]))

    try:
        await _original_run_agent_job(db, bounded_job)
    finally:
        _persist_usage(db, job_id, role)
        if scheduled and role == "inbox":
            try:
                state = db.table("agent_jobs").select("status").eq("id", job_id).single().execute().data
                if (state or {}).get("status") == "completed":
                    mark_inbox_llm_completed(db)
            except Exception:
                pass


# Rebind local_worker behavior before its process loop runs. The legacy names stay
# internal so we do not disturb the already-tested profile/login/queue lifecycle.
local_worker.Agent = AirtableEnabledAgent
local_worker.ChatOllama = openai_llm
local_worker.ensure_ollama = lambda: None
local_worker.OLLAMA_MODEL = OPENAI_MODEL
local_worker.run_agent_job = cost_optimized_run_agent_job
local_worker.pack_profile = profile_patch.pack_profile
local_worker.unpack_profile = profile_patch.unpack_profile


def main() -> None:
    local_worker.main()


if __name__ == "__main__":
    main()
