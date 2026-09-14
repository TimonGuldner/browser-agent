from __future__ import annotations

"""Real Supabase canary for the complete Run 2 control-plane lifecycle."""

import json
import os
import time
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from agent.control_plane import ControlPlaneClient


def main() -> None:
    cp = ControlPlaneClient()
    marker = uuid4().hex
    run_id = cp.create_run(
        f"Control-plane CI canary {marker}",
        {"revenue": 1, "customers": 1, "trials": 1, "visibility_checks": 1,
         "qualified_traffic": 1, "clicks": 1, "impressions": 1},
        {"test": True, "marker": marker},
    )
    cp.start_run(run_id)
    task_id = cp.create_task(
        run_id, "DISTRIBUTION", "integration_canary", "Persist and verify the control-plane lifecycle",
        900, {"test": True, "outcome_metric": "qualified_traffic"}, f"ci-complete:{marker}",
    )
    cp.assign_task(task_id, "GITHUB_BROWSER_WORKER")
    cp.heartbeat("GITHUB_BROWSER_WORKER", "running", task_id)
    event_id = cp.event(
        run_id=run_id, agent_id="GITHUB_BROWSER_WORKER", department="DISTRIBUTION", task_id=task_id,
        event_type="worker.progress", status="running", message="Real integration worker event",
        metadata={"test": True, "marker": marker},
    )
    cp.complete_task(
        task_id, "GITHUB_BROWSER_WORKER", {"observed": True, "marker": marker},
        {"passed": True, "evidence": "Persisted RPC state and event"},
    )
    decision_id = cp.receive_result(task_id)
    authorization = cp.rpc("company_authorize_spend", {
        "p_amount_eur": "0", "p_category": "llm", "p_provider": "integration",
        "p_service": "control-plane-ci", "p_reason": "zero-cost ledger path canary",
        "p_run_id": run_id, "p_job_id": task_id, "p_agent_id": "CFO_GUARD",
        "p_department": "CFO", "p_model_tier": "small", "p_difficult_decision": False,
        "p_metadata": {"test": True, "marker": marker},
    })
    cost_id = cp.rpc("company_record_cost", {
        "p_reservation_id": authorization["reservation_id"], "p_amount_eur": "0",
        "p_dedupe_key": f"ci-cost:{marker}", "p_units": {"test": True},
    })
    stale_id = cp.create_task(
        run_id, "DISTRIBUTION", "watchdog_canary", "Must be detected as stale", 800,
        {"test": True, "outcome_metric": "impressions"}, f"ci-stale:{marker}",
    )
    cp.assign_task(stale_id, "GITHUB_BROWSER_WORKER")
    future = (datetime.now(timezone.utc) + timedelta(minutes=31)).isoformat()
    watchdog = cp.watchdog(now=future, stale_minutes=30, dead_minutes=15, queue_threshold=25,
                           distribution_hours=24, failure_hours=24)
    incidents = cp.get("company_incidents", f"job_id=eq.{stale_id}&failure_code=eq.STALE_TASK&select=id,status")
    if not incidents:
        raise AssertionError("watchdog did not persist STALE_TASK")
    cp.cancel_task(stale_id, "CI canary cleanup")
    cp.finish_run(run_id, summary={"test": True, "marker": marker})
    cp.watchdog()
    events = cp.get(
        "agent_events",
        f"run_id=eq.{run_id}&select=created_at,run_id,agent_id,department,task_id,event_type,status,message,cost_eur,metadata&order=created_at.asc",
    )
    required = {"run.created", "ceo.started", "task.created", "task.assigned", "worker.progress",
                "task.completed", "ceo.result_received", "cost.recorded", "watchdog.scan"}
    observed = {event["event_type"] for event in events}
    missing = required - observed
    if missing:
        raise AssertionError(f"missing structured events: {sorted(missing)}")
    print(json.dumps({
        "create_run": run_id, "ceo_start": True, "create_task": task_id, "assign_task": True,
        "worker_event": event_id, "complete_task": True, "ceo_receives_result": decision_id,
        "cost_update": cost_id, "watchdog_detection": watchdog, "structured_events": len(events),
    }, sort_keys=True))


if __name__ == "__main__":
    main()
