import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

import httpx

from agent import local_worker

SESSION_FILE = Path("/tmp/google_ads_login_session.json")
CREDS_FILE = Path("google_ads_login_credentials.json")
LOGIN_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_LOGIN_TIMEOUT_SECONDS", "900"))


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
    env["DISPLAY"] = ":99"
    env.pop("RUNNER_TRACKING_ID", None)
    return env


def start() -> None:
    db = local_worker.db_client()
    local_worker.restore_profile(db)
    chromium = local_worker.find_chromium()
    env = persistent_env()
    local_worker.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    pids: list[int] = []
    xvfb = subprocess.Popen(["Xvfb", ":99", "-screen", "0", "1440x1000x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(xvfb.pid)
    time.sleep(1.5)

    import secrets
    vnc_password = secrets.token_urlsafe(9)
    passwd_file = "/tmp/locenix-google-vnc.pass"
    subprocess.run(["x11vnc", "-storepasswd", vnc_password, passwd_file], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)

    vnc = subprocess.Popen(["x11vnc", "-display", ":99", "-rfbport", "5900", "-rfbauth", passwd_file, "-forever", "-shared", "-o", "/tmp/x11vnc-google.log"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(vnc.pid)
    time.sleep(1)

    ws = subprocess.Popen(["websockify", "--web=/usr/share/novnc", "6080", "127.0.0.1:5900"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(ws.pid)

    cloudflared_log = "/tmp/cloudflared-google.log"
    cf = subprocess.Popen(["cloudflared", "tunnel", "--url", "http://127.0.0.1:6080", "--no-autoupdate", "--loglevel", "info", "--logfile", cloudflared_log], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(cf.pid)

    chrome = subprocess.Popen([
        chromium,
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--password-store=basic",
        f"--user-data-dir={local_worker.PROFILE_DIR}",
        "--window-size=1440,1000",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-debugging-port=9222",
        "https://ads.google.com/aw/overview",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, start_new_session=True)
    pids.append(chrome.pid)

    tunnel_url = ""
    deadline = time.time() + 45
    pattern = re.compile(r"https://[a-z0-9-]+\.trycloudflare\.com")
    while time.time() < deadline:
        if Path(cloudflared_log).exists():
            match = pattern.search(Path(cloudflared_log).read_text(errors="ignore"))
            if match:
                tunnel_url = match.group(0)
                break
        time.sleep(1)
    if not tunnel_url:
        stop_pids(pids)
        raise RuntimeError("Temporary browser tunnel could not be created")

    payload = {
        "live_url": tunnel_url + "/vnc.html?autoconnect=true&resize=remote&quality=6",
        "vnc_password": vnc_password,
        "expires_seconds": LOGIN_TIMEOUT_SECONDS,
        "instruction": "Open live_url, enter the VNC password, then sign in to Google Ads. Enter Google credentials only inside the remote browser.",
    }
    SESSION_FILE.write_text(json.dumps({"pids": pids, "started_at": time.time()}, indent=2), encoding="utf-8")
    CREDS_FILE.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print("Temporary Google Ads login browser started.", flush=True)


async def wait_for_login() -> None:
    db = local_worker.db_client()
    state = json.loads(SESSION_FILE.read_text(encoding="utf-8"))
    pids = [int(x) for x in state.get("pids", [])]
    deadline = float(state.get("started_at", time.time())) + LOGIN_TIMEOUT_SECONDS
    verified = False

    try:
        while time.time() < deadline:
            try:
                tabs = httpx.get("http://127.0.0.1:9222/json", timeout=3).json()
                urls = [str(tab.get("url") or "") for tab in tabs if isinstance(tab, dict)]
                if any("ads.google.com" in url and "nav/login" not in url for url in urls):
                    if not any("accounts.google.com" in url for url in urls):
                        verified = True
                        break
            except Exception:
                pass
            await asyncio.sleep(4)
    finally:
        stop_pids(pids)
        await asyncio.sleep(2)
        try:
            saved_bytes = local_worker.save_profile(db)
        except Exception:
            saved_bytes = 0
        print(f"GOOGLE_LOGIN_VERIFIED={str(verified).lower()}", flush=True)
        print(f"GOOGLE_PROFILE_SAVED_BYTES={saved_bytes}", flush=True)
        try:
            CREDS_FILE.unlink(missing_ok=True)
        except Exception:
            pass

    if not verified:
        raise RuntimeError("Google Ads login was not verified before the temporary session ended.")


def main() -> None:
    command = sys.argv[1] if len(sys.argv) > 1 else "start"
    if command == "start":
        start()
    elif command == "wait":
        asyncio.run(wait_for_login())
    else:
        raise SystemExit(f"Unknown command: {command}")


if __name__ == "__main__":
    main()
