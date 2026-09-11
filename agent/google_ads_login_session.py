import os
import re
import secrets
import subprocess
import time
from pathlib import Path

from playwright.async_api import async_playwright

from agent import local_worker

LOGIN_TIMEOUT_SECONDS = int(os.getenv("GOOGLE_LOGIN_TIMEOUT_SECONDS", "900"))


def process_alive(proc):
    return proc is not None and proc.poll() is None


def stop_processes(procs):
    for proc in reversed(procs):
        if process_alive(proc):
            proc.terminate()
    deadline = time.time() + 8
    for proc in reversed(procs):
        if process_alive(proc):
            timeout = max(0.1, deadline - time.time())
            try:
                proc.wait(timeout=timeout)
            except Exception:
                proc.kill()


def start_google_login_desktop(chromium: str):
    display = ":99"
    env = os.environ.copy()
    env["DISPLAY"] = display
    local_worker.PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    procs = []
    procs.append(subprocess.Popen(["Xvfb", display, "-screen", "0", "1440x1000x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(1.5)

    vnc_password = secrets.token_urlsafe(9)
    passwd_file = "/tmp/locenix-google-vnc.pass"
    subprocess.run(["x11vnc", "-storepasswd", vnc_password, passwd_file], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(subprocess.Popen(["x11vnc", "-display", display, "-rfbport", "5900", "-rfbauth", passwd_file, "-forever", "-shared", "-o", "/tmp/x11vnc-google.log"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))
    time.sleep(1)
    procs.append(subprocess.Popen(["websockify", "--web=/usr/share/novnc", "6080", "127.0.0.1:5900"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))

    cloudflared_log = "/tmp/cloudflared-google.log"
    procs.append(subprocess.Popen(["cloudflared", "tunnel", "--url", "http://127.0.0.1:6080", "--no-autoupdate", "--loglevel", "info", "--logfile", cloudflared_log], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))

    chrome_args = [
        chromium,
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--password-store=basic",
        f"--user-data-dir={local_worker.PROFILE_DIR}",
        "--window-size=1440,1000",
        "--no-first-run",
        "--no-default-browser-check",
        "https://ads.google.com/aw/overview",
    ]
    procs.append(subprocess.Popen(chrome_args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env))

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
        raise RuntimeError("Temporary browser tunnel could not be created")

    live_url = tunnel_url + "/vnc.html?autoconnect=true&resize=remote&quality=6"
    return procs, live_url, vnc_password


async def verify_google_ads_login(chromium: str):
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
            await page.wait_for_timeout(4000)
            url = page.url
            title = await page.title()
            logged_in = "accounts.google.com" not in url and "signin" not in url.lower() and "ServiceLogin" not in url
            return {"logged_in": logged_in, "title": title}
        finally:
            await context.close()


async def main():
    db = local_worker.db_client()
    local_worker.restore_profile(db)
    chromium = local_worker.find_chromium()
    procs = []
    try:
        procs, live_url, vnc_password = start_google_login_desktop(chromium)
        print(f"GOOGLE_LOGIN_LIVE_URL={live_url}", flush=True)
        print(f"GOOGLE_LOGIN_VNC_PASSWORD={vnc_password}", flush=True)
        print(f"GOOGLE_LOGIN_EXPIRES_SECONDS={LOGIN_TIMEOUT_SECONDS}", flush=True)
        print("GOOGLE_LOGIN_INSTRUCTION=Open the live URL, enter the VNC password, then sign in to Google Ads manually. Do not paste Google credentials into chat.", flush=True)

        deadline = time.time() + LOGIN_TIMEOUT_SECONDS
        while time.time() < deadline:
            await __import__("asyncio").sleep(5)

        stop_processes(procs)
        procs = []
        await __import__("asyncio").sleep(2)
        verification = await verify_google_ads_login(chromium)
        saved_bytes = local_worker.save_profile(db)
        print(f"GOOGLE_LOGIN_VERIFIED={str(verification['logged_in']).lower()}", flush=True)
        print(f"GOOGLE_PROFILE_SAVED_BYTES={saved_bytes}", flush=True)
    finally:
        if procs:
            stop_processes(procs)


if __name__ == "__main__":
    import asyncio
    asyncio.run(main())
