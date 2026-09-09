import argparse
import asyncio
import base64
import hashlib
import io
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from browser_use import Agent, BrowserSession, ChatOllama
from cryptography.fernet import Fernet
from playwright.async_api import async_playwright
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
WORKER_ID = os.getenv("AGENT_WORKER_ID", f"github-actions:{socket.gethostname()}")
PROFILE_BUCKET = os.getenv("PROFILE_BUCKET", "locenix-agent-private")
PROFILE_OBJECT = os.getenv("PROFILE_OBJECT", "profiles/locenix-linkedin.tar.gz.enc")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen3:4b")
OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
LOGIN_TIMEOUT_SECONDS = int(os.getenv("LOGIN_TIMEOUT_SECONDS", "900"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "1200"))

PROFILE_DIR = Path(os.getenv("PROFILE_DIR", "/tmp/locenix-chrome-profile"))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def update_job(db: Client, job_id: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    db.table("agent_jobs").update(fields).eq("id", job_id).execute()


def add_event(db: Client, job_id: str, event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
    db.table("agent_events").insert(
        {"job_id": job_id, "event_type": event_type, "message": message, "data": data or {}}
    ).execute()


def claim_next_job(db: Client) -> dict[str, Any] | None:
    response = db.rpc("claim_agent_job", {"p_worker": WORKER_ID}).execute()
    if not response.data:
        return None
    return response.data[0] if isinstance(response.data, list) else response.data


def derive_fernet() -> Fernet:
    digest = hashlib.sha256(("locenix-profile-v1:" + SUPABASE_SERVICE_ROLE_KEY).encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _tar_filter(info: tarfile.TarInfo) -> tarfile.TarInfo | None:
    skip_parts = {
        "Cache",
        "Code Cache",
        "GPUCache",
        "ShaderCache",
        "GrShaderCache",
        "DawnCache",
        "Crashpad",
        "BrowserMetrics",
    }
    if any(part in skip_parts for part in Path(info.name).parts):
        return None
    if Path(info.name).name.startswith("Singleton"):
        return None
    return info


def pack_profile() -> bytes:
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w:gz") as tar:
        for item in PROFILE_DIR.iterdir():
            tar.add(item, arcname=item.name, recursive=True, filter=_tar_filter)
    return derive_fernet().encrypt(raw.getvalue())


def unpack_profile(data: bytes) -> None:
    decrypted = derive_fernet().decrypt(data)
    if PROFILE_DIR.exists():
        shutil.rmtree(PROFILE_DIR)
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(decrypted), mode="r:gz") as tar:
        tar.extractall(PROFILE_DIR, filter="data")


def restore_profile(db: Client) -> bool:
    try:
        encrypted = db.storage.from_(PROFILE_BUCKET).download(PROFILE_OBJECT)
        unpack_profile(encrypted)
        return True
    except Exception:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        return False


def save_profile(db: Client) -> int:
    encrypted = pack_profile()
    store = db.storage.from_(PROFILE_BUCKET)
    try:
        store.update(
            PROFILE_OBJECT,
            encrypted,
            {"content-type": "application/octet-stream", "upsert": "true"},
        )
    except Exception:
        try:
            store.remove([PROFILE_OBJECT])
        except Exception:
            pass
        store.upload(
            PROFILE_OBJECT,
            encrypted,
            {"content-type": "application/octet-stream", "upsert": "true"},
        )
    return len(encrypted)


def find_chromium() -> str:
    configured = os.getenv("CHROMIUM_EXECUTABLE")
    if configured and Path(configured).exists():
        return configured
    for name in ("chromium", "chromium-browser", "google-chrome", "google-chrome-stable"):
        found = shutil.which(name)
        if found:
            return found
    code = (
        "from playwright.sync_api import sync_playwright; "
        "p=sync_playwright().start(); print(p.chromium.executable_path); p.stop()"
    )
    candidate = subprocess.check_output([sys.executable, "-c", code], text=True).strip()
    if not Path(candidate).exists():
        raise RuntimeError("Chromium executable was not found")
    return candidate


def process_alive(proc: subprocess.Popen[Any] | None) -> bool:
    return proc is not None and proc.poll() is None


def start_login_desktop(chromium: str) -> tuple[list[subprocess.Popen[Any]], str, str]:
    display = ":99"
    env = os.environ.copy()
    env["DISPLAY"] = display
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    procs: list[subprocess.Popen[Any]] = []
    procs.append(subprocess.Popen(["Xvfb", display, "-screen", "0", "1440x1000x24"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    time.sleep(1.5)

    vnc_password = secrets.token_urlsafe(9)
    passwd_file = "/tmp/locenix-vnc.pass"
    subprocess.run(["x11vnc", "-storepasswd", vnc_password, passwd_file], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    procs.append(
        subprocess.Popen(
            ["x11vnc", "-display", display, "-rfbport", "5900", "-rfbauth", passwd_file, "-forever", "-shared", "-o", "/tmp/x11vnc.log"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    )
    time.sleep(1)
    procs.append(
        subprocess.Popen(
            ["websockify", "--web=/usr/share/novnc", "6080", "127.0.0.1:5900"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    )

    cloudflared_log = "/tmp/cloudflared.log"
    procs.append(
        subprocess.Popen(
            ["cloudflared", "tunnel", "--url", "http://127.0.0.1:6080", "--no-autoupdate", "--loglevel", "info", "--logfile", cloudflared_log],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=env,
        )
    )

    chrome_args = [
        chromium,
        "--no-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu",
        "--password-store=basic",
        f"--user-data-dir={PROFILE_DIR}",
        "--window-size=1440,1000",
        "--no-first-run",
        "--no-default-browser-check",
        "https://www.linkedin.com/login",
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


def stop_processes(procs: list[subprocess.Popen[Any]]) -> None:
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


async def verify_linkedin_login(chromium: str) -> dict[str, Any]:
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            executable_path=chromium,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--password-store=basic"],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.linkedin.com/feed/", wait_until="domcontentloaded", timeout=45000)
            await page.wait_for_timeout(3000)
            url = page.url
            title = await page.title()
            logged_in = "linkedin.com/login" not in url and "/checkpoint/" not in url and "/authwall" not in url
            return {"logged_in": logged_in, "url": url, "title": title}
        finally:
            await context.close()


async def run_login_job(db: Client, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    restore_profile(db)
    chromium = find_chromium()
    procs: list[subprocess.Popen[Any]] = []
    try:
        procs, live_url, vnc_password = start_login_desktop(chromium)
        session = db.table("agent_browser_sessions").insert(
            {
                "status": "active",
                "provider": "github-actions-novnc",
                "provider_session_id": os.getenv("GITHUB_RUN_ID", WORKER_ID),
                "live_url": live_url,
                "profile_id": "supabase:locenix-linkedin",
                "metadata": {"purpose": "linkedin_login", "expires_in_seconds": LOGIN_TIMEOUT_SECONDS},
            }
        ).execute().data[0]
        update_job(
            db,
            job_id,
            browser_session_id=session["id"],
            status="waiting_approval",
            result={
                "live_url": live_url,
                "vnc_password": vnc_password,
                "message": "Open live_url, enter the VNC password, then sign in to LinkedIn manually. Tell ChatGPT when finished.",
            },
        )
        add_event(db, job_id, "browser.login_ready", "Temporary LinkedIn login browser is ready")

        deadline = time.time() + LOGIN_TIMEOUT_SECONDS
        completed = False
        cancelled = False
        while time.time() < deadline:
            row = db.table("agent_jobs").select("status,input").eq("id", job_id).single().execute().data
            input_data = row.get("input") or {}
            if row.get("status") == "cancelled":
                cancelled = True
                break
            if bool(input_data.get("login_complete")):
                completed = True
                break
            await asyncio.sleep(4)

        stop_processes(procs)
        procs = []
        await asyncio.sleep(2)

        if cancelled:
            return
        verification = await verify_linkedin_login(chromium)
        saved_bytes = save_profile(db)
        db.table("agent_browser_sessions").update(
            {
                "status": "closed",
                "metadata": {
                    "purpose": "linkedin_login",
                    "verification": verification,
                    "profile_saved": True,
                    "saved_bytes": saved_bytes,
                },
                "updated_at": now_iso(),
            }
        ).eq("id", session["id"]).execute()

        if completed and verification["logged_in"]:
            update_job(
                db,
                job_id,
                status="completed",
                result={
                    "logged_in": True,
                    "verification": verification,
                    "profile_saved": True,
                    "message": "LinkedIn login verified and encrypted browser profile saved.",
                },
            )
            add_event(db, job_id, "browser.login_saved", "LinkedIn login verified and profile saved")
        else:
            update_job(
                db,
                job_id,
                status="failed",
                error="LinkedIn login was not verified before the temporary session ended.",
                result={"logged_in": False, "verification": verification, "profile_saved": True},
            )
    finally:
        if procs:
            stop_processes(procs)


def task_guard(mode: str) -> str:
    if mode == "view":
        return (
            "\n\nCONTROL RULES: Read only. Do not send messages, connect, follow, like, comment, submit forms, or change settings. "
            "If a CAPTCHA, 2FA, or security checkpoint appears, stop and report it."
        )
    return (
        "\n\nCONTROL RULES: Work only on the explicit task, one contact at a time. Do not bulk-message or bulk-connect. "
        "Never bypass CAPTCHA, 2FA, rate limits, or security checkpoints. Stop and report if human interaction is required. "
        "Do not reveal credentials or private session data in the final result."
    )


def ensure_ollama() -> None:
    if not shutil.which("ollama"):
        raise RuntimeError("Ollama is not installed on this runner")
    env = os.environ.copy()
    env["OLLAMA_HOST"] = "127.0.0.1:11434"
    subprocess.Popen(["ollama", "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            if httpx.get(OLLAMA_HOST, timeout=2).status_code < 500:
                break
        except Exception:
            pass
        time.sleep(1)
    subprocess.run(["ollama", "pull", OLLAMA_MODEL], check=True, timeout=900, env=env, stdout=subprocess.DEVNULL)


async def run_agent_job(db: Client, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    mode = str(job.get("mode") or "autonomous")
    if not restore_profile(db):
        raise RuntimeError("No saved browser profile exists yet. Run a login job first.")

    ensure_ollama()
    chromium = find_chromium()
    browser_session = BrowserSession(
        headless=True,
        executable_path=chromium,
        user_data_dir=str(PROFILE_DIR),
        args=["--no-sandbox", "--disable-dev-shm-usage", "--password-store=basic"],
        allowed_domains=["linkedin.com", "www.linkedin.com", "sales.linkedin.com"],
        keep_alive=False,
    )
    llm = ChatOllama(model=OLLAMA_MODEL, host=OLLAMA_HOST, timeout=180)
    task = str(job["task"]) + task_guard(mode)
    max_steps = min(int(job.get("max_steps") or 30), 40)

    try:
        agent = Agent(task=task, llm=llm, browser_session=browser_session, use_vision=False)
        history = await asyncio.wait_for(agent.run(max_steps=max_steps), timeout=MAX_RUNTIME_SECONDS)
        result = {
            "final_result": history.final_result(),
            "is_done": history.is_done(),
            "is_successful": history.is_successful(),
            "errors": [str(error) for error in history.errors() if error],
            "model": OLLAMA_MODEL,
            "browser": "local-chromium-on-github-actions",
        }
        update_job(db, job_id, status="completed" if history.is_successful() else "failed", result=result)
        add_event(db, job_id, "browser.task_finished", "Local browser task finished", {"successful": history.is_successful()})
    finally:
        try:
            await browser_session.stop()
        except Exception:
            pass
        save_profile(db)


async def process_once() -> int:
    db = db_client()
    job = claim_next_job(db)
    if not job:
        return 0
    job_id = str(job["id"])
    mode = str(job.get("mode") or "autonomous")
    try:
        add_event(db, job_id, "worker.claimed", f"Job claimed by {WORKER_ID}", {"mode": mode})
        if mode == "login":
            await run_login_job(db, job)
        elif mode == "stop_session":
            update_job(db, job_id, status="completed", result={"message": "No separate cloud session exists in local Chromium mode."})
        else:
            await run_agent_job(db, job)
        return 0
    except Exception as exc:
        update_job(db, job_id, status="failed", error=str(exc)[:10000])
        add_event(db, job_id, "worker.failed", "Job failed", {"error": str(exc)[:2000]})
        return 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true", default=True)
    parser.parse_args()
    raise SystemExit(asyncio.run(process_once()))


if __name__ == "__main__":
    main()
