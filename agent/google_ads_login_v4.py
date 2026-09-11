import asyncio
import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from agent import local_worker

SESSION_FILE = Path('/tmp/google_ads_login_v4_session.json')
CREDS_FILE = Path('google_ads_login_v4_credentials.json')
FLAG_URL = 'https://raw.githubusercontent.com/TimonGuldner/browser-agent/main/google_ads_login_complete.flag'
LOGIN_TIMEOUT_SECONDS = int(os.getenv('GOOGLE_LOGIN_TIMEOUT_SECONDS', '900'))


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


def persistent_env() -> dict[str, str]:
    env = os.environ.copy()
    env['DISPLAY'] = ':99'
    env.pop('RUNNER_TRACKING_ID', None)
    return env


def start() -> None:
    db = local_worker.db_client()
    local_worker.restore_profile(db)
    chromium = local_worker.find_chromium()
    env = persistent_env()
    local_worker.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pids: list[int] = []
    xvfb = subprocess.Popen(['Xvfb', ':99', '-screen', '0', '1440x1000x24'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(xvfb.pid)
    time.sleep(1.5)

    vnc_password = secrets.token_urlsafe(9)
    session_token = secrets.token_urlsafe(24)
    passwd_file = '/tmp/locenix-google-v4.pass'
    subprocess.run(['x11vnc', '-storepasswd', vnc_password, passwd_file], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    vnc = subprocess.Popen(['x11vnc', '-display', ':99', '-rfbport', '5900', '-rfbauth', passwd_file, '-forever', '-shared', '-o', '/tmp/x11vnc-google-v4.log'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(vnc.pid)
    time.sleep(1)

    ws = subprocess.Popen(['websockify', '--web=/usr/share/novnc', '6080', '127.0.0.1:5900'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(ws.pid)

    cloudflared_log = '/tmp/cloudflared-google-v4.log'
    cf = subprocess.Popen(['cloudflared', 'tunnel', '--url', 'http://127.0.0.1:6080', '--no-autoupdate', '--loglevel', 'info', '--logfile', cloudflared_log], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
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

    tunnel_url = ''
    deadline = time.time() + 45
    pattern = re.compile(r'https://[a-z0-9-]+\.trycloudflare\.com')
    while time.time() < deadline:
        if Path(cloudflared_log).exists():
            match = pattern.search(Path(cloudflared_log).read_text(errors='ignore'))
            if match:
                tunnel_url = match.group(0)
                break
        time.sleep(1)
    if not tunnel_url:
        stop_pids(pids)
        raise RuntimeError('Temporary browser tunnel could not be created')

    SESSION_FILE.write_text(json.dumps({'pids': pids, 'started_at': time.time(), 'session_token': session_token}, indent=2), encoding='utf-8')
    CREDS_FILE.write_text(json.dumps({
        'live_url': tunnel_url + '/vnc.html?autoconnect=true&resize=remote&quality=6',
        'vnc_password': vnc_password,
        'session_token': session_token,
        'expires_seconds': LOGIN_TIMEOUT_SECONDS,
        'instruction': 'Sign in to Google Ads in this remote browser. Enter Google credentials only inside the remote browser. Then tell ChatGPT fertig.',
    }, indent=2), encoding='utf-8')
    print('Isolated Google Ads login browser started.', flush=True)


def chrome_is_logged_into_google_ads() -> bool:
    try:
        tabs = httpx.get('http://127.0.0.1:9222/json', timeout=3).json()
        urls = [str(tab.get('url') or '') for tab in tabs if isinstance(tab, dict)]
        ads_open = any('ads.google.com' in url and 'nav/login' not in url and 'signin' not in url.lower() for url in urls)
        google_login_open = any('accounts.google.com' in url or 'ServiceLogin' in url for url in urls)
        return ads_open and not google_login_open
    except Exception:
        return False


async def verify_saved_login(chromium: str) -> dict:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(local_worker.PROFILE_DIR), executable_path=chromium, headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--password-store=basic'],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://ads.google.com/aw/overview', wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(5000)
            url = page.url
            logged_in = 'accounts.google.com' not in url and 'signin' not in url.lower() and 'ServiceLogin' not in url
            return {'logged_in': logged_in, 'title': await page.title()}
        finally:
            await context.close()


async def wait_and_save() -> None:
    db = local_worker.db_client()
    state = json.loads(SESSION_FILE.read_text(encoding='utf-8'))
    pids = [int(x) for x in state['pids']]
    session_token = str(state['session_token'])
    deadline = float(state['started_at']) + LOGIN_TIMEOUT_SECONDS
    signaled = False
    detected = False

    try:
        while time.time() < deadline:
            if chrome_is_logged_into_google_ads():
                detected = True
                break
            try:
                r = httpx.get(FLAG_URL, params={'t': str(time.time())}, timeout=5, headers={'Cache-Control': 'no-cache'})
                if r.status_code == 200 and r.text.strip() == session_token:
                    signaled = True
                    break
            except Exception:
                pass
            await asyncio.sleep(3)
    finally:
        stop_pids(pids)
        await asyncio.sleep(2)
        saved_bytes = local_worker.save_profile(db)
        print(f'GOOGLE_PROFILE_SAVED_BYTES={saved_bytes}', flush=True)

    if not (signaled or detected):
        raise RuntimeError('Google Ads login was not confirmed before the temporary session expired.')

    chromium = local_worker.find_chromium()
    verification = await verify_saved_login(chromium)
    print('GOOGLE_LOGIN_VERIFICATION=' + json.dumps(verification), flush=True)
    if not verification.get('logged_in'):
        raise RuntimeError('Google Ads login confirmation was received, but the saved browser profile is not authenticated.')


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else 'start'
    if command == 'start':
        start()
    elif command == 'wait':
        asyncio.run(wait_and_save())
    else:
        raise SystemExit(f'Unknown command: {command}')


if __name__ == '__main__':
    main()
