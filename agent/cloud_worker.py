import argparse
import asyncio
import os
import socket
from datetime import datetime, timezone
from typing import Any

import httpx
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
BROWSER_USE_API_KEY = os.environ["BROWSER_USE_API_KEY"]

MODEL = os.getenv("BROWSER_USE_MODEL", "gpt-5.6-luna")
PROXY_COUNTRY = os.getenv("BROWSER_USE_PROXY_COUNTRY", "de")
MAX_COST_USD = float(os.getenv("BROWSER_USE_MAX_COST_USD", "1.00"))
POLL_SECONDS = float(os.getenv("POLL_SECONDS", "4"))
MAX_RUNTIME_SECONDS = int(os.getenv("MAX_RUNTIME_SECONDS", "1200"))
PROFILE_NAME = os.getenv("BROWSER_USE_PROFILE_NAME", "locenix-linkedin")
WORKER_ID = os.getenv("AGENT_WORKER_ID", f"github-actions:{socket.gethostname()}")

API_BASE = "https://api.browser-use.com/api/v3"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


class BrowserUseAPI:
    def __init__(self) -> None:
        self.client = httpx.AsyncClient(
            base_url=API_BASE,
            headers={
                "X-Browser-Use-API-Key": BROWSER_USE_API_KEY,
                "Content-Type": "application/json",
            },
            timeout=60,
        )

    async def close(self) -> None:
        await self.client.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self.client.request(method, path, **kwargs)
        response.raise_for_status()
        if response.status_code == 204:
            return {}
        return response.json()

    async def list_profiles(self, query: str | None = None) -> list[dict[str, Any]]:
        params = {"query": query} if query else None
        data = await self._request("GET", "/profiles", params=params)
        if isinstance(data, list):
            return data
        for key in ("profiles", "items", "data"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, list):
                return value
        return []

    async def create_profile(self, name: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/profiles",
            json={"name": name, "userId": "locenix-linkedin"},
        )

    async def create_session(
        self,
        *,
        task: str,
        profile_id: str,
        keep_alive: bool,
        model: str,
        proxy_country: str | None,
        max_cost_usd: float,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "task": task,
            "model": model,
            "keepAlive": keep_alive,
            "profileId": profile_id,
            "maxCostUsd": max_cost_usd,
            "enableRecording": False,
        }
        if proxy_country:
            payload["proxyCountryCode"] = proxy_country
        return await self._request("POST", "/sessions", json=payload)

    async def get_session(self, session_id: str) -> dict[str, Any]:
        return await self._request("GET", f"/sessions/{session_id}")

    async def stop_session(self, session_id: str, strategy: str = "session") -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/sessions/{session_id}/stop",
            json={"strategy": strategy},
        )


def claim_next_job(db: Client) -> dict[str, Any] | None:
    try:
        response = db.rpc("claim_agent_job", {"p_worker": WORKER_ID}).execute()
        if response.data:
            return response.data[0] if isinstance(response.data, list) else response.data
    except Exception:
        response = (
            db.table("agent_jobs")
            .select("*")
            .eq("status", "queued")
            .order("priority")
            .order("created_at")
            .limit(1)
            .execute()
        )
        if not response.data:
            return None
        job = response.data[0]
        claimed = (
            db.table("agent_jobs")
            .update(
                {
                    "status": "running",
                    "locked_at": now_iso(),
                    "locked_by": WORKER_ID,
                    "updated_at": now_iso(),
                }
            )
            .eq("id", job["id"])
            .eq("status", "queued")
            .execute()
        )
        return job if claimed.data else None
    return None


def get_config(db: Client, key: str) -> Any | None:
    response = db.table("agent_config").select("value").eq("key", key).limit(1).execute()
    if not response.data:
        return None
    return response.data[0]["value"]


def set_config(db: Client, key: str, value: Any) -> None:
    db.table("agent_config").upsert(
        {"key": key, "value": value, "updated_at": now_iso()},
        on_conflict="key",
    ).execute()


async def ensure_profile(db: Client, api: BrowserUseAPI) -> str:
    configured = get_config(db, "linkedin_profile_id")
    if isinstance(configured, str) and configured:
        return configured
    if isinstance(configured, dict) and configured.get("id"):
        return str(configured["id"])

    profiles = await api.list_profiles(PROFILE_NAME)
    profile = next(
        (p for p in profiles if p.get("name") == PROFILE_NAME),
        profiles[0] if profiles else None,
    )
    if profile is None:
        profile = await api.create_profile(PROFILE_NAME)

    profile_id = str(profile["id"])
    set_config(db, "linkedin_profile_id", profile_id)
    return profile_id


def insert_browser_session(
    db: Client,
    *,
    provider_session_id: str,
    live_url: str | None,
    profile_id: str,
    metadata: dict[str, Any] | None = None,
) -> str:
    payload = {
        "status": "active",
        "provider": "browser-use-v3",
        "provider_session_id": provider_session_id,
        "live_url": live_url,
        "profile_id": profile_id,
        "metadata": metadata or {},
        "updated_at": now_iso(),
    }
    response = db.table("agent_browser_sessions").insert(payload).execute()
    return str(response.data[0]["id"])


def update_browser_session(db: Client, local_id: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    db.table("agent_browser_sessions").update(fields).eq("id", local_id).execute()


def update_job(db: Client, job_id: str, **fields: Any) -> None:
    fields["updated_at"] = now_iso()
    db.table("agent_jobs").update(fields).eq("id", job_id).execute()


def add_event(
    db: Client,
    job_id: str,
    event_type: str,
    message: str,
    data: dict[str, Any] | None = None,
) -> None:
    db.table("agent_events").insert(
        {
            "job_id": job_id,
            "event_type": event_type,
            "message": message,
            "data": data or {},
        }
    ).execute()


def task_guard(mode: str) -> str:
    if mode == "view":
        return (
            "\n\nCONTROL RULES: This is READ-ONLY. Do not send messages, connect, follow, "
            "like, comment, submit forms, change settings, or make any other external change."
        )
    return (
        "\n\nCONTROL RULES: Work only on the explicit task. Do not perform bulk messaging "
        "or bulk connection requests. Work one contact at a time. Never bypass a security "
        "checkpoint, CAPTCHA, or 2FA challenge. If one appears, stop and report that human "
        "interaction is required. Do not expose credentials in output."
    )


async def run_login_job(db: Client, api: BrowserUseAPI, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    profile_id = await ensure_profile(db, api)
    input_data = job.get("input") or {}
    model = input_data.get("model", MODEL)
    proxy = input_data.get("proxy_country", PROXY_COUNTRY)
    max_cost = float(input_data.get("max_cost_usd", min(MAX_COST_USD, 0.50)))

    task = (
        "Open https://www.linkedin.com/login and leave the browser on the LinkedIn sign-in "
        "page so the human can sign in manually. Do not enter any credentials. Do not solve "
        "CAPTCHA, security checks, or 2FA. Finish the navigation task once the sign-in page "
        "is visible and keep the browser session alive for the human."
    )
    session = await api.create_session(
        task=task,
        profile_id=profile_id,
        keep_alive=True,
        model=model,
        proxy_country=proxy,
        max_cost_usd=max_cost,
    )
    provider_session_id = str(session["id"])
    live_url = session.get("liveUrl")
    local_session_id = insert_browser_session(
        db,
        provider_session_id=provider_session_id,
        live_url=live_url,
        profile_id=profile_id,
        metadata={"purpose": "linkedin_login"},
    )
    update_job(
        db,
        job_id,
        browser_session_id=local_session_id,
        result={
            "provider_session_id": provider_session_id,
            "live_url": live_url,
            "profile_id": profile_id,
            "message": "Login browser created. Open live_url and sign in manually.",
        },
    )
    add_event(db, job_id, "browser.login_started", "LinkedIn login browser created", {"live_url": live_url})

    elapsed = 0.0
    latest = session
    while elapsed < 120:
        status = latest.get("status")
        live_url = latest.get("liveUrl") or live_url
        update_browser_session(
            db,
            local_session_id,
            live_url=live_url,
            metadata={
                "purpose": "linkedin_login",
                "provider_status": status,
                "last_step_summary": latest.get("lastStepSummary"),
            },
        )
        if status in {"idle", "stopped", "timed_out", "error"}:
            break
        await asyncio.sleep(POLL_SECONDS)
        elapsed += POLL_SECONDS
        latest = await api.get_session(provider_session_id)

    if latest.get("status") in {"error", "timed_out", "stopped"} and not live_url:
        raise RuntimeError(f"Login session ended before live view was available: {latest}")

    update_job(
        db,
        job_id,
        status="waiting_approval",
        result={
            "provider_session_id": provider_session_id,
            "live_url": live_url,
            "profile_id": profile_id,
            "provider_status": latest.get("status"),
            "message": "Sign in manually in the live browser, then submit a stop_session job.",
        },
    )
    add_event(db, job_id, "browser.login_ready", "Login browser ready for human sign-in", {"live_url": live_url})


async def run_stop_session_job(db: Client, api: BrowserUseAPI, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    input_data = job.get("input") or {}
    provider_session_id = input_data.get("provider_session_id")
    local_session_id = input_data.get("browser_session_id")

    if not provider_session_id and local_session_id:
        row = (
            db.table("agent_browser_sessions")
            .select("provider_session_id")
            .eq("id", local_session_id)
            .single()
            .execute()
        )
        provider_session_id = row.data["provider_session_id"]

    if not provider_session_id:
        active = (
            db.table("agent_browser_sessions")
            .select("id,provider_session_id")
            .eq("status", "active")
            .eq("provider", "browser-use-v3")
            .order("created_at", desc=True)
            .limit(1)
            .execute()
        )
        if active.data:
            local_session_id = active.data[0]["id"]
            provider_session_id = active.data[0]["provider_session_id"]

    if not provider_session_id:
        raise RuntimeError("No active Browser Use session found to stop.")

    result = await api.stop_session(str(provider_session_id), strategy="session")
    if local_session_id:
        update_browser_session(
            db,
            str(local_session_id),
            status="closed",
            metadata={"stopped_at": now_iso(), "provider_result": result},
        )
    update_job(
        db,
        job_id,
        status="completed",
        result={
            "provider_session_id": provider_session_id,
            "message": "Browser session stopped; profile state should now be persisted.",
            "provider_result": result,
        },
    )
    add_event(db, job_id, "browser.session_stopped", "Browser session stopped and profile persisted")


async def run_task_job(db: Client, api: BrowserUseAPI, job: dict[str, Any]) -> None:
    job_id = str(job["id"])
    mode = str(job.get("mode") or "autonomous")
    input_data = job.get("input") or {}
    profile_id = input_data.get("profile_id") or await ensure_profile(db, api)
    model = input_data.get("model", MODEL)
    proxy = input_data.get("proxy_country", PROXY_COUNTRY)
    max_cost = float(input_data.get("max_cost_usd", MAX_COST_USD))
    task = str(job["task"]) + task_guard(mode)

    session = await api.create_session(
        task=task,
        profile_id=str(profile_id),
        keep_alive=False,
        model=str(model),
        proxy_country=proxy,
        max_cost_usd=max_cost,
    )
    provider_session_id = str(session["id"])
    live_url = session.get("liveUrl")
    local_session_id = insert_browser_session(
        db,
        provider_session_id=provider_session_id,
        live_url=live_url,
        profile_id=str(profile_id),
        metadata={"purpose": "agent_task", "mode": mode},
    )
    update_job(
        db,
        job_id,
        browser_session_id=local_session_id,
        result={"provider_session_id": provider_session_id, "live_url": live_url},
    )
    add_event(db, job_id, "browser.task_started", "Browser Use task started", {"live_url": live_url})

    elapsed = 0.0
    latest = session
    while elapsed < MAX_RUNTIME_SECONDS:
        status = latest.get("status")
        live_url = latest.get("liveUrl") or live_url
        partial = {
            "provider_session_id": provider_session_id,
            "live_url": live_url,
            "provider_status": status,
            "last_step_summary": latest.get("lastStepSummary"),
            "step_count": latest.get("stepCount"),
            "screenshot_url": latest.get("screenshotUrl"),
        }
        update_job(db, job_id, result=partial)
        update_browser_session(
            db,
            local_session_id,
            live_url=live_url,
            metadata={
                "purpose": "agent_task",
                "mode": mode,
                "provider_status": status,
                "last_step_summary": latest.get("lastStepSummary"),
            },
        )

        if status in {"stopped", "idle", "timed_out", "error"}:
            break
        await asyncio.sleep(POLL_SECONDS)
        elapsed += POLL_SECONDS
        latest = await api.get_session(provider_session_id)

    status = latest.get("status")
    success = latest.get("isTaskSuccessful")
    output = latest.get("output")
    result = {
        "provider_session_id": provider_session_id,
        "live_url": latest.get("liveUrl") or live_url,
        "provider_status": status,
        "output": output,
        "is_successful": success,
        "step_count": latest.get("stepCount"),
        "last_step_summary": latest.get("lastStepSummary"),
        "total_cost_usd": latest.get("totalCostUsd"),
        "screenshot_url": latest.get("screenshotUrl"),
    }

    if elapsed >= MAX_RUNTIME_SECONDS and status not in {"stopped", "idle", "timed_out", "error"}:
        await api.stop_session(provider_session_id, strategy="session")
        update_browser_session(db, local_session_id, status="closed", metadata={"reason": "worker_timeout"})
        update_job(db, job_id, status="failed", result=result, error="Agent worker timeout.")
        return

    update_browser_session(
        db,
        local_session_id,
        status="closed" if status in {"stopped", "timed_out", "error"} else "active",
        metadata={"final_provider_status": status, "total_cost_usd": latest.get("totalCostUsd")},
    )

    if status in {"error", "timed_out"} or success is False:
        update_job(
            db,
            job_id,
            status="failed",
            result=result,
            error=str(output or latest.get("lastStepSummary") or status)[:10000],
        )
        add_event(db, job_id, "browser.task_failed", "Browser Use task failed", result)
    else:
        update_job(db, job_id, status="completed", result=result, error=None)
        add_event(db, job_id, "browser.task_completed", "Browser Use task completed", result)


async def process_once() -> bool:
    db = db_client()
    job = claim_next_job(db)
    if not job:
        print("No queued agent job.")
        return False

    job_id = str(job["id"])
    api = BrowserUseAPI()
    try:
        add_event(db, job_id, "worker.claimed", f"Claimed by {WORKER_ID}")
        mode = str(job.get("mode") or "autonomous")
        if mode == "login":
            await run_login_job(db, api, job)
        elif mode == "stop_session":
            await run_stop_session_job(db, api, job)
        else:
            await run_task_job(db, api, job)
    except Exception as exc:
        update_job(db, job_id, status="failed", error=str(exc)[:10000])
        add_event(db, job_id, "worker.failed", str(exc)[:2000])
        raise
    finally:
        await api.close()
    return True


async def main() -> None:
    parser = argparse.ArgumentParser(description="LOCENIX Browser Use Cloud worker")
    parser.add_argument("--once", action="store_true", help="Process at most one queued job")
    args = parser.parse_args()

    if args.once:
        await process_once()
        return

    while True:
        processed = await process_once()
        if not processed:
            await asyncio.sleep(10)


if __name__ == "__main__":
    asyncio.run(main())
