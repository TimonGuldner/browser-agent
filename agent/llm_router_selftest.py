from __future__ import annotations

import json
import os

from agent import llm_router


def run() -> dict:
    touched = [
        "GOOGLE_API_KEY", "GOOGLE_API_KEY_2", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "BROWSER_USE_API_KEY", "LOCENIX_LLM_PROVIDER", "GOOGLE_LITE_MODELS", "GOOGLE_FLASH_MODELS",
    ]
    original = {name: os.environ.get(name) for name in touched}
    try:
        os.environ["GOOGLE_API_KEY"] = "test-google-1"
        os.environ["GOOGLE_API_KEY_2"] = "test-google-2"
        os.environ["OPENAI_API_KEY"] = "test-openai-1"
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["BROWSER_USE_API_KEY"] = ""
        os.environ["LOCENIX_LLM_PROVIDER"] = "auto"
        os.environ["GOOGLE_LITE_MODELS"] = "gemini-2.5-flash-lite"
        os.environ["GOOGLE_FLASH_MODELS"] = "gemini-3.6-flash"
        slots = llm_router.configured_slots(llm_router.TASK_PERSONALIZATION)
        assert slots[0].provider == "google", slots
        assert slots[0].model == "gemini-2.5-flash-lite", slots[0]
        assert slots[1].provider == "google", slots
        assert slots[1].model == "gemini-2.5-flash-lite", slots[1]
        models = [s.model for s in slots if s.provider == "google"]
        assert "gemini-2.5-flash-lite" in models, models
        assert "gemini-3.6-flash" in models, models
        assert llm_router.task_type_for_role("lead", "lead", "research_v3") == llm_router.TASK_DETERMINISTIC
        assert llm_router.task_type_for_role("growth", "dm_outreach", "") == llm_router.TASK_PERSONALIZATION
        assert llm_router.should_failover("HTTP 429 RESOURCE_EXHAUSTED") is True
        assert llm_router.provider_health("HTTP 429 rate limit") == "temporarily_limited"
        assert llm_router.provider_health("HTTP 401 invalid api key") == "invalid"
        assert llm_router.should_failover("LLM returned empty text; finish_reason=MAX_TOKENS") is True
        os.environ["LOCENIX_LLM_PROVIDER"] = "openai"
        forced = llm_router.configured_slots(llm_router.TASK_CONTENT)
        assert [s.provider for s in forced] == ["openai"]
        return {"ok": True, "tested": ["gemini_model_pool", "gemini_first", "multiple_google_keys", "forced_provider", "deterministic_routing", "failover_classification"]}
    finally:
        for name, value in original.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def main() -> None:
    print(json.dumps(run()))


if __name__ == "__main__":
    main()
