from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from browser_use import ChatAnthropic, ChatBrowserUse, ChatGoogle, ChatOpenAI

TASK_DETERMINISTIC = "deterministic"
TASK_CHEAP_CLASSIFICATION = "cheap_classification"
TASK_PERSONALIZATION = "personalization"
TASK_CONTENT = "content"
TASK_EXECUTIVE_ANALYSIS = "executive_analysis"
TASK_REPAIR_ANALYSIS = "repair_analysis"
TASK_BROWSER_REASONING = "browser_reasoning"
TASK_HIGH_REASONING = "high_reasoning"

GOOGLE_DEFAULT_MODEL = os.getenv("GOOGLE_DEFAULT_MODEL", "gemini-3.6-flash").strip()
OPENAI_DEFAULT_MODEL = os.getenv("OPENAI_DEFAULT_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.6-luna")).strip()
ANTHROPIC_DEFAULT_MODEL = os.getenv("ANTHROPIC_DEFAULT_MODEL", "claude-sonnet-4-6").strip()
BROWSER_USE_DEFAULT_MODEL = os.getenv("BROWSER_USE_DEFAULT_MODEL", "bu-latest").strip()

# Gemini quotas are model-specific. Use a model pool so a 429 on one Gemini model
# can move to another Gemini model before falling back to a paid provider.
GOOGLE_LITE_MODELS = tuple(
    m.strip() for m in os.getenv(
        "GOOGLE_LITE_MODELS",
        "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
    ).split(",") if m.strip()
)
GOOGLE_FLASH_MODELS = tuple(
    m.strip() for m in os.getenv(
        "GOOGLE_FLASH_MODELS",
        "gemini-3.5-flash,gemini-3.7-flash,gemini-3.8-flash,gemini-3.6-flash",
    ).split(",") if m.strip()
)

DEFAULT_ORDER = ("google", "openai", "anthropic", "browser_use")
TASK_ORDERS: dict[str, tuple[str, ...]] = {
    TASK_CHEAP_CLASSIFICATION: DEFAULT_ORDER,
    TASK_PERSONALIZATION: DEFAULT_ORDER,
    TASK_CONTENT: DEFAULT_ORDER,
    TASK_EXECUTIVE_ANALYSIS: DEFAULT_ORDER,
    TASK_REPAIR_ANALYSIS: ("google", "openai", "anthropic"),
    TASK_BROWSER_REASONING: DEFAULT_ORDER,
    TASK_HIGH_REASONING: ("google", "openai", "anthropic"),
}

_TRANSIENT_MARKERS = (
    "429", "rate limit", "rate_limit", "resource exhausted", "resource_exhausted", "quota", "insufficient_quota",
    "credit_balance_exhausted", "billing", "timeout", "timed out", "502", "503", "504",
    "service unavailable", "overloaded", "capacity", "temporarily unavailable", "connection reset",
    "404", "model not found", "model_not_found", "model unavailable", "not available", "no longer available",
    "provider unavailable", "llm request", "empty text", "max_tokens", "max tokens",
)
_AUTH_MARKERS = ("401", "403", "authentication", "unauthorized", "forbidden", "invalid api key", "api key invalid")

@dataclass(frozen=True)
class ProviderSlot:
    provider: str
    env_name: str
    key_slot: str
    model: str

    @property
    def key(self) -> str:
        return os.getenv(self.env_name, "").strip()

    def public(self) -> dict[str, Any]:
        return {"provider": self.provider, "key_slot": self.key_slot, "model": self.model}


def _slot_number(name: str, prefix: str) -> int:
    if name == prefix:
        return 1
    tail = name[len(prefix):].lstrip("_")
    return int(tail) if tail.isdigit() else 999999


def _discover(prefix: str, provider: str, model: str) -> list[ProviderSlot]:
    names = [name for name, value in os.environ.items() if name.startswith(prefix) and str(value).strip()]
    names.sort(key=lambda n: (_slot_number(n, prefix), n))
    slots: list[ProviderSlot] = []
    for idx, name in enumerate(names, start=1):
        slot_no = _slot_number(name, prefix)
        if slot_no == 999999:
            slot_no = idx
        slots.append(ProviderSlot(provider, name, f"{provider}_{slot_no}", model))
    return slots


def _google_model_chain(task_type: str) -> tuple[str, ...]:
    if task_type in {TASK_CHEAP_CLASSIFICATION, TASK_PERSONALIZATION, TASK_CONTENT}:
        chain = GOOGLE_LITE_MODELS + GOOGLE_FLASH_MODELS
    else:
        chain = GOOGLE_FLASH_MODELS + GOOGLE_LITE_MODELS
    # Keep env-configured default available as a final Google fallback, without duplicates.
    ordered: list[str] = []
    for model in chain + ((GOOGLE_DEFAULT_MODEL,) if GOOGLE_DEFAULT_MODEL else ()): 
        if model and model not in ordered:
            ordered.append(model)
    return tuple(ordered)


def _discover_google(task_type: str) -> list[ProviderSlot]:
    names = [name for name, value in os.environ.items() if name.startswith("GOOGLE_API_KEY") and str(value).strip()]
    names.sort(key=lambda n: (_slot_number(n, "GOOGLE_API_KEY"), n))
    slots: list[ProviderSlot] = []
    for model_index, model in enumerate(_google_model_chain(task_type), start=1):
        for idx, name in enumerate(names, start=1):
            slot_no = _slot_number(name, "GOOGLE_API_KEY")
            if slot_no == 999999:
                slot_no = idx
            # Model suffix makes production failover telemetry unambiguous.
            slots.append(ProviderSlot("google", name, f"google_{slot_no}_m{model_index}", model))
    return slots


def configured_slots(task_type: str = TASK_BROWSER_REASONING) -> list[ProviderSlot]:
    if task_type == TASK_DETERMINISTIC:
        return []
    forced = os.getenv("LOCENIX_LLM_PROVIDER", "auto").strip().lower()
    by_provider = {
        "google": _discover_google(task_type),
        "openai": _discover("OPENAI_API_KEY", "openai", OPENAI_DEFAULT_MODEL),
        "anthropic": _discover("ANTHROPIC_API_KEY", "anthropic", ANTHROPIC_DEFAULT_MODEL),
        "browser_use": _discover("BROWSER_USE_API_KEY", "browser_use", BROWSER_USE_DEFAULT_MODEL),
    }
    if forced != "auto":
        return list(by_provider.get(forced, []))
    order = TASK_ORDERS.get(task_type, DEFAULT_ORDER)
    return [slot for provider in order for slot in by_provider.get(provider, [])]


def provider_health(error_text: str) -> str:
    text = (error_text or "").lower()
    if any(m in text for m in _AUTH_MARKERS):
        return "invalid"
    if "quota" in text or "credit_balance_exhausted" in text or "insufficient_quota" in text:
        return "quota_exhausted"
    if "429" in text or "rate limit" in text or "resource exhausted" in text or "resource_exhausted" in text:
        return "temporarily_limited"
    if any(m in text for m in ("404", "model not found", "model unavailable", "not available", "no longer available")):
        return "model_unavailable"
    if any(m in text for m in ("timeout", "502", "503", "504", "service unavailable", "overloaded", "capacity")):
        return "cooldown"
    if "empty text" in text or "max_tokens" in text or "max tokens" in text:
        return "response_exhausted"
    return "unknown"


def should_failover(error_text: str) -> bool:
    text = (error_text or "").lower()
    return any(marker in text for marker in _TRANSIENT_MARKERS + _AUTH_MARKERS)


def task_type_for_role(role: str, department_role: str = "", pipeline_phase: str = "") -> str:
    role = (role or "").lower()
    department_role = (department_role or "").lower()
    pipeline_phase = (pipeline_phase or "").lower()
    if role == "lead" and pipeline_phase != "outreach":
        return TASK_DETERMINISTIC
    if department_role in {"outreach", "dm_outreach"}:
        return TASK_PERSONALIZATION
    if department_role == "engagement":
        return TASK_CONTENT
    if role == "inbox":
        return TASK_CHEAP_CLASSIFICATION
    if role == "content":
        return TASK_CONTENT
    if role == "growth":
        return TASK_BROWSER_REASONING
    return TASK_BROWSER_REASONING


def activate_slot(slot: ProviderSlot) -> None:
    # SDKs read canonical env vars. Never log or persist secret values.
    if slot.provider == "google":
        os.environ["GOOGLE_API_KEY"] = slot.key
    elif slot.provider == "openai":
        os.environ["OPENAI_API_KEY"] = slot.key
    elif slot.provider == "anthropic":
        os.environ["ANTHROPIC_API_KEY"] = slot.key
    elif slot.provider == "browser_use":
        os.environ["BROWSER_USE_API_KEY"] = slot.key


def create_browser_llm(slot: ProviderSlot, *, reasoning_effort: str = "medium", max_completion_tokens: int = 3000):
    activate_slot(slot)
    if slot.provider == "google":
        return ChatGoogle(model=slot.model)
    if slot.provider == "openai":
        return ChatOpenAI(
            model=slot.model, api_key=slot.key, temperature=None, frequency_penalty=None,
            reasoning_effort=reasoning_effort, reasoning_models=[slot.model],
            max_completion_tokens=max_completion_tokens, timeout=100, max_retries=1,
        )
    if slot.provider == "anthropic":
        return ChatAnthropic(model=slot.model)
    if slot.provider == "browser_use":
        return ChatBrowserUse()
    raise RuntimeError(f"Unsupported LLM provider: {slot.provider}")


def _http_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: int = 120) -> dict[str, Any]:
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST", headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:2000]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc


def _google_generation_config(task_type: str, max_output_tokens: int) -> dict[str, Any]:
    config: dict[str, Any] = {"maxOutputTokens": max_output_tokens}
    # Cheap tasks should not burn most of a small response budget on internal thinking.
    if task_type in {TASK_CHEAP_CLASSIFICATION, TASK_PERSONALIZATION}:
        config["thinkingConfig"] = {"thinkingLevel": "minimal"}
    return config


def text_complete(prompt: str, *, task_type: str = TASK_CHEAP_CLASSIFICATION, max_output_tokens: int = 2600) -> tuple[str, dict[str, Any]]:
    slots = [s for s in configured_slots(task_type) if s.provider != "browser_use"]
    if not slots:
        raise RuntimeError("BLOCKED_LLM_PROVIDER: no text-capable provider configured")
    attempts: list[dict[str, Any]] = []
    last_error: Exception | None = None
    started_all = time.monotonic()
    for slot in slots:
        started = time.monotonic()
        try:
            if slot.provider == "google":
                data = _http_json(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{slot.model}:generateContent?key={slot.key}",
                    {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": _google_generation_config(task_type, max_output_tokens)},
                    {},
                )
                text = "".join(part.get("text", "") for c in data.get("candidates", []) for part in (c.get("content") or {}).get("parts", []))
                usage = data.get("usageMetadata") or {}
                candidates = data.get("candidates") or []
                finish_reason = candidates[0].get("finishReason") if candidates else None
                finish_message = candidates[0].get("finishMessage") if candidates else None
                meta = {
                    "provider": slot.provider, "model": slot.model, "key_slot": slot.key_slot,
                    "prompt_tokens": usage.get("promptTokenCount"), "completion_tokens": usage.get("candidatesTokenCount"),
                    "thought_tokens": usage.get("thoughtsTokenCount"), "finish_reason": finish_reason,
                    "finish_message": finish_message, "prompt_feedback": data.get("promptFeedback"),
                    "estimated_cost_usd": None, "free_tier_unknown": True,
                }
            elif slot.provider == "openai":
                data = _http_json(
                    "https://api.openai.com/v1/responses",
                    {"model": slot.model, "input": prompt, "max_output_tokens": max_output_tokens},
                    {"Authorization": f"Bearer {slot.key}"},
                )
                text = "".join(item.get("text", "") for output in data.get("output", []) for item in output.get("content", []) if item.get("type") == "output_text")
                usage = data.get("usage") or {}
                meta = {
                    "provider": slot.provider, "model": slot.model, "key_slot": slot.key_slot,
                    "prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                    "estimated_cost_usd": None, "free_tier_unknown": False,
                }
            elif slot.provider == "anthropic":
                data = _http_json(
                    "https://api.anthropic.com/v1/messages",
                    {"model": slot.model, "max_tokens": max_output_tokens, "messages": [{"role": "user", "content": prompt}]},
                    {"x-api-key": slot.key, "anthropic-version": "2023-06-01"},
                )
                text = "".join(block.get("text", "") for block in data.get("content", []) if block.get("type") == "text")
                usage = data.get("usage") or {}
                meta = {
                    "provider": slot.provider, "model": slot.model, "key_slot": slot.key_slot,
                    "prompt_tokens": usage.get("input_tokens"), "completion_tokens": usage.get("output_tokens"),
                    "estimated_cost_usd": None, "free_tier_unknown": False,
                }
            else:
                continue
            meta.update({
                "llm_task_type": task_type,
                "llm_latency_ms": round((time.monotonic() - started) * 1000),
                "provider_attempts": attempts + [{"provider": slot.provider, "model": slot.model, "key_slot": slot.key_slot, "status": "completed"}],
                "provider_failover_used": bool(attempts),
                "provider_failover_reason": attempts[-1].get("error_class") if attempts else None,
                "total_latency_ms": round((time.monotonic() - started_all) * 1000),
            })
            if not text.strip():
                reason = str(meta.get("finish_reason") or "unknown")
                raise RuntimeError(f"LLM returned empty text; finish_reason={reason}")
            return text, meta
        except Exception as exc:
            last_error = exc
            err = str(exc)
            attempts.append({
                "provider": slot.provider, "model": slot.model, "key_slot": slot.key_slot, "status": "failed",
                "error_class": provider_health(err), "failover_eligible": should_failover(err),
            })
            if not should_failover(err):
                break
    raise RuntimeError(f"BLOCKED_LLM_PROVIDER: all eligible providers failed; last_error={str(last_error)[:1000]}")


def router_status(task_type: str = TASK_BROWSER_REASONING) -> dict[str, Any]:
    return {
        "task_type": task_type,
        "slots": [slot.public() for slot in configured_slots(task_type)],
        "default_order": list(TASK_ORDERS.get(task_type, DEFAULT_ORDER)),
        "google_model_chain": list(_google_model_chain(task_type)),
    }
