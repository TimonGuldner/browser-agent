import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any

from playwright.async_api import async_playwright
from supabase import Client

from agent.airtable_tools import AirtableClient, TABLES
from agent import local_worker

STATE_KEY = "locenix_inbox_cost_gate_v1"
FORCE_LLM_HOURS = max(1, int(os.getenv("INBOX_FORCE_LLM_HOURS", "4")))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:
        return None


def _get_state(db: Client) -> dict[str, Any]:
    response = db.table("agent_runtime_state").select("value").eq("state_key", STATE_KEY).limit(1).execute()
    if not response.data:
        return {}
    return response.data[0].get("value") or {}


def _set_state(db: Client, value: dict[str, Any]) -> None:
    db.table("agent_runtime_state").upsert(
        {"state_key": STATE_KEY, "value": value, "updated_at": _now().isoformat()},
        on_conflict="state_key",
    ).execute()


def due_followup_count() -> int:
    """Count only clearly due CRM follow-ups. No LLM and no LinkedIn write."""
    client = AirtableClient()
    try:
        rows = client.list_records(
            TABLES["people"],
            max_records=500,
            params={
                "fields[]": [
                    "Follow-up Date",
                    "Contact Status",
                    "DM Status",
                    "Do Not Contact",
                ]
            },
        )
    finally:
        client.close()

    today = datetime.now().date().isoformat()
    closed_statuses = {"NOT_INTERESTED", "DO_NOT_CONTACT", "NO_RESPONSE"}
    count = 0
    for row in rows:
        fields = row.get("fields") or {}
        if bool(fields.get("Do Not Contact")):
            continue
        if str(fields.get("Contact Status") or "").upper() in closed_statuses:
            continue
        followup = str(fields.get("Follow-up Date") or "")[:10]
        if followup and followup <= today:
            count += 1
    return count


async def probe_linkedin_message_badge(db: Client) -> dict[str, Any]:
    """Read the global LinkedIn Messaging nav badge without opening a conversation."""
    if not local_worker.restore_profile(db):
        return {"confident": False, "unread_count": None, "blocker": "saved_profile_missing"}

    chromium = local_worker.find_chromium()
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(local_worker.PROFILE_DIR),
            executable_path=chromium,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--password-store=basic"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(2500)
            url = page.url
            if any(marker in url for marker in ("/login", "/checkpoint/", "/authwall")):
                return {"confident": False, "unread_count": None, "blocker": "linkedin_verification_required", "url": url}

            links = page.locator('a[href*="/messaging/"]')
            link_count = await links.count()
            if link_count == 0:
                return {"confident": False, "unread_count": None, "blocker": "messaging_nav_not_found", "url": url}

            samples: list[str] = []
            for i in range(min(link_count, 4)):
                link = links.nth(i)
                try:
                    payload = await link.evaluate(
                        """el => {
                          const box = el.closest('li') || el.parentElement || el;
                          return [
                            el.getAttribute('aria-label') || '',
                            el.innerText || '',
                            box.getAttribute && (box.getAttribute('aria-label') || ''),
                            box.innerText || '',
                            box.className || ''
                          ].join(' | ');
                        }"""
                    )
                    samples.append(str(payload))
                except Exception:
                    continue

            combined = " | ".join(samples).lower()
            numbers = [int(x) for x in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", combined)]
            explicit_unread = any(token in combined for token in ("unread", "ungelesen", "new message", "neue nachricht"))
            unread = max(numbers) if numbers else (1 if explicit_unread else 0)
            return {"confident": True, "unread_count": unread, "blocker": None, "url": url}
        finally:
            await context.close()
            try:
                local_worker.save_profile(db)
            except Exception:
                pass


async def evaluate_inbox_gate(db: Client) -> dict[str, Any]:
    """Return run_llm=False only when skipping is high-confidence and safe."""
    now = _now()
    state = _get_state(db)
    followups = due_followup_count()
    probe = await probe_linkedin_message_badge(db)
    last_llm_at = _parse_iso(state.get("last_llm_at"))
    stale = last_llm_at is None or now - last_llm_at >= timedelta(hours=FORCE_LLM_HOURS)

    if probe.get("blocker") == "linkedin_verification_required":
        decision = "human_verification_required"
        run_llm = False
        terminal_blocker = True
    elif followups > 0:
        decision = "due_followup"
        run_llm = True
        terminal_blocker = False
    elif (probe.get("unread_count") or 0) > 0:
        decision = "unread_message"
        run_llm = True
        terminal_blocker = False
    elif stale:
        decision = "periodic_full_sweep"
        run_llm = True
        terminal_blocker = False
    elif bool(probe.get("confident")):
        decision = "no_change_skip_llm"
        run_llm = False
        terminal_blocker = False
    else:
        decision = "uncertain_fallback_to_llm"
        run_llm = True
        terminal_blocker = False

    next_state = {
        **state,
        "last_checked_at": now.isoformat(),
        "last_probe_confident": bool(probe.get("confident")),
        "last_unread_count": probe.get("unread_count"),
        "last_due_followup_count": followups,
        "last_decision": decision,
        "last_blocker": probe.get("blocker"),
    }
    _set_state(db, next_state)
    return {
        "run_llm": run_llm,
        "terminal_blocker": terminal_blocker,
        "reason": decision,
        "unread_count": probe.get("unread_count"),
        "due_followups": followups,
        "probe_confident": bool(probe.get("confident")),
        "blocker": probe.get("blocker"),
    }


def mark_inbox_llm_completed(db: Client) -> None:
    state = _get_state(db)
    state["last_llm_at"] = _now().isoformat()
    _set_state(db, state)
