import asyncio
import json
import os
import re
from pathlib import Path
from typing import Any

from browser_use import Agent, BrowserSession, ChatOpenAI
from playwright.async_api import async_playwright

from agent import local_worker

RESULT_PATH = Path("google_ads_conversion_result.json")
ACCOUNT_ID = "475-469-4125"
MODEL = os.getenv("OPENAI_MODEL", "gpt-5.6-luna").strip()


def redact(text: str) -> str:
    text = re.sub(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[REDACTED_EMAIL]", text)
    text = re.sub(r"(?i)(password|passwort|token|secret)\s*[:=]\s*\S+", r"\1=[REDACTED]", text)
    return text[:12000]


def write_result(payload: dict[str, Any]) -> None:
    safe = dict(payload)
    for key in ("message", "final_result"):
        if isinstance(safe.get(key), str):
            safe[key] = redact(safe[key])
    RESULT_PATH.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")
    print("GOOGLE_ADS_RESULT=" + json.dumps(safe, ensure_ascii=False))


async def google_login_preflight(chromium: str) -> dict[str, Any]:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(local_worker.PROFILE_DIR),
            executable_path=chromium,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--password-store=basic"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://ads.google.com/aw/overview", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(5000)
            url = page.url
            title = await page.title()
            login_required = (
                "accounts.google.com" in url
                or "signin" in url.lower()
                or "ServiceLogin" in url
            )
            return {"login_required": login_required, "title": title}
        finally:
            await context.close()


async def main() -> None:
    db = local_worker.db_client()
    if not local_worker.restore_profile(db):
        write_result({
            "status": "blocked",
            "reason": "NO_PROFILE",
            "message": "No encrypted browser profile is available in the LOCENIX agent storage.",
        })
        return

    chromium = local_worker.find_chromium()
    preflight = await google_login_preflight(chromium)
    if preflight["login_required"]:
        write_result({
            "status": "blocked",
            "reason": "GOOGLE_LOGIN_REQUIRED",
            "message": "The saved LOCENIX browser profile is not authenticated in Google Ads. No account settings were changed.",
            "preflight": preflight,
        })
        return

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    if not api_key:
        write_result({
            "status": "blocked",
            "reason": "NO_OPENAI_KEY",
            "message": "The GitHub Actions OpenAI secret is missing; browser task was not started.",
        })
        return

    task = f"""
Open Google Ads and work ONLY inside account LOCENIX with customer ID {ACCOUNT_ID}.

GOAL:
Create or import the missing Google Ads conversion action for the existing linked GA4 property LOCENIX so that the already implemented LOCENIX Stripe trial success event can be attributed to Google Ads.

The website already sends the successful paid trial as GA4 event `purchase`. Google Ads and GA4 are already linked. Auto-tagging is already enabled.

Required result:
1. Go to Goals / Conversions in Google Ads.
2. If a suitable existing conversion action for GA4 `purchase` already exists, do NOT create a duplicate. Verify it is enabled and suitable for bidding/primary conversion measurement, then finish.
3. Otherwise create/import a conversion from the linked Google Analytics 4 property and select event `purchase`.
4. Use the normal purchase/subscription-trial interpretation. Make it a primary conversion / included in account-level conversion goals if the UI offers this during import.
5. Do not create any fake website event, do not invent a second event name, and do not alter the LOCENIX website.
6. After saving, return to the conversions overview and verify the conversion action is present.

HARD SAFETY RULES:
- Do NOT enable, pause, create, delete, rename, or edit any campaign, ad group, ad, keyword, asset, audience, budget, bidding strategy, geo target, schedule, payment setting, billing setting, user/permission, or account-level spend setting.
- Do NOT change the campaign `LOCENIX | Search | High Intent | DE`; it must remain PAUSED.
- Do NOT change any budget or cause ad spend.
- Do NOT change GA4 settings except what is strictly required by the Google Ads import flow.
- Never bypass CAPTCHA, 2FA, security checkpoints, or Google account verification. If one appears, stop and report `LOGIN_OR_VERIFICATION_REQUIRED`.
- If you are not certain you are in customer ID {ACCOUNT_ID}, stop without changing anything.
- If `purchase` cannot be imported from the linked GA4 property, stop and report exactly what the UI says instead of creating another conversion type.

Finish with a concise statement containing: whether the conversion action exists/was created, its displayed name, whether it is primary, and confirmation that the LOCENIX campaign remained paused and no budget was changed.
""".strip()

    browser_session = BrowserSession(
        headless=True,
        executable_path=chromium,
        user_data_dir=str(local_worker.PROFILE_DIR),
        args=["--no-sandbox", "--disable-dev-shm-usage", "--password-store=basic"],
        allowed_domains=[
            "ads.google.com",
            "accounts.google.com",
            "google.com",
            "www.google.com",
            "support.google.com",
        ],
        keep_alive=False,
    )
    llm = ChatOpenAI(
        model=MODEL,
        api_key=api_key,
        reasoning_effort="medium",
        max_completion_tokens=3500,
        timeout=120,
        max_retries=2,
    )

    try:
        agent = Agent(
            task=task,
            llm=llm,
            browser_session=browser_session,
            use_vision=False,
            max_history_items=10,
            message_compaction=True,
            use_judge=True,
            enable_planning=True,
        )
        history = await asyncio.wait_for(agent.run(max_steps=38), timeout=1100)
        result = {
            "status": "completed" if history.is_successful() else "failed",
            "is_done": history.is_done(),
            "is_successful": history.is_successful(),
            "final_result": history.final_result() or "",
            "errors": [redact(str(e)) for e in history.errors() if e],
        }
        write_result(result)
    except Exception as exc:
        write_result({
            "status": "failed",
            "reason": type(exc).__name__,
            "message": str(exc),
        })
    finally:
        try:
            local_worker.save_profile(db)
        except Exception:
            pass


if __name__ == "__main__":
    asyncio.run(main())
