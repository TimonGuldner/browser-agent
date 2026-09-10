from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openai import OpenAI
from supabase import Client, create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("REPAIR_OPENAI_MODEL", os.getenv("OPENAI_MODEL", "gpt-5.6-luna")).strip()

FAILED_LOOKBACK_HOURS = 6
MAX_LLM_REPAIRS_PER_CHAIN = 1
MIN_PATCH_CONFIDENCE = 0.85

KNOWN_RETRYABLE_MARKERS = (
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
    "missing api secret",
)

EDITABLE_FILES = {
    "agent/local_worker.py",
    "agent/airtable_worker.py",
    "agent/airtable_tools.py",
    "agent/extended_airtable_tools.py",
    "agent/supervisor.py",
    "agent/cost_gate.py",
    "agent/profile_patch.py",
}
CONTEXT_FILES = sorted(EDITABLE_FILES | {"pyproject.toml"})


def db_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def job_text(job: dict[str, Any]) -> str:
    parts = [str(job.get("error") or "")]
    result = job.get("result") or {}
    if isinstance(result, dict):
        parts.append(str(result.get("final_result") or ""))
        errors = result.get("errors") or []
        if isinstance(errors, list):
            parts.extend(str(x) for x in errors)
    return "\n".join(parts)


def is_hard_block(job: dict[str, Any]) -> bool:
    text = job_text(job).lower()
    return any(marker in text for marker in HARD_BLOCK_MARKERS)


def is_known_retryable(job: dict[str, Any]) -> bool:
    text = job_text(job).lower()
    return any(marker in text for marker in KNOWN_RETRYABLE_MARKERS)


def needs_llm_repair(job: dict[str, Any]) -> bool:
    if is_hard_block(job):
        return False
    input_data = job.get("input") or {}
    if int(input_data.get("llm_repair_count") or 0) >= MAX_LLM_REPAIRS_PER_CHAIN:
        return False
    retry_count = int(input_data.get("supervisor_retry_count") or 0)
    # Unknown errors go directly to diagnosis. Known transient errors get two cheap retries first.
    return (not is_known_retryable(job)) or retry_count >= 2


def has_repair_activity(db: Client, job_id: str) -> bool:
    events = (
        db.table("agent_events")
        .select("id")
        .eq("job_id", job_id)
        .in_("event_type", ["repair.started", "repair.diagnosed", "repair.patch_applied", "repair.test_enqueued", "repair.escalated"])
        .limit(1)
        .execute()
        .data
        or []
    )
    if events:
        return True
    rows = (
        db.table("agent_jobs")
        .select("id,input")
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )
    return any(str((row.get("input") or {}).get("repair_of") or "") == job_id for row in rows)


def add_event(db: Client, job_id: str, event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
    db.table("agent_events").insert(
        {"job_id": job_id, "event_type": event_type, "message": message, "data": data or {}}
    ).execute()


def repo_context() -> str:
    chunks: list[str] = []
    for rel in CONTEXT_FILES:
        path = Path(rel)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        # Enough source to diagnose while bounding token cost.
        if len(text) > 24000:
            text = text[:12000] + "\n...<middle omitted>...\n" + text[-12000:]
        chunks.append(f"\n===== {rel} =====\n{text}")
    return "".join(chunks)


def parse_json_response(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].lstrip()
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Repair response must be a JSON object")
    return data


def request_repair(job: dict[str, Any]) -> dict[str, Any]:
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY unavailable for repair agent")
    input_data = job.get("input") or {}
    failure = job_text(job)
    prompt = f"""You are the LOCENIX technical repair agent. Diagnose one failed GitHub/Supabase browser-agent job and, only if safe and high-confidence, propose ONE minimal unified-diff patch to ONE editable Python file.

FAILED JOB
id: {job.get('id')}
role: {input_data.get('agent_role')}
retry_count: {input_data.get('supervisor_retry_count', 0)}
error/result:\n{failure[:18000]}

REPOSITORY CONTEXT
{repo_context()}

STRICT RULES
- Return JSON only. No markdown fences.
- action must be "patch" or "diagnose".
- Editable file must be exactly one of: {sorted(EDITABLE_FILES)}.
- Never edit workflow YAML, secrets, credentials, authentication data, profile data or database access keys.
- Never weaken or bypass CAPTCHA, 2FA, checkpoint, authwall, rate-limit, anti-abuse or platform safety controls.
- Never increase bulk messaging/connection behavior or remove dedupe/Do-Not-Contact protections.
- Prefer root-cause fixes over sleeps/retries, but transient infrastructure errors may be diagnosed without a patch.
- Patch must be a valid git unified diff rooted at repository paths (a/... b/...).
- If confidence is below 0.85 or evidence is insufficient, use action="diagnose" and patch="".
- Keep changes minimal and backward-compatible.

JSON schema:
{{"action":"patch|diagnose","file_path":"agent/...py or empty","patch":"unified diff or empty","reason":"concise root cause and remedy","confidence":0.0,"test_plan":"concise"}}
"""
    client = OpenAI(api_key=OPENAI_API_KEY)
    response = client.responses.create(
        model=OPENAI_MODEL,
        input=prompt,
        max_output_tokens=2600,
    )
    return parse_json_response(response.output_text)


def run_cmd(args: list[str], *, input_text: str | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, input=input_text, text=True, capture_output=True, check=check)


def apply_and_validate(proposal: dict[str, Any]) -> tuple[bool, str]:
    action = str(proposal.get("action") or "").strip().lower()
    file_path = str(proposal.get("file_path") or "").strip()
    patch = str(proposal.get("patch") or "")
    confidence = float(proposal.get("confidence") or 0)
    if action != "patch" or confidence < MIN_PATCH_CONFIDENCE:
        return False, "diagnosis_only"
    if file_path not in EDITABLE_FILES or not patch.strip():
        return False, "patch_not_allowed"
    if any(forbidden in patch.lower() for forbidden in ("captcha bypass", "bypass 2fa", "disable checkpoint", "do_not_contact = false")):
        return False, "security_guard_rejected_patch"

    check_result = run_cmd(["git", "apply", "--check", "-"], input_text=patch, check=False)
    if check_result.returncode != 0:
        return False, "git_apply_check_failed: " + check_result.stderr[-1000:]
    apply_result = run_cmd(["git", "apply", "-"], input_text=patch, check=False)
    if apply_result.returncode != 0:
        return False, "git_apply_failed: " + apply_result.stderr[-1000:]

    compile_result = run_cmd(["python", "-m", "compileall", "-q", "agent"], check=False)
    diff_result = run_cmd(["git", "diff", "--check"], check=False)
    changed = run_cmd(["git", "diff", "--name-only"], check=False).stdout.splitlines()
    valid = compile_result.returncode == 0 and diff_result.returncode == 0 and changed == [file_path]
    if not valid:
        run_cmd(["git", "checkout", "--", file_path], check=False)
        detail = (compile_result.stderr + diff_result.stderr + f" changed={changed}")[-1400:]
        return False, "validation_failed: " + detail
    return True, "validated"


def commit_and_push(reason: str) -> str:
    run_cmd(["git", "config", "user.name", "LOCENIX Repair Agent"])
    run_cmd(["git", "config", "user.email", "actions@users.noreply.github.com"])
    run_cmd(["git", "add", "agent"])
    msg = "Repair LOCENIX agent: " + " ".join(reason.split())[:120]
    run_cmd(["git", "commit", "-m", msg])
    sha = run_cmd(["git", "rev-parse", "HEAD"]).stdout.strip()
    push = run_cmd(["git", "push", "origin", "HEAD:main"], check=False)
    if push.returncode != 0:
        raise RuntimeError("git push failed: " + push.stderr[-1200:])
    return sha


def current_task(db: Client, role: str, fallback: str) -> tuple[str, int, int]:
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
    return "\n\n".join(x for x in (adapter, prompt) if x) or fallback, int(row.get("max_steps") or 40), int(row.get("priority") or 100)


def enqueue_controlled_test(db: Client, job: dict[str, Any], commit_sha: str | None) -> str | None:
    input_data = dict(job.get("input") or {})
    role = str(input_data.get("agent_role") or "").strip().lower()
    if role not in {"inbox", "growth", "lead", "content"}:
        return None
    task, max_steps, priority = current_task(db, role, str(job.get("task") or ""))
    new_input = dict(input_data)
    new_input.update(
        {
            "source": "llm-repair-test",
            "repair_of": str(job["id"]),
            "llm_repair_count": int(input_data.get("llm_repair_count") or 0) + 1,
            "supervisor_retry_count": 0,
            "scheduled": False,
            "repair_commit": commit_sha,
        }
    )
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
        or []
    )
    return str(created[0]["id"]) if created else None


def run() -> dict[str, Any]:
    if not OPENAI_API_KEY:
        return {"enabled": False, "reason": "OPENAI_API_KEY missing"}
    db = db_client()
    failed_after = (datetime.now(timezone.utc) - timedelta(hours=FAILED_LOOKBACK_HOURS)).isoformat()
    failed = (
        db.table("agent_jobs")
        .select("id,status,created_at,updated_at,task,mode,priority,max_steps,input,error,result")
        .eq("status", "failed")
        .gte("updated_at", failed_after)
        .order("updated_at", desc=True)
        .limit(30)
        .execute()
        .data
        or []
    )

    for job in failed:
        job_id = str(job["id"])
        if not needs_llm_repair(job) or has_repair_activity(db, job_id):
            continue
        add_event(db, job_id, "repair.started", "LLM repair diagnosis started", {"model": OPENAI_MODEL})
        try:
            proposal = request_repair(job)
            reason = str(proposal.get("reason") or "No reason supplied")
            add_event(db, job_id, "repair.diagnosed", reason, {"proposal": proposal})
            applied, state = apply_and_validate(proposal)
            if not applied:
                add_event(db, job_id, "repair.escalated", "Repair did not auto-patch; diagnosis recorded", {"state": state, "reason": reason})
                return {"job_id": job_id, "action": "diagnose", "state": state, "reason": reason}

            commit_sha = commit_and_push(reason)
            add_event(db, job_id, "repair.patch_applied", "Validated repair committed and pushed", {"commit_sha": commit_sha, "reason": reason})
            test_id = enqueue_controlled_test(db, job, commit_sha)
            if test_id:
                add_event(db, job_id, "repair.test_enqueued", "Controlled post-repair test job queued", {"test_job_id": test_id, "commit_sha": commit_sha})
            return {"job_id": job_id, "action": "patch", "commit_sha": commit_sha, "test_job_id": test_id, "reason": reason}
        except Exception as exc:
            add_event(db, job_id, "repair.escalated", "Repair agent failed safely", {"error": str(exc)[:1800]})
            return {"job_id": job_id, "action": "failed_safe", "error": str(exc)}

    return {"action": "none", "reason": "No eligible failed job requires LLM repair"}


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
