import os
from typing import Any

from browser_use import Agent as BrowserUseAgent
from browser_use import ChatOpenAI as BrowserUseChatOpenAI
from browser_use import ChatBrowserUse, ChatGoogle, ChatAnthropic

from agent.airtable_tools import build_airtable_tools
from agent.extended_airtable_tools import add_extended_airtable_tools
from agent.cost_gate import evaluate_inbox_gate, mark_inbox_llm_completed, probe_linkedin_message_badge
from agent import local_worker
from agent import profile_patch
from agent import lead_v3

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()
MONTHLY_LLM_BUDGET_USD = max(0.50, float(os.getenv("MONTHLY_LLM_BUDGET_USD", "10")))

ROLE_POLICIES: dict[str, dict[str, Any]] = {
    "inbox": {"max_steps": 30,"max_history_items": 12,"run_budget_usd": 0.12,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 2600,"max_clickable_elements_length": 16000,"use_judge": True,"enable_planning": True},
    "growth": {"max_steps": 28,"max_history_items": 8,"run_budget_usd": 0.15,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 3000,"max_clickable_elements_length": 14000,"use_judge": False,"enable_planning": False},
    "lead": {"max_steps": 30,"max_history_items": 6,"run_budget_usd": 0.12,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 2600,"max_clickable_elements_length": 12000,"use_judge": False,"enable_planning": False},
    "content": {"max_steps": 24,"max_history_items": 8,"run_budget_usd": 0.12,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 3200,"max_clickable_elements_length": 12000,"use_judge": False,"enable_planning": False},
}
DEFAULT_POLICY = {"max_steps": 24,"max_history_items": 8,"run_budget_usd": 0.12,"reasoning_effort": "medium","flash_mode": False,"use_thinking": True,"max_completion_tokens": 2600,"max_clickable_elements_length": 12000,"use_judge": False,"enable_planning": False}

INPUT_USD_PER_M = 0.20
CACHED_INPUT_USD_PER_M = 0.02
OUTPUT_USD_PER_M = 1.20
_USAGE = {"prompt_tokens": 0, "cached_prompt_tokens": 0, "completion_tokens": 0}
_CURRENT_RUN_BUDGET_USD = float(DEFAULT_POLICY["run_budget_usd"])
_ACTIVE_PROVIDER = "unknown"
_ACTIVE_MODEL = "unknown"

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
- For Lead, research/CRM work comes before outreach. The hard CEO daily target is measured independently; deterministic research jobs receive the exact remaining target.
- Do not repeatedly reopen the same Sales Navigator result or re-read the same Airtable context in one run.
- Keep only the minimum browser state needed for the current person. Do not carry large prior DOM snapshots forward.
- Scheduled Lead research is handled by deterministic Playwright V3 with zero LLM calls. LLM providers are reserved for outreach/personalization/conversation decisions.
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
    return (uncached * INPUT_USD_PER_M + cached * CACHED_INPUT_USD_PER_M + int(_USAGE["completion_tokens"]) * OUTPUT_USD_PER_M) / 1_000_000


class MeteredChatOpenAI(BrowserUseChatOpenAI):
    async def ainvoke(self, messages, output_format=None, **kwargs):
        if _estimated_cost_usd() >= _CURRENT_RUN_BUDGET_USD:
            raise RuntimeError(f"LOCENIX per-run LLM safety budget reached (${_CURRENT_RUN_BUDGET_USD:.2f}).")
        response = await super().ainvoke(messages, output_format=output_format, **kwargs)
        usage = getattr(response, "usage", None)
        if usage is not None:
            _USAGE["prompt_tokens"] += int(getattr(usage, "prompt_tokens", 0) or 0)
            _USAGE["cached_prompt_tokens"] += int(getattr(usage, "prompt_cached_tokens", 0) or 0)
            _USAGE["completion_tokens"] += int(getattr(usage, "completion_tokens", 0) or 0)
        return response


def _configured_provider_chain() -> list[str]:
    """Provider preference for automatic failover.

    Default order is OpenAI -> Google/Gemini -> Anthropic -> Browser Use.
    LOCENIX_LLM_PROVIDER may force a single provider for debugging.
    """
    requested = os.getenv("LOCENIX_LLM_PROVIDER", "auto").strip().lower()
    configured = {
        "openai": bool(os.getenv("OPENAI_API_KEY", "").strip()),
        "google": bool(os.getenv("GOOGLE_API_KEY", "").strip()),
        "anthropic": bool(os.getenv("ANTHROPIC_API_KEY", "").strip()),
        "browser_use": bool(os.getenv("BROWSER_USE_API_KEY", "").strip()),
    }
    if requested != "auto":
        return [requested] if configured.get(requested) else []
    return [name for name in ("openai", "google", "anthropic", "browser_use") if configured[name]]


def openai_llm(*args, **kwargs):
    """Construct the provider selected for this attempt.

    Automatic retry/failover is handled around the complete browser-agent run so a
    failed OpenAI attempt can be restarted cleanly on Gemini.
    """
    global _ACTIVE_PROVIDER, _ACTIVE_MODEL
    requested = os.getenv("LOCENIX_LLM_PROVIDER", "auto").strip().lower()
    provider = requested if requested != "auto" else (_configured_provider_chain()[0] if _configured_provider_chain() else "")

    if provider == "openai" and os.getenv("OPENAI_API_KEY", "").strip():
        policy = _policy_for_current_role()
        _ACTIVE_PROVIDER, _ACTIVE_MODEL = "openai", OPENAI_MODEL
        return MeteredChatOpenAI(
            model=OPENAI_MODEL, api_key=os.getenv("OPENAI_API_KEY", "").strip(), temperature=None, frequency_penalty=None,
            reasoning_effort=str(policy["reasoning_effort"]), reasoning_models=[OPENAI_MODEL],
            max_completion_tokens=int(policy["max_completion_tokens"]), timeout=100, max_retries=2,
        )
    if provider == "google" and os.getenv("GOOGLE_API_KEY", "").strip():
        _ACTIVE_PROVIDER, _ACTIVE_MODEL = "google", "gemini-2.5-flash"
        return ChatGoogle(model="gemini-2.5-flash")
    if provider == "anthropic" and os.getenv("ANTHROPIC_API_KEY", "").strip():
        _ACTIVE_PROVIDER, _ACTIVE_MODEL = "anthropic", "claude-sonnet-4-6"
        return ChatAnthropic(model="claude-sonnet-4-6")
    if provider == "browser_use" and os.getenv("BROWSER_USE_API_KEY", "").strip():
        _ACTIVE_PROVIDER, _ACTIVE_MODEL = "browser_use", "bu-latest"
        return ChatBrowserUse()
    raise RuntimeError("BLOCKED_LLM_PROVIDER: selected provider is not configured for LOCENIX.")


def _provider_failure_text(db, job_id: str, exc: Exception | None = None) -> str:
    parts: list[str] = []
    if exc is not None:
        parts.append(str(exc))
    try:
        row = db.table("agent_jobs").select("status,error,result").eq("id", job_id).single().execute().data or {}
        parts.append(str(row.get("error") or ""))
        result = row.get("result") or {}
        parts.append(str(result.get("errors") or ""))
        parts.append(str(result.get("final_result") or ""))
    except Exception:
        pass
    return " ".join(parts).lower()


def _should_failover(text: str) -> bool:
    provider_markers = (
        "credit_balance_exhausted", "insufficient_quota", "quota", "billing", "429",
        "rate limit", "rate_limit", "401", "403", "authentication", "api key", "api_key",
        "model not found", "model_not_found", "provider unavailable", "service unavailable",
        "overloaded", "capacity", "llm provider", "llm request", "resource exhausted",
    )
    return any(marker in text for marker in provider_markers)


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


def _monthly_cost(db) -> float:
    try:
        value = db.rpc("agent_monthly_llm_cost", {}).execute().data
        return float(value or 0)
    except Exception as exc:
        raise RuntimeError("Could not read the monthly LOCENIX LLM cost ledger from Supabase.") from exc


def _persist_usage(db, job_id: str, role: str, provider_attempts: list[dict[str, Any]] | None = None) -> None:
    try:
        row = db.table("agent_jobs").select("result").eq("id", job_id).single().execute().data
        result = (row or {}).get("result") or {}
        policy = ROLE_POLICIES.get(role, DEFAULT_POLICY)
        result["llm_provider"] = _ACTIVE_PROVIDER
        result["llm_model"] = _ACTIVE_MODEL
        result["llm_usage"] = dict(_USAGE)
        result["estimated_openai_metered_cost_usd"] = round(_estimated_cost_usd(), 6) if _ACTIVE_PROVIDER == "openai" else None
        if provider_attempts is not None:
            result["provider_attempts"] = provider_attempts
            result["provider_failover_used"] = len(provider_attempts) > 1
        result["cost_optimization"] = {
            "vision": False, "message_compaction": True, "prompt_cache_friendly": True,
            "flash_mode": bool(policy["flash_mode"]), "reasoning_effort": policy["reasoning_effort"],
            "quality_planning_preserved": bool(policy["enable_planning"]),
            "max_history_items": int(policy["max_history_items"]),
            "max_clickable_elements_length": int(policy["max_clickable_elements_length"]),
            "use_judge": bool(policy["use_judge"]),
        }
        local_worker.update_job(db, job_id, result=result)
    except Exception:
        pass


_original_run_agent_job = local_worker.run_agent_job


async def cost_optimized_run_agent_job(db, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    input_data = job.get("input") or {}
    role = str(input_data.get("agent_role") or "").strip().lower()
    scheduled = bool(input_data.get("scheduled"))
    pipeline_phase = str(input_data.get("pipeline_phase") or "").strip().lower()
    policy = ROLE_POLICIES.get(role, DEFAULT_POLICY)

    if bool(input_data.get("cost_gate_probe_only")):
        probe = await probe_linkedin_message_badge(db)
        local_worker.update_job(db, job_id, status="completed", result={"probe": probe, "llm_skipped": True, "estimated_llm_cost_usd": 0.0})
        return

    if role == "lead" and pipeline_phase != "outreach":
        await lead_v3.run_deterministic_research(db, job)
        return

    if scheduled and role == "inbox":
        gate = await evaluate_inbox_gate(db)
        if gate.get("terminal_blocker"):
            local_worker.update_job(db, job_id, status="failed", error="LinkedIn requires legitimate human verification.", result={"llm_skipped": True, "cost_gate": gate, "estimated_llm_cost_usd": 0.0})
            local_worker.add_event(db, job_id, "cost_gate.blocked", "Inbox gate found a LinkedIn verification blocker", gate)
            return
        if not gate.get("run_llm"):
            local_worker.update_job(db, job_id, status="completed", result={"llm_skipped": True,"no_change": True,"cost_gate": gate,"llm_model": _ACTIVE_MODEL,"estimated_llm_cost_usd": 0.0})
            local_worker.add_event(db, job_id, "cost_gate.no_change", "No inbox AI call was needed", gate)
            return

    month_cost = _monthly_cost(db)
    if month_cost >= MONTHLY_LLM_BUDGET_USD:
        local_worker.update_job(db, job_id, status="failed", error=f"Monthly LLM budget of ${MONTHLY_LLM_BUDGET_USD:.2f} reached.", result={"budget_blocked": True,"month_cost_usd": round(month_cost, 4),"monthly_budget_usd": MONTHLY_LLM_BUDGET_USD,"estimated_llm_cost_usd": 0.0})
        local_worker.add_event(db, job_id, "cost_budget.blocked", "Monthly LLM budget reached")
        return

    os.environ["LOCENIX_AGENT_ROLE"] = role
    _reset_usage(float(policy["run_budget_usd"]))
    bounded_job = dict(job)
    bounded_job["max_steps"] = min(int(job.get("max_steps") or policy["max_steps"]), int(policy["max_steps"]))

    requested = os.getenv("LOCENIX_LLM_PROVIDER", "auto").strip().lower()
    provider_chain = _configured_provider_chain()
    if not provider_chain:
        raise RuntimeError("BLOCKED_LLM_PROVIDER: no configured provider is available.")
    if requested != "auto":
        provider_chain = provider_chain[:1]

    attempts: list[dict[str, Any]] = []
    last_exc: Exception | None = None
    original_provider_setting = os.environ.get("LOCENIX_LLM_PROVIDER")
    try:
        for index, provider in enumerate(provider_chain):
            os.environ["LOCENIX_LLM_PROVIDER"] = provider
            _reset_usage(float(policy["run_budget_usd"]))
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

            attempts.append({
                "provider": provider,
                "model": _ACTIVE_MODEL,
                "status": status,
                "failover_eligible": _should_failover(failure_text),
            })

            if exc is None and status == "completed":
                break

            has_next = index + 1 < len(provider_chain)
            if not has_next or not _should_failover(failure_text):
                if exc is not None:
                    raise exc
                break

            next_provider = provider_chain[index + 1]
            local_worker.add_event(
                db, job_id, "llm.provider_failover",
                f"LLM provider {provider} failed; retrying with {next_provider}.",
                {"from": provider, "to": next_provider},
            )
            local_worker.update_job(
                db, job_id, status="running", error=None,
                result={"provider_failover_in_progress": True, "from": provider, "to": next_provider},
            )

        if last_exc is not None:
            try:
                state = db.table("agent_jobs").select("status").eq("id", job_id).single().execute().data or {}
                if str(state.get("status") or "") != "completed" and attempts and not attempts[-1]["failover_eligible"]:
                    raise last_exc
            except Exception:
                pass
    finally:
        if original_provider_setting is None:
            os.environ.pop("LOCENIX_LLM_PROVIDER", None)
        else:
            os.environ["LOCENIX_LLM_PROVIDER"] = original_provider_setting
        _persist_usage(db, job_id, role, attempts)
        if scheduled and role == "inbox":
            try:
                state = db.table("agent_jobs").select("status").eq("id", job_id).single().execute().data
                if (state or {}).get("status") == "completed":
                    mark_inbox_llm_completed(db)
            except Exception:
                pass


local_worker.Agent = AirtableEnabledAgent
local_worker.ChatOllama = openai_llm
local_worker.ensure_ollama = lambda: None
local_worker.OLLAMA_MODEL = "auto-provider"
local_worker.run_agent_job = cost_optimized_run_agent_job
local_worker.pack_profile = profile_patch.pack_profile
local_worker.unpack_profile = profile_patch.unpack_profile


def main() -> None:
    local_worker.main()


if __name__ == "__main__":
    main()
