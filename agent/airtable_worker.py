import os

from browser_use import Agent as BrowserUseAgent
from browser_use import ChatOllama as BrowserUseChatOllama

from agent.airtable_tools import build_airtable_tools
from agent.extended_airtable_tools import add_extended_airtable_tools
from agent import local_worker
from agent import profile_patch

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


def tuned_chat_ollama(*args, **kwargs):
    """Use a deterministic CPU-friendly Qwen configuration on GitHub-hosted runners."""
    kwargs.setdefault(
        "ollama_options",
        {
            "think": False,
            "num_ctx": 8192,
            "temperature": 0,
        },
    )
    kwargs["timeout"] = max(float(kwargs.get("timeout") or 0), 240.0)
    return BrowserUseChatOllama(*args, **kwargs)


class AirtableEnabledAgent(BrowserUseAgent):
    """Browser Use Agent with the complete LOCENIX Airtable handoff surface."""

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("llm_timeout", 240)
        kwargs.setdefault("step_timeout", 300)
        if os.getenv("AIRTABLE_PAT", "").strip():
            if kwargs.get("tools") is None:
                tools = build_airtable_tools()
                kwargs["tools"] = add_extended_airtable_tools(tools)
            if isinstance(kwargs.get("task"), str):
                kwargs["task"] = kwargs["task"] + AIRTABLE_RULES
        super().__init__(*args, **kwargs)


# Rebind local_worker behavior before its process loop runs.
local_worker.Agent = AirtableEnabledAgent
local_worker.ChatOllama = tuned_chat_ollama
local_worker.pack_profile = profile_patch.pack_profile
local_worker.unpack_profile = profile_patch.unpack_profile


def main() -> None:
    local_worker.main()


if __name__ == "__main__":
    main()
