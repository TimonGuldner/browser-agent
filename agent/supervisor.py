from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

MAX_RETRIES = 2
RUNNING_STALE_MINUTES = 30
FAILED_LOOKBACK_HOURS = 6

RETRYABLE_MARKERS = (
    "browserstartevent",
    "timed out after 30.0s",
    "watchdog_base",
    "browser start",
    "invalid agent_role",
    "invalid run_status",
    "invalid content engine select value",
    "navigation failed - site unavailable: https://jobs.result/airtable-run-reports",
)

HARD_BLOCK_MARKERS = (
    "captcha",
    "2fa",
    "checkpoint",
    "authwall",
    "human verification",
    "monthly llm budget",
    "openai_api_key is not configured",
    "airtable_pat is not configured",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def db_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def _job_text(job: dict[str, Any]) -> str:
    parts = [str(job.get("error") or "")]
    result = job.get("result") or {}
    if isinstance(result, dict):
        parts.append(str(result.get("final_result") or ""))
        errors = result.get("errors") or []
        if isinstance(errors, list):
            parts.extend(str(x) for x in errors)
    return "\n".join(parts).lower()


def _retryable(job: dict[str, Any]) -> bool:
    text = _job_text(job)
    if any(marker in text for marker in HARD_BLOCK_MARKERS):
        return False
    return any(marker in text for marker in RETRYABLE_MARKERS)


def _current_task(db: Client, role: str, fallback: str) -> tuple[str, int, int]:
    row = (
        db.table("agent_templates")
        .select("prompt,runtime_adapter,max_steps,priority,enabled")
        .eq("role_key", role)
        .single()
        .execute()
        .data
    )
    if not row or not row.get("enabled", True):
        return fallback, 40, 100
    adapter = str(row.get("runtime_adapter") or "").strip()
    prompt = str(row.get("prompt") or "").strip()
    task = "\n\n".join(x for x in (adapter, prompt) if x) or fallback
    return task, int(row.get("max_steps") or 40), int(row.get("priority") or 100)


def _has_child_retry(db: Client, job_id: str) -> bool:
    rows = (
        db.table("agent_jobs")
        .select("id,input,status")
        .in_("status", ["queued", "running", "completed"])
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )
    for row in rows:
        input_data = row.get("input") or {}
        if str(input_data.get("retry_of") or "") == job_id:
            return True
    return False


def _retry_job(db: Client, job: dict[str, Any]) -> str | None:
    input_data = dict(job.get("input") or {})
    retry_count = int(input_data.get("supervisor_retry_count") or 0)
    if retry_count >= MAX_RETRIES:
        return None
    job_id = str(job["id"])
    if _has_child_retry(db, job_id):
        return None

    role = str(input_data.get("agent_role") or "").strip().lower()
    if role not in {"inbox", "growth", "lead", "content"}:
        return None

    task, max_steps, priority = _current_task(db, role, str(job.get("task") or ""))
    new_input = input_data
    new_input["source"] = "supervisor-retry"
    new_input["retry_of"] = job_id
    new_input["supervisor_retry_count"] = retry_count + 1
    new_input["scheduled"] = False
    if role == "lead":
        new_input["target_new_profiles"] = 10

    created = (
        db.table("agent_jobs")
        .insert(
            {
                "status": "queued",
                "task": task,
                "mode": str(job.get("mode") or "autonomous"),
                "priority": min(int(job.get("priority") or priority), priority),
                "max_steps": max_steps,
                "input": new_input,
            }
        )
        .execute()
        .data
    )
    if not created:
        return None
    new_id = str(created[0]["id"])
    db.table("agent_events").insert(
        {
            "job_id": new_id,
            "event_type": "supervisor.retry_created",
            "message": f"Supervisor retried failed {role} job {job_id}",
            "data": {"retry_of": job_id, "retry_count": retry_count + 1},
        }
    ).execute()
    return new_id


def run() -> dict[str, Any]:
    db = db_client()
    now = datetime.now(timezone.utc)
    stale_before = (now - timedelta(minutes=RUNNING_STALE_MINUTES)).isoformat()
    failed_after = (now - timedelta(hours=FAILED_LOOKBACK_HOURS)).isoformat()

    stale_rows = (
        db.table("agent_jobs")
        .select("id,status,locked_at,input,error,result,task,mode,priority,max_steps")
        .eq("status", "running")
        .lt("locked_at", stale_before)
        .execute()
        .data
        or []
    )
    stale_marked = 0
    for job in stale_rows:
        db.table("agent_jobs").update(
            {
                "status": "failed",
                "error": "Supervisor: stale running job exceeded 30 minutes; eligible for bounded retry.",
                "updated_at": now_iso(),
            }
        ).eq("id", job["id"]).execute()
        job["status"] = "failed"
        job["error"] = "Supervisor: stale running job exceeded 30 minutes; eligible for bounded retry."
        stale_marked += 1

    failed_rows = (
        db.table("agent_jobs")
        .select("id,status,created_at,updated_at,task,mode,priority,max_steps,input,error,result")
        .eq("status", "failed")
        .gte("updated_at", failed_after)
        .order("updated_at", desc=True)
        .limit(50)
        .execute()
        .data
        or []
    )

    retried: list[dict[str, str]] = []
    skipped_hard = 0
    for job in failed_rows:
        text = _job_text(job)
        if any(marker in text for marker in HARD_BLOCK_MARKERS):
            skipped_hard += 1
            continue
        if not _retryable(job):
            continue
        new_id = _retry_job(db, job)
        if new_id:
            retried.append({"old": str(job["id"]), "new": new_id})

    return {
        "stale_marked_failed": stale_marked,
        "retries_created": retried,
        "hard_blocks_not_retried": skipped_hard,
    }


def main() -> None:
    result = run()
    print(result)


if __name__ == "__main__":
    main()
