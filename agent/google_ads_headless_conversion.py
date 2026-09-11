import asyncio
import json
import os
import re
import signal
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
from browser_use import Agent
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.llm import ChatOpenAI
from playwright.async_api import Page, async_playwright

from agent import local_worker

ACCOUNT_ID = '475-469-4125'
MODEL = os.getenv('OPENAI_MODEL', 'gpt-5.6-luna').strip()
RESULT_FILE = Path('google_ads_headless_conversion_result.json')
CDP_URL = 'http://127.0.0.1:9222'


def write_result(payload: dict[str, Any]) -> None:
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print('HEADLESS_GOOGLE_ADS_RESULT=' + json.dumps(payload, ensure_ascii=False), flush=True)


def stop_process(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except Exception:
        try:
            proc.terminate()
        except Exception:
            return
    try:
        proc.wait(timeout=5)
    except Exception:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def launch_chrome(chromium: str) -> subprocess.Popen:
    local_worker.PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            chromium, '--headless=new', '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu',
            '--password-store=basic', f'--user-data-dir={local_worker.PROFILE_DIR}',
            '--window-size=1920,1080', '--no-first-run', '--no-default-browser-check',
            '--remote-debugging-port=9222', 'https://ads.google.com/aw/overview',
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


async def wait_for_cdp(timeout: int = 40) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(CDP_URL + '/json/version', timeout=2).status_code == 200:
                return
        except Exception:
            pass
        await asyncio.sleep(1)
    raise RuntimeError('Chrome CDP did not become ready')


async def ads_page() -> tuple[Any, Page]:
    pw = await async_playwright().start()
    browser = await pw.chromium.connect_over_cdp(CDP_URL)
    context = browser.contexts[0]
    pages = context.pages
    page = next((p for p in pages if 'ads.google.com' in p.url), pages[0] if pages else await context.new_page())
    return pw, page


async def verify_authenticated_and_account() -> dict[str, Any]:
    pw, page = await ads_page()
    try:
        await page.wait_for_timeout(5000)
        url = page.url
        if 'accounts.google.com' in url or 'ServiceLogin' in url or 'signin' in url.lower():
            return {'authenticated': False, 'account_verified': False, 'reason': 'GOOGLE_LOGIN_REQUIRED'}
        try:
            body = await page.locator('body').inner_text(timeout=10000)
        except Exception:
            body = ''
        normalized = re.sub(r'\D', '', body)
        account_verified = ACCOUNT_ID.replace('-', '') in normalized or 'LOCENIX' in body.upper()
        return {
            'authenticated': 'ads.google.com' in url,
            'account_verified': account_verified,
            'title': await page.title(),
            'host': url.split('/')[2] if '://' in url else '',
        }
    finally:
        # Stopping Playwright only disconnects this CDP client; do not close the shared Chrome process.
        await pw.stop()


async def set_control_near_text(page: Page, text_patterns: list[str], desired: bool) -> dict[str, Any]:
    combined = re.compile('|'.join(text_patterns), re.I)
    matches = page.get_by_text(combined, exact=False)
    count = min(await matches.count(), 20)
    diagnostics: list[dict[str, Any]] = []
    for i in range(count):
        loc = matches.nth(i)
        try:
            if not await loc.is_visible(timeout=800):
                continue
            outcome = await loc.evaluate(
                """(el, desired) => {
                    const selector = 'input[type=checkbox],input[type=radio],[role=checkbox],[role=radio],[aria-checked],[aria-selected],[aria-pressed]';
                    function state(c) {
                      if (c instanceof HTMLInputElement && (c.type === 'checkbox' || c.type === 'radio')) return !!c.checked;
                      for (const a of ['aria-checked','aria-selected','aria-pressed']) {
                        const v = c.getAttribute && c.getAttribute(a);
                        if (v === 'true') return true;
                        if (v === 'false') return false;
                      }
                      const cls = String(c.className || '').toLowerCase();
                      if (/selected|checked|active/.test(cls) && !/unselected|unchecked|inactive/.test(cls)) return true;
                      return null;
                    }
                    let node = el;
                    for (let depth = 0; depth < 7 && node; depth++, node = node.parentElement) {
                      const txt = String(node.innerText || '').trim();
                      if (!txt || txt.length > 2500) continue;
                      const controls = [];
                      if (node.matches && node.matches(selector)) controls.push(node);
                      if (node.querySelectorAll) controls.push(...node.querySelectorAll(selector));
                      for (const c of controls) {
                        const before = state(c);
                        if (before === null) continue;
                        if (before !== desired) c.click();
                        return {
                          found:true, before, desired, clicked:before !== desired,
                          tag:c.tagName, role:c.getAttribute && c.getAttribute('role'),
                          ariaChecked:c.getAttribute && c.getAttribute('aria-checked'),
                          ariaSelected:c.getAttribute && c.getAttribute('aria-selected'),
                          text:txt.slice(0,500)
                        };
                      }
                    }
                    return {found:false, text:String(el.innerText || '').slice(0,300)};
                }""",
                desired,
            )
            diagnostics.append(outcome)
            if outcome and outcome.get('found'):
                await page.wait_for_timeout(1200)
                return {'ok': True, 'outcome': outcome, 'diagnostics': diagnostics}
        except Exception as exc:
            diagnostics.append({'error': type(exc).__name__})
    return {'ok': False, 'diagnostics': diagnostics}


async def click_continue(page: Page) -> bool:
    labels = [
        r'^Weiter$', r'^Fortfahren$', r'^Continue$', r'^Next$',
        r'Importieren.*fortfahren', r'Import.*continue', r'^Importieren$', r'^Import$',
    ]
    for label in labels:
        buttons = page.get_by_role('button', name=re.compile(label, re.I))
        for i in range(min(await buttons.count(), 8)):
            button = buttons.nth(i)
            try:
                if await button.is_visible(timeout=500) and await button.is_enabled(timeout=500):
                    await button.click()
                    await page.wait_for_timeout(2500)
                    return True
            except Exception:
                continue
    return False


async def deterministic_source_fix() -> dict[str, Any]:
    pw, page = await ads_page()
    try:
        await page.wait_for_timeout(1500)
        body = await page.locator('body').inner_text(timeout=10000)
        if 'Google Analytics' not in body and 'Google-Analytics' not in body:
            return {'ok': False, 'reason': 'SOURCE_DIALOG_NOT_VISIBLE', 'page_excerpt': body[:1200]}

        calls = await set_control_near_text(
            page,
            [r'Conversions? aus Anrufen', r'Anruf-Conversions?', r'Calls?', r'Call conversions?'],
            False,
        )
        analytics = await set_control_near_text(page, [r'Google Analytics', r'Google-Analytics'], True)

        lower = body.lower()
        calls_text_present = 'anruf' in lower or 'call conversion' in lower or re.search(r'\bcalls?\b', lower) is not None
        if calls_text_present and not calls.get('ok'):
            return {'ok': False, 'reason': 'CALL_CONTROL_UNRESOLVED', 'calls': calls, 'analytics': analytics}
        if not analytics.get('ok'):
            return {'ok': False, 'reason': 'ANALYTICS_CONTROL_UNRESOLVED', 'calls': calls, 'analytics': analytics}

        continued = await click_continue(page)
        return {
            'ok': continued,
            'reason': None if continued else 'CONTINUE_BUTTON_NOT_FOUND',
            'calls': calls,
            'analytics': analytics,
        }
    finally:
        await pw.stop()


async def run_agent(browser_session: BrowserSession, llm: ChatOpenAI, task: str, max_steps: int) -> dict[str, Any]:
    agent = Agent(
        task=task,
        llm=llm,
        browser_session=browser_session,
        use_vision=False,
        max_history_items=10,
        message_compaction=True,
        use_judge=False,
        enable_planning=True,
    )
    history = await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=700)
    return {
        'done': history.is_done(),
        'successful': history.is_successful(),
        'final_result': history.final_result() or '',
        'errors': [str(e) for e in history.errors() if e],
    }


async def main() -> None:
    db = local_worker.db_client()
    if not local_worker.restore_profile(db):
        write_result({'status': 'blocked', 'reason': 'STORED_PROFILE_NOT_FOUND'})
        return

    chromium = local_worker.find_chromium()
    chrome: subprocess.Popen | None = None
    browser_session: BrowserSession | None = None
    try:
        chrome = launch_chrome(chromium)
        await wait_for_cdp()
        await asyncio.sleep(8)

        auth = await verify_authenticated_and_account()
        if not auth.get('authenticated'):
            write_result({'status': 'blocked', 'reason': 'GOOGLE_LOGIN_REQUIRED', 'auth': auth})
            return
        if not auth.get('account_verified'):
            write_result({'status': 'blocked', 'reason': 'ACCOUNT_NOT_VERIFIED', 'auth': auth})
            return

        api_key = os.getenv('OPENAI_API_KEY', '').strip()
        if not api_key:
            write_result({'status': 'blocked', 'reason': 'NO_OPENAI_KEY'})
            return

        browser_session = BrowserSession(
            browser_profile=BrowserProfile(
                cdp_url=CDP_URL,
                is_local=True,
                allowed_domains=['ads.google.com', 'accounts.google.com', 'google.com', 'www.google.com', 'support.google.com'],
                keep_alive=True,
            )
        )
        llm = ChatOpenAI(
            model=MODEL,
            api_key=api_key,
            reasoning_effort='medium',
            max_completion_tokens=3000,
            timeout=120,
            max_retries=2,
        )

        phase1 = await run_agent(
            browser_session,
            llm,
            f'''Work only in Google Ads account {ACCOUNT_ID} / LOCENIX. Do not alter campaigns, ads, keywords, budgets, bidding, billing, users, or spend settings. Campaign `LOCENIX | Search | High Intent | DE` must remain paused.

Navigate to Goals > Conversions > Summary and confirm the active customer is {ACCOUNT_ID}. If a suitable imported GA4 conversion named `purchase` already exists, stop and report exactly `ALREADY_EXISTS` plus whether it is Primary. Otherwise start New conversion action > Import and navigate only to the conversion-source selection screen where Google Analytics and source types such as Calls/Anrufe are shown. Do NOT select, deselect, or toggle any conversion source on that screen. Stop there and report `SOURCE_SCREEN_READY`.''',
            24,
        )

        if 'ALREADY_EXISTS' in phase1.get('final_result', ''):
            write_result({'status': 'completed', 'reason': 'ALREADY_EXISTS', 'auth': auth, 'phase1': phase1})
            return

        source_fix = await deterministic_source_fix()
        if not source_fix.get('ok'):
            write_result({
                'status': 'blocked', 'reason': source_fix.get('reason', 'SOURCE_FIX_FAILED'),
                'auth': auth, 'phase1': phase1, 'source_fix': source_fix,
            })
            return

        phase2 = await run_agent(
            browser_session,
            llm,
            f'''Continue ONLY the Google Analytics 4 conversion import already open in Google Ads account {ACCOUNT_ID} / LOCENIX. Calls/Anrufe must NOT be imported.

Select ONLY the linked GA4 event `purchase` from property 549213640. Import/save it as the Google Ads conversion. If the UI asks whether it is Primary or used for bidding/account-level goals, make `purchase` Primary. Do not create any call conversion or any other event. Then return to Goals > Conversions > Summary and verify `purchase` is present and Primary.

HARD SAFETY: do not enable/pause/edit any campaign, ad group, ad, keyword, asset, budget, bid strategy, targeting, billing, payment, user/permission, or spend setting. Campaign `LOCENIX | Search | High Intent | DE` must remain PAUSED and no budget may change. Finish with `CONVERSION_DONE` only if `purchase` is visibly present after saving; otherwise report the exact blocker.''',
            28,
        )

        success = 'CONVERSION_DONE' in phase2.get('final_result', '')
        write_result({
            'status': 'completed' if success else 'failed',
            'auth': auth,
            'phase1': phase1,
            'source_fix': source_fix,
            'phase2': phase2,
        })
    except Exception as exc:
        write_result({'status': 'failed', 'reason': type(exc).__name__, 'message': str(exc)[:2000]})
    finally:
        try:
            if browser_session is not None:
                await browser_session.kill()
        except Exception:
            pass
        stop_process(chrome)


if __name__ == '__main__':
    asyncio.run(main())
