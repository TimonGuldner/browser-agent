from __future__ import annotations

import json
import os

from agent import llm_router


def run() -> dict:
    touched = [
        "GOOGLE_API_KEY", "GOOGLE_API_KEY_2", "OPENAI_API_KEY", "ANTHROPIC_API_KEY",
        "BROWSER_USE_API_KEY", "LOCENIX_LLM_PROVIDER",
    ]
    original = {name: os.environ.get(name) for name in touched}
    try:
        os.environ["GOOGLE_API_KEY"] = "test-google-1"
        os.environ["GOOGLE_API_KEY_2"] = "test-google-2"
        os.environ["OPENAI_API_KEY"] = "test-openai-1"
        os.environ["ANTHROPIC_API_KEY"] = ""
        os.environ["BROWSER_USE_API_KEY"] = ""
        os.environ["LOCENIX_LLM_PROVIDER"] = "auto"
        slots = llm_router.configured_slots(llm_router.TASK_PERSONALIZATION)
        labels = [s.key_slot for s in slots]
        assert labels[:3] == ["google_1", "google_2", "openai_1"], labels
        assert llm_router.task_type_for_role("lead", "lead", "research_v3") == llm_router.TASK_DETERMINISTIC
        assert llm_router.task_type_for_role("growth", "dm_outreach", "") == llm_router.TASK_PERSONALIZATION
        assert llm_router.should_failover("HTTP 429 resource exhausted") is True
        assert llm_router.provider_health("HTTP 429 rate limit") == "temporarily_limited"
        assert llm_router.provider_health("HTTP 401 invalid api key") == "invalid"
        os.environ["LOCENIX_LLM_PROVIDER"] = "openai"
        forced = llm_router.configured_slots(llm_router.TASK_CONTENT)
        assert [s.provider for s in forced] == ["openai"]
        return {"ok": True, "tested": ["gemini_first", "multiple_google_keys", "forced_provider", "deterministic_routing", "failover_classification"]}
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
