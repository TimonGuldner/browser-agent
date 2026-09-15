from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from supabase import Client, create_client
from agent import llm_router
from agent.ai_control import AIControl, AIRequest
from agent.resilience import RecoveryPlan, plan_recovery

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
FAILED_LOOKBACK_HOURS = 6
MAX_LLM_REPAIRS_PER_CHAIN = 1
MIN_PATCH_CONFIDENCE = 0.85
KNOWN_RETRYABLE_MARKERS = (
    "browserstartevent", "timed out after 30.0s", "watchdog_base", "browser start",
    "invalid agent_role", "invalid run_status", "invalid content engine select value",
    "navigation failed - site unavailable: https://jobs.result/airtable-run-reports",
)
HARD_BLOCK_MARKERS = (
    "captcha", "2fa", "checkpoint", "authwall", "human verification", "monthly llm budget",
    "cfo_budget_blocked", "projected_30d_budget_exceeded",
    "airtable_pat is not configured", "missing api secret",
)
EDITABLE_FILES = {
    "agent/local_worker.py", "agent/airtable_worker.py", "agent/airtable_tools.py",
    "agent/extended_airtable_tools.py", "agent/supervisor.py", "agent/cost_gate.py",
    "agent/profile_patch.py", "agent/llm_router.py",
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
    return any(marker in job_text(job).lower() for marker in HARD_BLOCK_MARKERS)


def is_known_retryable(job: dict[str, Any]) -> bool:
    return any(marker in job_text(job).lower() for marker in KNOWN_RETRYABLE_MARKERS)


def needs_llm_repair(job: dict[str, Any]) -> bool:
    if is_hard_block(job):
        return False
    input_data = job.get("input") or {}
    if int(input_data.get("llm_repair_count") or 0) >= MAX_LLM_REPAIRS_PER_CHAIN:
        return False
    retry_count = int(input_data.get("supervisor_retry_count") or 0)
    return (not is_known_retryable(job)) or retry_count >= 2


def has_repair_activity(db: Client, job_id: str) -> bool:
    events = db.table("agent_events").select("id").eq("job_id", job_id).in_("event_type", ["repair.started", "repair.diagnosed", "repair.patch_applied", "repair.test_enqueued", "repair.escalated"]).limit(1).execute().data or []
    if events:
        return True
    rows = db.table("agent_jobs").select("id,input").order("created_at", desc=True).limit(100).execute().data or []
    return any(str((row.get("input") or {}).get("repair_of") or "") == job_id for row in rows)


def add_event(db: Client, job_id: str, event_type: str, message: str, data: dict[str, Any] | None = None) -> None:
    db.table("agent_events").insert({"job_id": job_id, "event_type": event_type, "message": message, "data": data or {}}).execute()


def ensure_incident(db: Client, job: dict[str, Any]) -> tuple[str, RecoveryPlan]:
    failure = job_text(job)
    plan = plan_recovery(
        failure, attempt=int(job.get("attempt") or 0),
        max_attempts=int(job.get("max_attempts") or 3), component="runtime",
        business_impact="high",
    )
    incident_id = db.rpc("company_incident_open", {
        "p_job_id": str(job["id"]), "p_component": plan.component,
        "p_error_type": "execution", "p_failure_code": plan.failure_code,
        "p_severity": "critical" if plan.human_gate else "high",
        "p_diagnosis": failure[:2000], "p_fingerprint": plan.fingerprint,
        "p_ai_tier": plan.ai_tier,
    }).execute().data
    return str(incident_id), plan


def transition(db: Client, incident_id: str, status: str, level: str, reason: str,
               patch: dict[str, Any] | None = None) -> None:
    db.rpc("company_incident_transition", {
        "p_incident_id": incident_id, "p_status": status, "p_level": level,
        "p_reason": reason[:1200], "p_patch": patch or {},
    }).execute()


def reconcile_repair_tests(db: Client) -> list[dict[str, str]]:
    """A passing child test enables a bounded retry of the original task."""
    rows = db.table("agent_jobs").select("id,input,result,verification_status").eq("status", "completed").order("updated_at", desc=True).limit(100).execute().data or []
    retried: list[dict[str, str]] = []
    for row in rows:
        original_id = str((row.get("input") or {}).get("repair_of") or "")
        if not original_id:
            continue
        incidents = db.table("company_incidents").select("id,status,escalation_level").eq("job_id", original_id).in_("status", ["repairing", "diagnosing", "escalated"]).limit(1).execute().data or []
        if not incidents:
            continue
        incident = incidents[0]
        evidence = {"test_task_id": str(row["id"]), "test_verification": row.get("verification_status"), "test_result": row.get("result") or {}}
        transition(db, str(incident["id"]), "verifying", str(incident.get("escalation_level") or "L3"), "Controlled repair test passed", {"verification": evidence})
        db.rpc("company_record_event", {
            "p_run_id": None, "p_agent_id": "CTO", "p_department": "CTO",
            "p_task_id": original_id, "p_event_type": "TEST_PASS", "p_status": "completed",
            "p_message": "Controlled repair test passed", "p_cost_eur": 0, "p_metadata": evidence,
        }).execute()
        ok = db.rpc("company_recovery_retry", {
            "p_incident_id": str(incident["id"]), "p_backoff_seconds": 0,
            "p_task_id": original_id,
        }).execute().data
        if ok:
            retried.append({"original_task_id": original_id, "test_task_id": str(row["id"]), "incident_id": str(incident["id"])})
    return retried


def repo_context() -> str:
    chunks: list[str] = []
    for rel in CONTEXT_FILES:
        path = Path(rel)
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
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


def request_repair(job: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
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
    retry_count = int(input_data.get("supervisor_retry_count") or 0)
    proposal, meta = AIControl().execute(
        prompt,
        AIRequest(
            task_type="unknown_production_error", purpose="technical_root_cause_and_safe_patch",
            complexity=0.9, risk="high", expected_value_eur=Decimal("50"),
            confidence_required=MIN_PATCH_CONFIDENCE, previous_attempts=retry_count,
            previous_model=input_data.get("previous_repair_model"),
        ),
        {
            "action": ("patch", "diagnose"), "file_path": str, "patch": str,
            "reason": str, "confidence": (int, float), "test_plan": str,
        },
        2600,
    )
    return dict(proposal), dict(meta)


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
    row = db.table("agent_templates").select("prompt,runtime_adapter,max_steps,priority,enabled").eq("role_key", role).single().execute().data
    if not row or not row.get("enabled", True):
        return fallback, 40, 50
    adapter = str(row.get("runtime_adapter") or "").strip()
    prompt = str(row.get("prompt") or "").strip()
    return "\n\n".join(x for x in (adapter, prompt) if x) or fallback, int(row.get("max_steps") or 40), int(row.get("priority") or 50)


def enqueue_controlled_test(db: Client, job: dict[str, Any], commit_sha: str | None) -> str | None:
    input_data = dict(job.get("input") or {})
    role = str(input_data.get("agent_role") or "").strip().lower()
    if role not in {"inbox", "growth", "lead", "content", "outreach"}:
        return None
    task, max_steps, priority = current_task(db, role, str(job.get("task") or ""))
    new_input = dict(input_data)
    new_input.update({"source": "llm-repair-test", "repair_of": str(job["id"]), "llm_repair_count": int(input_data.get("llm_repair_count") or 0) + 1, "supervisor_retry_count": 0, "scheduled": False, "repair_commit": commit_sha})
    if role == "lead":
        new_input["target_new_profiles"] = int(input_data.get("target_new_profiles") or 10)
    created = db.table("agent_jobs").insert({"status": "queued", "task": task, "mode": str(job.get("mode") or "autonomous"), "priority": max(int(job.get("priority") or 0), priority), "max_steps": max_steps, "input": new_input}).execute().data or []
    return str(created[0]["id"]) if created else None


def run() -> dict[str, Any]:
    db = db_client()
    recovered_tests = reconcile_repair_tests(db)
    router = llm_router.router_status(llm_router.TASK_REPAIR_ANALYSIS)
    if not router["slots"]:
        return {"enabled": False, "reason": "No repair-capable LLM provider configured", "router": router, "recovered_tests": recovered_tests}
    failed_after = (datetime.now(timezone.utc) - timedelta(hours=FAILED_LOOKBACK_HOURS)).isoformat()
    failed = db.table("agent_jobs").select("id,status,created_at,updated_at,task,mode,priority,max_steps,input,error,result,run_id,department,assigned_agent_id,attempt,max_attempts").eq("status", "failed").gte("updated_at", failed_after).order("updated_at", desc=True).limit(30).execute().data or []
    for job in failed:
        job_id = str(job["id"])
        incident_id, recovery = ensure_incident(db, job)
        if recovery.human_gate:
            transition(db, incident_id, "human_gate", "L5", "A genuine credential or platform verification gate requires an authorized person")
            continue
        if is_hard_block(job):
            transition(db, incident_id, "escalated", "L1", "Deterministic guard stopped unsafe or over-budget repair", {"prevention_rule": recovery.action})
            continue
        if not needs_llm_repair(job) or has_repair_activity(db, job_id):
            continue
        transition(db, incident_id, "diagnosing", "L2", "Specialist diagnosis started after deterministic runbook lookup", {"ai_tier": recovery.ai_tier})
        add_event(db, job_id, "repair.started", "Routed LLM repair diagnosis started", {"router": router})
        try:
            proposal, meta = request_repair(job)
            reason = str(proposal.get("reason") or "No reason supplied")
            ai_tier = str(meta.get("model_tier") or recovery.ai_tier)
            add_event(db, job_id, "repair.diagnosed", reason, {"proposal": proposal, "llm": meta})
            applied, state = apply_and_validate(proposal)
            if not applied:
                transition(db, incident_id, "escalated", "L3", "CTO received diagnosis but no safe patch was validated", {"root_cause": reason, "ai_tier": ai_tier})
                add_event(db, job_id, "repair.escalated", "Repair did not auto-patch; diagnosis recorded", {"state": state, "reason": reason, "llm": meta})
                return {"job_id": job_id, "incident_id": incident_id, "action": "diagnose", "state": state, "reason": reason, "llm": meta, "recovered_tests": recovered_tests}
            transition(db, incident_id, "repairing", "L3", "Validated minimal patch prepared", {"root_cause": reason, "ai_tier": ai_tier})
            commit_sha = commit_and_push(reason)
            add_event(db, job_id, "repair.patch_applied", "Validated repair committed and pushed", {"commit_sha": commit_sha, "reason": reason, "llm": meta})
            test_id = enqueue_controlled_test(db, job, commit_sha)
            if test_id:
                add_event(db, job_id, "repair.test_enqueued", "Controlled post-repair test job queued", {"test_job_id": test_id, "commit_sha": commit_sha})
            return {"job_id": job_id, "incident_id": incident_id, "action": "patch", "commit_sha": commit_sha, "test_job_id": test_id, "reason": reason, "llm": meta, "recovered_tests": recovered_tests}
        except Exception as exc:
            transition(db, incident_id, "escalated", "L3", "Repair agent failed safely and stopped before deployment", {"root_cause": str(exc)[:1000]})
            add_event(db, job_id, "repair.escalated", "Repair agent failed safely", {"error": str(exc)[:1800]})
            return {"job_id": job_id, "incident_id": incident_id, "action": "failed_safe", "error": str(exc), "recovered_tests": recovered_tests}
    return {"action": "none", "reason": "No eligible failed job requires LLM repair", "router": router, "recovered_tests": recovered_tests}


def main() -> None:
    print(json.dumps(run(), ensure_ascii=False))


if __name__ == "__main__":
    main()
