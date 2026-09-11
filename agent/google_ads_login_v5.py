import asyncio
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from browser_use import Agent
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.llm import ChatOpenAI

from agent import local_worker

SESSION_FILE = Path('/tmp/google_ads_login_v5_session.json')
CREDS_FILE = Path('google_ads_login_v5_credentials.json')
RESULT_FILE = Path('google_ads_conversion_result.json')
LOGIN_TIMEOUT_SECONDS = int(os.getenv('GOOGLE_LOGIN_TIMEOUT_SECONDS', '900'))
ACCOUNT_ID = '475-469-4125'
MODEL = os.getenv('OPENAI_MODEL', 'gpt-5.6-luna').strip()


def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_pids(pids: list[int]) -> None:
    for pid in reversed(pids):
        if alive(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
    time.sleep(2)
    for pid in reversed(pids):
        if alive(pid):
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass


def chrome_logged_into_ads() -> bool:
    try:
        tabs = httpx.get('http://127.0.0.1:9222/json', timeout=3).json()
        urls = [str(tab.get('url') or '') for tab in tabs if isinstance(tab, dict)]
        ads_open = any('ads.google.com' in u and 'nav/login' not in u and 'signin' not in u.lower() for u in urls)
        login_open = any('accounts.google.com' in u or 'ServiceLogin' in u for u in urls)
        return ads_open and not login_open
    except Exception:
        return False


def write_result(payload: dict) -> None:
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print('GOOGLE_ADS_RESULT=' + json.dumps(payload, ensure_ascii=False), flush=True)


def start() -> None:
    chromium = local_worker.find_chromium()
    env = os.environ.copy()
    env['DISPLAY'] = ':99'
    env.pop('RUNNER_TRACKING_ID', None)
    local_worker.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pids: list[int] = []
    xvfb = subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1440x1000x24'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(xvfb.pid)
    time.sleep(1.5)

    vnc_password = secrets.token_urlsafe(9)
    passwd_file = '/tmp/locenix-google-v5.pass'
    subprocess.run(['x11vnc', '-storepasswd', vnc_password, passwd_file], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    vnc = subprocess.Popen(['x11vnc', '-display', ':99', '-rfbport', '5900', '-rfbauth', passwd_file, '-forever', '-shared'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(vnc.pid)
    ws = subprocess.Popen(['websockify', '--web=/usr/share/novnc', '6080', '127.0.0.1:5900'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(ws.pid)

    cf_log = '/tmp/cloudflared-google-v5.log'
    cf = subprocess.Popen(['cloudflared', 'tunnel', '--url', 'http://127.0.0.1:6080', '--no-autoupdate', '--loglevel', 'info', '--logfile', cf_log], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(cf.pid)

    chrome = subprocess.Popen([
        chromium,
        '--no-sandbox',
        '--disable-dev-shm-usage',
        '--disable-gpu',
        '--password-store=basic',
        f'--user-data-dir={local_worker.PROFILE_DIR}',
        '--window-size=1440,1000',
        '--no-first-run',
        '--no-default-browser-check',
        '--remote-debugging-port=9222',
        'https://ads.google.com/aw/overview',
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(chrome.pid)

    tunnel = ''
    deadline = time.time() + 45
    import re
    pattern = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
    while time.time() < deadline:
        p = Path(cf_log)
        if p.exists():
            m = pattern.search(p.read_text(errors='ignore'))
            if m:
                tunnel = m.group(0)
                break
        time.sleep(1)
    if not tunnel:
        stop_pids(pids)
        raise RuntimeError('Temporary browser tunnel could not be created')

    SESSION_FILE.write_text(json.dumps({'pids': pids, 'started_at': time.time()}), encoding='utf-8')
    CREDS_FILE.write_text(json.dumps({
        'live_url': tunnel + '/vnc.html?autoconnect=true&resize=remote&quality=6',
        'vnc_password': vnc_password,
        'expires_seconds': LOGIN_TIMEOUT_SECONDS,
        'instruction': 'Sign in to Google Ads only inside this remote browser. The agent will detect login automatically and continue in the same browser.',
    }, indent=2), encoding='utf-8')
    print('Google Ads live login browser started.', flush=True)


async def run_live_conversion() -> None:
    state = json.loads(SESSION_FILE.read_text(encoding='utf-8'))
    pids = [int(x) for x in state['pids']]
    deadline = float(state['started_at']) + LOGIN_TIMEOUT_SECONDS

    try:
        while time.time() < deadline:
            if chrome_logged_into_ads():
                print('GOOGLE_LOGIN_DETECTED=true', flush=True)
                break
            await asyncio.sleep(2)
        else:
            write_result({'status': 'blocked', 'reason': 'GOOGLE_LOGIN_TIMEOUT', 'message': 'Google Ads login was not detected before timeout. No account settings were changed.'})
            raise RuntimeError('Google Ads login timeout')

        api_key = os.getenv('OPENAI_API_KEY', '').strip()
        if not api_key:
            write_result({'status': 'blocked', 'reason': 'NO_OPENAI_KEY'})
            raise RuntimeError('OPENAI_API_KEY missing')

        browser_session = BrowserSession(
            browser_profile=BrowserProfile(
                cdp_url='http://127.0.0.1:9222',
                is_local=True,
                allowed_domains=['ads.google.com', 'accounts.google.com', 'google.com', 'www.google.com', 'support.google.com'],
                keep_alive=True,
            )
        )
        llm = ChatOpenAI(
            model=MODEL,
            api_key=api_key,
            reasoning_effort='medium',
            max_completion_tokens=3500,
            timeout=120,
            max_retries=2,
        )

        task = f'''Work ONLY inside the already authenticated Google Ads browser session and ONLY in LOCENIX customer account {ACCOUNT_ID}.

GOAL: Create/import the missing conversion action from the already linked GA4 property for event `purchase`, which LOCENIX fires after a verified successful Stripe trial checkout.

Steps:
1. Confirm the active Google Ads customer is {ACCOUNT_ID}. If not, stop without changing anything.
2. Open Goals / Conversions / Summary.
3. If an existing suitable GA4 `purchase` conversion already exists, do not create a duplicate. Verify it is enabled and primary/used for bidding if appropriate, then finish.
4. Otherwise create/import a conversion from the linked Google Analytics 4 property and select event `purchase`.
5. Make it the primary conversion / included in account-level conversion goals if the UI offers this.
6. Return to the conversions overview and verify the action is present.

HARD SAFETY RULES:
- Do NOT enable, pause, create, delete, rename, or edit any campaign, ad group, ad, keyword, asset, audience, budget, bidding strategy, geo target, schedule, billing setting, payment setting, user, permission, or spend setting.
- Campaign `LOCENIX | Search | High Intent | DE` must remain PAUSED.
- Do NOT change any budget and do not cause ad spend.
- Do NOT bypass CAPTCHA, 2FA, security checkpoints, or account verification. If one appears, stop and report it.
- If `purchase` cannot be imported from GA4, stop and report the exact blocker instead of creating a different event or website conversion.

Finish with: conversion action name, whether it was created or already existed, whether it is primary, and confirmation that the LOCENIX campaign stayed paused and no budget changed.'''

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
        history = await asyncio.wait_for(agent.run(max_steps=40), timeout=1100)
        payload = {
            'status': 'completed' if history.is_successful() else 'failed',
            'is_done': history.is_done(),
            'is_successful': history.is_successful(),
            'final_result': history.final_result() or '',
            'errors': [str(e) for e in history.errors() if e],
        }
        write_result(payload)
    finally:
        stop_pids(pids)


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else 'start'
    if command == 'start':
        start()
    elif command == 'run':
        asyncio.run(run_live_conversion())
    else:
        raise SystemExit(f'Unknown command: {command}')


if __name__ == '__main__':
    main()
