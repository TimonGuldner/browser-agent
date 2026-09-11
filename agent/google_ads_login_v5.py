import asyncio
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import time
from pathlib import Path

import httpx
from playwright.async_api import async_playwright

from agent import local_worker

SESSION_FILE = Path('/tmp/google_ads_login_v5_session.json')
CREDS_FILE = Path('google_ads_login_v5_credentials.json')
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


def chrome_logged_into_ads() -> bool:
    try:
        tabs = httpx.get('http://127.0.0.1:9222/json', timeout=3).json()
        urls = [str(tab.get('url') or '') for tab in tabs if isinstance(tab, dict)]
        ads_open = any('ads.google.com' in u and 'nav/login' not in u and 'signin' not in u.lower() for u in urls)
        login_open = any('accounts.google.com' in u or 'ServiceLogin' in u for u in urls)
        return ads_open and not login_open
    except Exception:
        return False


def save_minimal_profile(db) -> int:
    root = local_worker.PROFILE_DIR
    raw = io.BytesIO()
    wanted = [
        root / 'Local State',
        root / 'Default' / 'Cookies',
        root / 'Default' / 'Cookies-journal',
        root / 'Default' / 'Preferences',
        root / 'Default' / 'Secure Preferences',
        root / 'Default' / 'Network Persistent State',
        root / 'Default' / 'TransportSecurity',
        root / 'Default' / 'Web Data',
        root / 'Default' / 'Web Data-journal',
        root / 'Default' / 'Local Storage',
        root / 'Default' / 'Session Storage',
        root / 'Default' / 'IndexedDB',
    ]
    with tarfile.open(fileobj=raw, mode='w:gz') as tar:
        for item in wanted:
            if item.exists():
                tar.add(item, arcname=str(item.relative_to(root)), recursive=True)
    encrypted = local_worker.derive_fernet().encrypt(raw.getvalue())
    store = db.storage.from_(local_worker.PROFILE_BUCKET)
    try:
        store.update(local_worker.PROFILE_OBJECT, encrypted, {'content-type': 'application/octet-stream', 'upsert': 'true'})
    except Exception:
        try:
            store.remove([local_worker.PROFILE_OBJECT])
        except Exception:
            pass
        store.upload(local_worker.PROFILE_OBJECT, encrypted, {'content-type': 'application/octet-stream', 'upsert': 'true'})
    return len(encrypted)


def start() -> None:
    db = local_worker.db_client()
    local_worker.restore_profile(db)
    chromium = local_worker.find_chromium()
    env = os.environ.copy()
    env['DISPLAY'] = ':99'
    env.pop('RUNNER_TRACKING_ID', None)
    local_worker.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pids = []
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
        chromium, '--no-sandbox', '--disable-dev-shm-usage', '--disable-gpu', '--password-store=basic',
        f'--user-data-dir={local_worker.PROFILE_DIR}', '--window-size=1440,1000', '--no-first-run',
        '--no-default-browser-check', '--remote-debugging-port=9222', 'https://ads.google.com/aw/overview',
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
    }, indent=2), encoding='utf-8')


async def verify_saved(chromium: str) -> bool:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(local_worker.PROFILE_DIR), executable_path=chromium, headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--password-store=basic'],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://ads.google.com/aw/overview', wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(5000)
            u = page.url
            return 'accounts.google.com' not in u and 'signin' not in u.lower() and 'ServiceLogin' not in u
        finally:
            await context.close()


async def wait_and_save() -> None:
    db = local_worker.db_client()
    state = json.loads(SESSION_FILE.read_text(encoding='utf-8'))
    deadline = float(state['started_at']) + LOGIN_TIMEOUT_SECONDS
    pids = [int(x) for x in state['pids']]
    detected = False
    try:
        while time.time() < deadline:
            if chrome_logged_into_ads():
                detected = True
                break
            await asyncio.sleep(3)
    finally:
        stop_pids(pids)
        await asyncio.sleep(2)
        saved = save_minimal_profile(db)
        print(f'GOOGLE_MINIMAL_PROFILE_SAVED_BYTES={saved}', flush=True)
    if not detected:
        raise RuntimeError('Google Ads login was not detected before timeout.')
    chromium = local_worker.find_chromium()
    if not await verify_saved(chromium):
        raise RuntimeError('Minimal Google Ads profile was saved but did not authenticate on verification.')
    print('GOOGLE_LOGIN_VERIFIED=true', flush=True)


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
