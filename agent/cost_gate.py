import os
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from playwright.async_api import async_playwright
from supabase import Client

from agent.airtable_tools import AirtableClient, TABLES
from agent import local_worker

STATE_KEY = "locenix_inbox_cost_gate_v2"
FORCE_LLM_HOURS = max(1, int(os.getenv("INBOX_FORCE_LLM_HOURS", "4")))
BERLIN = ZoneInfo("Europe/Berlin")


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

    today = datetime.now(BERLIN).date().isoformat()
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


def _badge_number(text: str) -> int | None:
    matches = [int(x) for x in re.findall(r"(?<!\d)(\d{1,3})(?!\d)", text)]
    return max(matches) if matches else None


def _blocked_url(url: str) -> bool:
    lowered = url.lower()
    return any(marker in lowered for marker in ("/login", "/checkpoint/", "/authwall"))


async def _read_nav_badge(
    page,
    *,
    link_selector: str,
    root_pattern: str,
    labels: tuple[str, ...],
    not_thread_markers: tuple[str, ...] = (),
) -> dict[str, Any]:
    links = page.locator(link_selector)
    link_count = await links.count()
    candidates: list[Any] = []

    for i in range(min(link_count, 30)):
        link = links.nth(i)
        try:
            info = await link.evaluate(
                """el => ({
                  href: el.getAttribute('href') || '',
                  aria: el.getAttribute('aria-label') || '',
                  text: (el.innerText || '').trim(),
                  cls: el.className || ''
                })"""
            )
        except Exception:
            continue

        href = str(info.get("href") or "")
        combined = f"{info.get('aria') or ''} {info.get('text') or ''}".lower()
        if any(marker in href.lower() for marker in not_thread_markers):
            continue
        is_label = any(token in combined for token in labels)
        is_root = bool(re.search(root_pattern, href, flags=re.I))
        if is_label or is_root:
            candidates.append(link)

    if not candidates:
        return {
            "confident": False,
            "unread_count": None,
            "blocker": "messaging_nav_not_found",
        }

    nav = candidates[0]
    data = await nav.evaluate(
        """el => {
          const box = el.closest('li') || el.parentElement || el;
          const badgeSelectors = [
            '.notification-badge__count',
            '[class*="notification-badge"]',
            '[class*="notification-count"]',
            '[class*="badge-count"]',
            '[class*="unread-count"]',
            '[class*="unread"]',
            '[aria-label*="unread" i]',
            '[aria-label*="ungeles" i]',
            '[aria-label*="new message" i]',
            '[aria-label*="neue nachricht" i]'
          ];
          const badgeNodes = [];
          for (const selector of badgeSelectors) {
            for (const node of box.querySelectorAll(selector)) {
              if (!badgeNodes.includes(node)) badgeNodes.push(node);
            }
          }
          return {
            href: el.getAttribute('href') || '',
            linkAria: el.getAttribute('aria-label') || '',
            linkText: (el.innerText || '').trim(),
            boxAria: box.getAttribute ? (box.getAttribute('aria-label') || '') : '',
            boxText: (box.innerText || '').trim(),
            badgeTexts: badgeNodes.map(node => [
              node.getAttribute('aria-label') || '',
              node.textContent || ''
            ].join(' ').trim()).filter(Boolean)
          };
        }"""
    )

    badge_texts = [str(x) for x in (data.get("badgeTexts") or [])]
    badge_blob = " | ".join(badge_texts).lower()
    nav_blob = " ".join(
        str(data.get(key) or "")
        for key in ("linkAria", "linkText", "boxAria")
    ).lower()

    count = _badge_number(badge_blob)
    unread_tokens = ("unread", "ungeles", "new message", "neue nachricht")

    if count is None and badge_blob and any(token in badge_blob for token in unread_tokens):
        count = 1

    if count is None and any(token in nav_blob for token in (*unread_tokens, "notification")):
        count = _badge_number(nav_blob) or 1

    # A confidently identified messaging nav item with no unread badge means zero.
    if count is None and not badge_texts:
        count = 0

    return {
        "confident": count is not None,
        "unread_count": count,
        "blocker": None if count is not None else "messaging_badge_ambiguous",
        "href": data.get("href"),
    }


async def _probe_standard_linkedin_inbox(page) -> dict[str, Any]:
    await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)
    url = page.url
    if _blocked_url(url):
        return {
            "confident": False,
            "unread_count": None,
            "blocker": "linkedin_verification_required",
            "url": url,
        }

    result = await _read_nav_badge(
        page,
        link_selector='a[href*="/messaging/"]',
        root_pattern=r"/messaging/?(?:\?.*)?$",
        labels=("messaging", "nachrichten"),
        not_thread_markers=("/messaging/thread/",),
    )
    result["url"] = url
    if result.get("blocker") == "messaging_nav_not_found":
        result["blocker"] = "linkedin_messaging_nav_not_found"
    elif result.get("blocker") == "messaging_badge_ambiguous":
        result["blocker"] = "linkedin_messaging_badge_ambiguous"
    return result


async def _probe_sales_navigator_inbox(page) -> dict[str, Any]:
    # Probe the Sales Navigator HOME nav, not the inbox itself, so no conversation is opened
    # and no unread message is accidentally marked as read.
    await page.goto("https://www.linkedin.com/sales/home", wait_until="domcontentloaded", timeout=45000)
    await page.wait_for_timeout(2500)
    url = page.url

    if _blocked_url(url):
        return {
            "confident": False,
            "unread_count": None,
            "blocker": "linkedin_verification_required",
            "url": url,
        }

    # Sales Navigator may redirect while preserving access. If the page clearly leaves
    # the /sales area, treat that as uncertain and fall back to the full agent.
    if "/sales" not in url.lower():
        return {
            "confident": False,
            "unread_count": None,
            "blocker": "sales_navigator_access_uncertain",
            "url": url,
        }

    result = await _read_nav_badge(
        page,
        link_selector='a[href*="/sales/inbox"], a[href*="/sales/messaging"]',
        root_pattern=r"/sales/(?:inbox|messaging)/?(?:\?.*)?$",
        labels=("messaging", "nachrichten", "inbox", "posteingang"),
    )
    result["url"] = url
    if result.get("blocker") == "messaging_nav_not_found":
        result["blocker"] = "sales_messaging_nav_not_found"
    elif result.get("blocker") == "messaging_badge_ambiguous":
        result["blocker"] = "sales_messaging_badge_ambiguous"
    return result


async def probe_linkedin_message_badge(db: Client) -> dict[str, Any]:
    """Read both LinkedIn and Sales Navigator messaging badges without opening a conversation."""
    if not local_worker.restore_profile(db):
        return {
            "confident": False,
            "unread_count": None,
            "standard_unread_count": None,
            "sales_unread_count": None,
            "blocker": "saved_profile_missing",
            "probe_version": 3,
        }

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

            standard = await _probe_standard_linkedin_inbox(page)
            if standard.get("blocker") == "linkedin_verification_required":
                return {
                    "confident": False,
                    "unread_count": None,
                    "standard_unread_count": standard.get("unread_count"),
                    "sales_unread_count": None,
                    "blocker": "linkedin_verification_required",
                    "standard_probe": standard,
                    "sales_probe": None,
                    "probe_version": 3,
                }

            sales = await _probe_sales_navigator_inbox(page)
            if sales.get("blocker") == "linkedin_verification_required":
                return {
                    "confident": False,
                    "unread_count": None,
                    "standard_unread_count": standard.get("unread_count"),
                    "sales_unread_count": sales.get("unread_count"),
                    "blocker": "linkedin_verification_required",
                    "standard_probe": standard,
                    "sales_probe": sales,
                    "probe_version": 3,
                }

            standard_count = standard.get("unread_count")
            sales_count = sales.get("unread_count")
            standard_confident = bool(standard.get("confident"))
            sales_confident = bool(sales.get("confident"))
            confident = standard_confident and sales_confident

            total = None
            if standard_count is not None or sales_count is not None:
                total = int(standard_count or 0) + int(sales_count or 0)

            blocker = None
            if not confident:
                blockers = [
                    str(x)
                    for x in (standard.get("blocker"), sales.get("blocker"))
                    if x
                ]
                blocker = ",".join(blockers) if blockers else "dual_inbox_probe_uncertain"

            return {
                "confident": confident,
                "unread_count": total,
                "standard_unread_count": standard_count,
                "sales_unread_count": sales_count,
                "blocker": blocker,
                "standard_probe": standard,
                "sales_probe": sales,
                "probe_version": 3,
            }
        finally:
            await context.close()
            try:
                local_worker.save_profile(db)
            except Exception:
                pass


async def evaluate_inbox_gate(db: Client) -> dict[str, Any]:
    """Return run_llm=False only when both inboxes are high-confidence clean."""
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
    elif (probe.get("standard_unread_count") or 0) > 0:
        decision = "unread_linkedin_message"
        run_llm = True
        terminal_blocker = False
    elif (probe.get("sales_unread_count") or 0) > 0:
        decision = "unread_sales_navigator_message"
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
        "last_standard_unread_count": probe.get("standard_unread_count"),
        "last_sales_unread_count": probe.get("sales_unread_count"),
        "last_due_followup_count": followups,
        "last_decision": decision,
        "last_blocker": probe.get("blocker"),
        "probe_version": probe.get("probe_version"),
    }
    _set_state(db, next_state)
    return {
        "run_llm": run_llm,
        "terminal_blocker": terminal_blocker,
        "reason": decision,
        "unread_count": probe.get("unread_count"),
        "standard_unread_count": probe.get("standard_unread_count"),
        "sales_unread_count": probe.get("sales_unread_count"),
        "due_followups": followups,
        "probe_confident": bool(probe.get("confident")),
        "blocker": probe.get("blocker"),
        "probe_version": probe.get("probe_version"),
    }


def mark_inbox_llm_completed(db: Client) -> None:
    state = _get_state(db)
    state["last_llm_at"] = _now().isoformat()
    _set_state(db, state)
