import os

from browser_use import Agent as BrowserUseAgent

from agent.airtable_tools import build_airtable_tools
from agent import local_worker


class AirtableEnabledAgent(BrowserUseAgent):
    """Browser Use Agent that receives LOCENIX Airtable CRM tools when configured."""

    def __init__(self, *args, **kwargs):
        if kwargs.get("tools") is None and os.getenv("AIRTABLE_PAT", "").strip():
            kwargs["tools"] = build_airtable_tools()
        super().__init__(*args, **kwargs)


# local_worker imports Agent at module import time. Rebind it before its process loop runs.
local_worker.Agent = AirtableEnabledAgent


def main() -> None:
    local_worker.main()


if __name__ == "__main__":
    main()
