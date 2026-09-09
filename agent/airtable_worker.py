import os

from browser_use import Agent as BrowserUseAgent

from agent.airtable_tools import build_airtable_tools
from agent import local_worker

AIRTABLE_RULES = """

AIRTABLE CRM RULES:
- The Airtable base "LOCENIX LinkedIn Growth OS" is the operational source of truth.
- Before contacting or researching a person, search Airtable first to avoid duplicates and check Do Not Contact.
- For scheduled work, load the Daily Growth Queue before deciding what is due.
- Load Brand & Profile before drafting outbound messages so positioning and tone stay current.
- After every real LinkedIn action, immediately update the relevant People record and log the Interaction.
- Mark a Daily Growth Queue item Executed only if the external action actually happened.
- Never contact a record marked Do Not Contact.
- If Airtable and the browser disagree, preserve both observations in the CRM and do not invent a successful action.
"""


class AirtableEnabledAgent(BrowserUseAgent):
    """Browser Use Agent that receives LOCENIX Airtable CRM tools when configured."""

    def __init__(self, *args, **kwargs):
        if os.getenv("AIRTABLE_PAT", "").strip():
            if kwargs.get("tools") is None:
                kwargs["tools"] = build_airtable_tools()
            if isinstance(kwargs.get("task"), str):
                kwargs["task"] = kwargs["task"] + AIRTABLE_RULES
        super().__init__(*args, **kwargs)


# local_worker imports Agent at module import time. Rebind it before its process loop runs.
local_worker.Agent = AirtableEnabledAgent


def main() -> None:
    local_worker.main()


if __name__ == "__main__":
    main()
