import os

from browser_use import Agent as BrowserUseAgent
from browser_use import ChatGoogle as BrowserUseChatGoogle

from agent.airtable_tools import build_airtable_tools
from agent.extended_airtable_tools import add_extended_airtable_tools
from agent import local_worker
from agent import profile_patch

GOOGLE_MODEL = os.getenv("GOOGLE_MODEL", "gemini-3.7-flash").strip()

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
"""


def gemini_llm(*args, **kwargs):
    """Use Google's hosted Flash model instead of a tiny CPU-bound local model."""
    api_key = os.getenv("GOOGLE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "GOOGLE_API_KEY is not configured. Add it as a GitHub Actions repository secret before enabling scheduled jobs."
        )
    return BrowserUseChatGoogle(
        model=GOOGLE_MODEL,
        api_key=api_key,
        temperature=0,
    )


class AirtableEnabledAgent(BrowserUseAgent):
    """Browser Use Agent with the complete LOCENIX Airtable handoff surface."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("llm_timeout", 120)
        kwargs.setdefault("step_timeout", 180)
        kwargs.setdefault("flash_mode", True)
        if os.getenv("AIRTABLE_PAT", "").strip():
            if kwargs.get("tools") is None:
                tools = build_airtable_tools()
                kwargs["tools"] = add_extended_airtable_tools(tools)
            if isinstance(kwargs.get("task"), str):
                kwargs["task"] = kwargs["task"] + AIRTABLE_RULES
        super().__init__(*args, **kwargs)


# Rebind local_worker behavior before its process loop runs. local_worker still
# references these legacy names internally, so replacing them here lets us keep
# the proven browser/profile/queue code while swapping only the inference layer.
local_worker.Agent = AirtableEnabledAgent
local_worker.ChatOllama = gemini_llm
local_worker.ensure_ollama = lambda: None
local_worker.OLLAMA_MODEL = GOOGLE_MODEL
local_worker.pack_profile = profile_patch.pack_profile
local_worker.unpack_profile = profile_patch.unpack_profile


def main() -> None:
    local_worker.main()


if __name__ == "__main__":
    main()
