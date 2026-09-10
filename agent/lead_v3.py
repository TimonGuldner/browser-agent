from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx
from playwright.async_api import async_playwright

from agent import local_worker

AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appN6ox7fjGFyXZhL")
AIRTABLE_PAT = os.getenv("AIRTABLE_PAT", "").strip()
PEOPLE_TABLE = "People"
DEFAULT_SEARCH_QUERY = os.getenv("LOCENIX_LEAD_SEARCH_QUERY", "Inhaber Handwerk").strip()
TARGET_NEW_PROFILES = 10
MAX_VISIBLE_CANDIDATES = 40


def _canonical_sales_url(url: str) -> str:
    url = (url or "").strip()
    if not url:
        return ""
    url = url.split("?")[0].split(",NAME_SEARCH")[0]
    return url.rstrip("/")


def _lead_id(url: str) -> str:
    return "li_" + hashlib.sha256(url.encode("utf-8")).hexdigest()[:20]


def _airtable_headers() -> dict[str, str]:
    if not AIRTABLE_PAT:
        raise RuntimeError("AIRTABLE_PAT is not configured")
    return {"Authorization": f"Bearer {AIRTABLE_PAT}", "Content-Type": "application/json"}


async def _existing_urls() -> set[str]:
    existing: set[str] = set()
    offset: str | None = None
    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            params: list[tuple[str, str]] = [("pageSize", "100"), ("fields[]", "LinkedIn URL")]
            if offset:
                params.append(("offset", offset))
            r = await client.get(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PEOPLE_TABLE}",
                headers=_airtable_headers(),
                params=params,
            )
            r.raise_for_status()
            payload = r.json()
            for record in payload.get("records", []):
                url = _canonical_sales_url(str((record.get("fields") or {}).get("LinkedIn URL") or ""))
                if url:
                    existing.add(url)
            offset = payload.get("offset")
            if not offset:
                break
    return existing


async def _create_people(candidates: list[dict[str, str]]) -> list[str]:
    if not candidates:
        return []
    created_ids: list[str] = []
    today = datetime.now(timezone.utc).date().isoformat()
    async with httpx.AsyncClient(timeout=30) as client:
        for start in range(0, len(candidates), 10):
            chunk = candidates[start : start + 10]
            records = []
            for c in chunk:
                notes = "Deterministic Playwright V3 research. Raw Sales Navigator card: " + c.get("card_text", "")[:1200]
                records.append(
                    {
                        "fields": {
                            "Full Name": c["name"],
                            "LinkedIn URL": c["url"],
                            "First Seen": today,
                            "Lead ID": _lead_id(c["url"]),
                            "Contact Status": "RESEARCHED",
                            "Connection Request Sent": False,
                            "DM Status": "NOT_SENT",
                            "Notes": notes,
                        }
                    }
                )
            r = await client.post(
                f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{PEOPLE_TABLE}",
                headers=_airtable_headers(),
                json={"records": records, "typecast": False},
            )
            r.raise_for_status()
            created_ids.extend(str(x.get("id")) for x in r.json().get("records", []) if x.get("id"))
    return created_ids


async def _extract_candidates(page) -> list[dict[str, str]]:
    # Sales Navigator virtualizes results; gather, scroll, gather again without sending DOM snapshots to an LLM.
    found: dict[str, dict[str, str]] = {}
    for _ in range(8):
        anchors = page.locator('a[href*="/sales/lead/"]')
        count = min(await anchors.count(), MAX_VISIBLE_CANDIDATES)
        for i in range(count):
            anchor = anchors.nth(i)
            try:
                href = _canonical_sales_url(await anchor.get_attribute("href") or "")
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.linkedin.com" + href
                name = re.sub(r"\s+", " ", (await anchor.inner_text()).strip())
                if not name or len(name) > 120:
                    continue
                card_text = ""
                for ancestor in ("li", "article", "div"):
                    try:
                        node = anchor.locator(f"xpath=ancestor::{ancestor}[1]")
                        text = re.sub(r"\s+", " ", (await node.inner_text()).strip())
                        if text and len(text) >= len(name):
                            card_text = text
                            break
                    except Exception:
                        pass
                found[href] = {"name": name, "url": href, "card_text": card_text}
            except Exception:
                continue
        if len(found) >= MAX_VISIBLE_CANDIDATES:
            break
        await page.mouse.wheel(0, 1400)
        await page.wait_for_timeout(1200)
    return list(found.values())


async def run_deterministic_research(db, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    input_data = job.get("input") or {}
    target = max(1, min(int(input_data.get("target_new_profiles") or TARGET_NEW_PROFILES), 20))
    search_query = str(input_data.get("search_query") or DEFAULT_SEARCH_QUERY).strip()

    if not local_worker.restore_profile(db):
        raise RuntimeError("No saved LinkedIn browser profile exists")

    chromium = local_worker.find_chromium()
    existing = await _existing_urls()
    candidates: list[dict[str, str]] = []

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(local_worker.PROFILE_DIR),
            executable_path=chromium,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--password-store=basic"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.linkedin.com/sales/search/people", wait_until="domcontentloaded", timeout=60000)
            await page.wait_for_timeout(2500)
            if "/login" in page.url or "/checkpoint/" in page.url or "/authwall" in page.url:
                raise RuntimeError("LinkedIn login/security checkpoint requires human action")

            # Use the visible Sales Navigator search box when available. No LLM/browser-use step is needed.
            boxes = page.locator('input[placeholder*="Search" i], input[aria-label*="Search" i], input[type="text"]')
            box_count = await boxes.count()
            if box_count:
                box = boxes.first
                try:
                    await box.fill(search_query)
                    await box.press("Enter")
                    await page.wait_for_timeout(3000)
                except Exception:
                    pass

            raw = await _extract_candidates(page)
            seen: set[str] = set()
            for c in raw:
                url = _canonical_sales_url(c["url"])
                if not url or url in existing or url in seen:
                    continue
                seen.add(url)
                c["url"] = url
                candidates.append(c)
                if len(candidates) >= target:
                    break
        finally:
            await context.close()

    created_ids = await _create_people(candidates)
    result = {
        "pipeline_phase": "research_v3",
        "browser": "playwright-deterministic",
        "search_query": search_query,
        "target_new_profiles": target,
        "candidates_found": len(candidates),
        "airtable_records_created": len(created_ids),
        "airtable_record_ids": created_ids,
        "llm_skipped": True,
        "llm_usage": {"prompt_tokens": 0, "cached_prompt_tokens": 0, "completion_tokens": 0},
        "estimated_llm_cost_usd": 0.0,
        "outreach_sent": 0,
        "next_phase": "outreach",
    }
    status = "completed" if len(created_ids) >= target else "failed"
    error = None if status == "completed" else f"Deterministic research found/created {len(created_ids)} of target {target} new profiles."
    local_worker.update_job(db, job_id, status=status, result=result, error=error)
    local_worker.add_event(db, job_id, "lead.v3_research", "Deterministic Sales Navigator research finished", result)
