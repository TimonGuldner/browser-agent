from __future__ import annotations

"""LOCENIX autonomous company control plane.

This module is a thin service-role client over the existing Supabase queue. It
contains deterministic CEO policy; all state transitions remain atomic RPCs.
"""

import argparse
import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping

OUTCOME_ORDER = (
    "revenue",
    "customers",
    "trials",
    "visibility_checks",
    "qualified_traffic",
    "clicks",
    "impressions",
)
OUTCOME_PRIORITY = {name: 1000 - index * 100 for index, name in enumerate(OUTCOME_ORDER)}
OUTCOME_DEPARTMENT = {
    "revenue": "CONVERSION",
    "customers": "CONVERSION",
    "trials": "CONVERSION",
    "visibility_checks": "CONVERSION",
    "qualified_traffic": "DISTRIBUTION",
    "clicks": "DISTRIBUTION",
    "impressions": "DISTRIBUTION",
}


class ControlPlaneError(RuntimeError):
    pass


Transport = Callable[[str, Mapping[str, Any]], Any]


class ControlPlaneClient:
    def __init__(self, url: str | None = None, key: str | None = None, transport: Transport | None = None):
        self.url = (url or os.getenv("SUPABASE_URL", "")).rstrip("/")
        self.key = key or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "")
        self.transport = transport
        if not self.transport and (not self.url or not self.key):
            raise ControlPlaneError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required")

    def rpc(self, name: str, payload: Mapping[str, Any]) -> Any:
        if self.transport:
            return self.transport(name, payload)
        request = urllib.request.Request(
            f"{self.url}/rest/v1/rpc/{name}",
            data=json.dumps(dict(payload), default=str).encode(),
            method="POST",
            headers={
                "apikey": self.key,
                "Authorization": f"Bearer {self.key}",
                "Content-Type": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read().decode()
                return json.loads(body) if body else None
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")[:2000]
            raise ControlPlaneError(f"{name} failed: HTTP {exc.code}: {body}") from exc

    def get(self, resource: str, query: str) -> Any:
        request = urllib.request.Request(
            f"{self.url}/rest/v1/{resource}?{query}",
            headers={"apikey": self.key, "Authorization": f"Bearer {self.key}"},
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read().decode())

    def create_run(self, mission: str, targets: Mapping[str, Any], metadata: Mapping[str, Any] | None = None) -> str:
        return str(self.rpc("company_create_run", {"p_mission": mission, "p_targets": targets, "p_metadata": metadata or {}}))

    def start_run(self, run_id: str) -> Mapping[str, Any]:
        return self.rpc("company_start_run", {"p_run_id": run_id})

    def create_task(self, run_id: str, department: str, task_type: str, task: str, priority: int,
                    payload: Mapping[str, Any], idempotency_key: str) -> str:
        return str(self.rpc("company_create_task", {
            "p_run_id": run_id, "p_department": department, "p_task_type": task_type,
            "p_task": task, "p_priority": priority, "p_input": payload,
            "p_idempotency_key": idempotency_key,
        }))

    def assign_task(self, task_id: str, agent_id: str) -> Mapping[str, Any]:
        return self.rpc("company_assign_task", {"p_task_id": task_id, "p_agent_id": agent_id})

    def event(self, *, run_id: str | None, agent_id: str, department: str, task_id: str | None,
              event_type: str, status: str, message: str, cost_eur: Decimal | str = "0",
              metadata: Mapping[str, Any] | None = None) -> int:
        return int(self.rpc("company_record_event", {
            "p_run_id": run_id, "p_agent_id": agent_id, "p_department": department,
            "p_task_id": task_id, "p_event_type": event_type, "p_status": status,
            "p_message": message, "p_cost_eur": str(cost_eur), "p_metadata": metadata or {},
        }))

    def complete_task(self, task_id: str, agent_id: str, result: Mapping[str, Any],
                      verification: Mapping[str, Any]) -> Mapping[str, Any]:
        return self.rpc("company_complete_task", {
            "p_task_id": task_id, "p_agent_id": agent_id,
            "p_result": result, "p_verification": verification,
        })

    def receive_result(self, task_id: str) -> str:
        return str(self.rpc("company_ceo_receive_result", {"p_task_id": task_id}))

    def heartbeat(self, agent_id: str, status: str = "active", task_id: str | None = None) -> None:
        self.rpc("company_heartbeat", {"p_agent_id": agent_id, "p_status": status, "p_task_id": task_id, "p_metadata": {}})

    def watchdog(self, **overrides: Any) -> Mapping[str, Any]:
        payload = {f"p_{key}": value for key, value in overrides.items()}
        return self.rpc("company_watchdog_scan", payload)

    def budget(self) -> Mapping[str, Any]:
        rows = self.get("company_budget_status", "select=*")
        return rows[0] if rows else {}


@dataclass(frozen=True)
class PriorityDecision:
    outcome: str
    department: str
    priority: int
    target: Decimal
    actual: Decimal
    gap: Decimal


def select_priority(targets: Mapping[str, Any], actuals: Mapping[str, Any]) -> PriorityDecision | None:
    """Select the highest business outcome with a positive target gap."""
    for outcome in OUTCOME_ORDER:
        target = Decimal(str(targets.get(outcome, 0)))
        actual = Decimal(str(actuals.get(outcome, 0)))
        if target > actual:
            return PriorityDecision(outcome, OUTCOME_DEPARTMENT[outcome], OUTCOME_PRIORITY[outcome], target, actual, target - actual)
    return None


def task_key(run_id: str, decision: PriorityDecision) -> str:
    raw = f"ceo-v2:{run_id}:{decision.outcome}:{decision.target}:{decision.actual}"
    return hashlib.sha256(raw.encode()).hexdigest()


class CEOOrchestrator:
    def __init__(self, control: ControlPlaneClient):
        self.control = control

    def delegate_gap(self, run_id: str, targets: Mapping[str, Any], actuals: Mapping[str, Any]) -> Mapping[str, Any]:
        decision = select_priority(targets, actuals)
        if decision is None:
            self.control.event(
                run_id=run_id, agent_id="CEO", department="CEO", task_id=None,
                event_type="ceo.targets_met", status="completed",
                message="All configured business outcome targets are met",
                metadata={"targets": dict(targets), "actuals": dict(actuals)},
            )
            return {"status": "targets_met"}
        payload = {
            "outcome_metric": decision.outcome,
            "target": str(decision.target),
            "actual": str(decision.actual),
            "gap": str(decision.gap),
            "rule": "business_outcome_priority",
        }
        task_id = self.control.create_task(
            run_id, decision.department, f"grow_{decision.outcome}",
            f"Produce verified incremental {decision.outcome}; current gap {decision.gap}.",
            decision.priority, payload, task_key(run_id, decision),
        )
        return {"status": "delegated", "task_id": task_id, **payload, "department": decision.department,
                "priority": decision.priority}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("create-run", "start-run", "ceo-cycle", "watchdog", "status"))
    parser.add_argument("--run-id")
    parser.add_argument("--mission", default="Generate measurable LOCENIX growth outcomes within 30 EUR / 30 days")
    parser.add_argument("--targets", default="{}")
    parser.add_argument("--actuals", default="{}")
    args = parser.parse_args()
    cp = ControlPlaneClient()
    if args.command == "create-run":
        output = {"run_id": cp.create_run(args.mission, json.loads(args.targets))}
    elif args.command == "start-run":
        output = cp.start_run(args.run_id or os.environ["LOCENIX_RUN_ID"])
    elif args.command == "ceo-cycle":
        output = CEOOrchestrator(cp).delegate_gap(
            args.run_id or os.environ["LOCENIX_RUN_ID"], json.loads(args.targets), json.loads(args.actuals)
        )
    elif args.command == "watchdog":
        output = cp.watchdog()
    else:
        output = cp.budget()
    print(json.dumps(output, ensure_ascii=False, default=str))


if __name__ == "__main__":
    main()
