import asyncio
import json
import os
import re
import secrets
import signal
import subprocess
import time
from pathlib import Path

import httpx
from browser_use.browser import BrowserProfile, BrowserSession
from browser_use.llm import ChatOpenAI
from playwright.async_api import async_playwright

from agent import local_worker
from agent.google_ads_headless_conversion import (
    ACCOUNT_ID,
    CDP_URL,
    deterministic_source_fix,
    run_agent,
    verify_authenticated_and_account,
)
from agent.google_ads_session_state import save_session_state

SESSION_FILE = Path('/tmp/google_ads_v6_session.json')
CREDS_FILE = Path('google_ads_v6_credentials.json')
RESULT_FILE = Path('google_ads_conversion_result.json')
LOGIN_TIMEOUT_SECONDS = int(os.getenv('GOOGLE_LOGIN_TIMEOUT_SECONDS', '1200'))
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


def write_result(payload: dict) -> None:
    RESULT_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    print('GOOGLE_ADS_V6_RESULT=' + json.dumps(payload, ensure_ascii=False), flush=True)


def start() -> None:
    chromium = local_worker.find_chromium()
    env = os.environ.copy()
    env['DISPLAY'] = ':99'
    env.pop('RUNNER_TRACKING_ID', None)
    local_worker.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pids: list[int] = []
    xvfb = subprocess.Popen(
        ['Xvfb', ':99', '-screen', '0', '1440x1000x24'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True,
    )
    pids.append(xvfb.pid)
    time.sleep(1.5)

    vnc_password = secrets.token_urlsafe(9)
    passwd_file = '/tmp/locenix-google-v6.pass'
    subprocess.run(
        ['x11vnc', '-storepasswd', vnc_password, passwd_file],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env,
    )
    vnc = subprocess.Popen(
        ['x11vnc', '-display', ':99', '-rfbport', '5900', '-rfbauth', passwd_file, '-forever', '-shared'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True,
    )
    pids.append(vnc.pid)
    ws = subprocess.Popen(
        ['websockify', '--web=/usr/share/novnc', '6080', '127.0.0.1:5900'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True,
    )
    pids.append(ws.pid)

    pinggy_log = Path('/tmp/pinggy-google-v6.log')
    log_handle = pinggy_log.open('wb')
    ssh = subprocess.Popen(
        [
            'ssh', '-p', '443',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'UserKnownHostsFile=/dev/null',
            '-o', 'ServerAliveInterval=20',
            '-o', 'ServerAliveCountMax=3',
            '-R0:127.0.0.1:6080', '-t', 'free.pinggy.io', 'x:https',
        ],
        stdin=subprocess.PIPE,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    pids.append(ssh.pid)
    # Pinggy may ask for a blank password on a first connection. Keep stdin open afterward.
    try:
        time.sleep(2)
        if ssh.stdin:
            ssh.stdin.write(b'\n')
            ssh.stdin.flush()
    except Exception:
        pass

    chrome = subprocess.Popen(
        [
            chromium, '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--password-store=basic',
            f'--user-data-dir={local_worker.PROFILE_DIR}', '--window-size=1440,1000', '--no-first-run',
            '--no-default-browser-check', '--remote-debugging-port=9222', 'https://ads.google.com/aw/overview',
        ],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True,
    )
    pids.append(chrome.pid)

    tunnel = ''
    deadline = time.time() + 60
    patterns = [
        re.compile(r'https://[a-z0-9-]+(?:\.[a-z0-9-]+)*\.pinggy\.link', re.I),
        re.compile(r'https://[a-z0-9-]+\.a\.free\.pinggy\.link', re.I),
    ]
    while time.time() < deadline:
        try:
            text = pinggy_log.read_text(errors='ignore') if pinggy_log.exists() else ''
            for pattern in patterns:
                m = pattern.search(text)
                if m:
                    tunnel = m.group(0)
                    break
            if tunnel:
                break
        except Exception:
            pass
        time.sleep(1)

    if not tunnel:
        stop_pids(pids)
        raise RuntimeError('Pinggy HTTPS tunnel could not be created')

    # Confirm the public URL actually serves the noVNC page before exposing it.
    live_url = tunnel + '/vnc.html?autoconnect=true&resize=remote&quality=6'
    reachable = False
    for _ in range(12):
        try:
            r = httpx.get(tunnel + '/vnc.html', timeout=8, follow_redirects=True)
            if r.status_code < 500 and ('noVNC' in r.text or 'vnc' in r.text.lower()):
                reachable = True
                break
        except Exception:
            pass
        time.sleep(2)
    if not reachable:
        stop_pids(pids)
        raise RuntimeError('Pinggy tunnel was created but noVNC was not externally reachable')

    SESSION_FILE.write_text(json.dumps({'pids': pids, 'started_at': time.time()}), encoding='utf-8')
    CREDS_FILE.write_text(json.dumps({
        'live_url': live_url,
        'vnc_password': vnc_password,
        'expires_seconds': LOGIN_TIMEOUT_SECONDS,
        'provider': 'pinggy',
        'preflight_reachable': True,
    }, indent=2), encoding='utf-8')
    print('PINGGY_LOGIN_BROWSER_READY=true', flush=True)


async def save_current_session(db) -> int:
    pw = await async_playwright().start()
    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
        context = browser.contexts[0]
        return await save_session_state(db, context)
    finally:
        await pw.stop()


async def run() -> None:
    state = json.loads(SESSION_FILE.read_text(encoding='utf-8'))
    pids = [int(x) for x in state['pids']]
    deadline = float(state['started_at']) + LOGIN_TIMEOUT_SECONDS
    db = local_worker.db_client()
    browser_session = None
    try:
        await asyncio.sleep(15)
        stable = 0
        auth = None
        while time.time() < deadline:
            auth = await verify_authenticated_and_account()
            if auth.get('authenticated') and auth.get('account_verified'):
                stable += 1
                print(f'GOOGLE_AUTH_STABLE={stable}/4', flush=True)
                if stable >= 4:
                    break
            else:
                stable = 0
            await asyncio.sleep(3)
        else:
            write_result({'status': 'blocked', 'reason': 'GOOGLE_LOGIN_TIMEOUT', 'auth': auth})
            return

        # Save the authenticated state BEFORE any Google Ads changes, so retries no longer need manual login.
        saved_bytes = await save_current_session(db)
        print(f'GOOGLE_SESSION_STATE_SAVED_BYTES={saved_bytes}', flush=True)

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
                'status': 'blocked',
                'reason': source_fix.get('reason', 'SOURCE_FIX_FAILED'),
                'auth': auth,
                'phase1': phase1,
                'source_fix': source_fix,
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

        # Refresh saved state after successful interaction as well.
        try:
            await save_current_session(db)
        except Exception:
            pass
    except Exception as exc:
        write_result({'status': 'failed', 'reason': type(exc).__name__, 'message': str(exc)[:2000]})
    finally:
        try:
            if browser_session is not None:
                await browser_session.kill()
        except Exception:
            pass
        stop_pids(pids)


def main() -> None:
    command = os.sys.argv[1] if len(os.sys.argv) > 1 else 'start'
    if command == 'start':
        start()
    elif command == 'run':
        asyncio.run(run())
    else:
        raise SystemExit(f'Unknown command: {command}')


if __name__ == '__main__':
    main()
