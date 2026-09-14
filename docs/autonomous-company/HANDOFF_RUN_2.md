# LOCENIX Autonomous Company — HANDOFF RUN 2

Stand: 2026-09-14 16:53 UTC  
Canonical repository: `TimonGuldner/browser-agent`  
Control-plane Supabase project: `automation-hub` / `lzqybaxpgwkqyysluivg`

## IMPLEMENTED

- Existing `agent_jobs` queue, `agent_events`, `agent_templates`, `agent_schedules` and the Run-1 company tables were extended. No second queue or parallel scheduler was created.
- Run management: `company_runs` with one-active-run invariant and atomic create/start/finish transitions.
- Department registry: existing canonical ten rows in `company_departments`.
- Agent registry: `company_agents` with department, worker type, status, capabilities and model tier. Current registry has ten entries including CEO, CFO Guard, Watchdog, Scheduler and the physical GitHub browser worker.
- CEO orchestrator: deterministic business ordering
  `revenue > customers > trials > visibility_checks > qualified_traffic > clicks > impressions`.
  It reads actual `company_metric_events`, calculates the highest outcome gap, deduplicates delegation and writes to the existing queue.
- Decision records: verified worker results and strategy reviews persist in `company_decisions`.
- Experiment records: start and threshold evaluation RPCs use the existing `company_experiments`; winners receive weight 2.0 and losers 0.25 only after a minimum sample.
- Company memory: idempotent `company_remember` RPC extends the existing `company_memory`.
- Scheduler: existing `enqueue_due_agent_jobs` now adds run, department, correlation and idempotency lineage plus structured events. Logical roles remain in job input; physical worker assignment happens at claim time.
- Workers: the existing GitHub/Airtable browser worker is registered as `GITHUB_BROWSER_WORKER`, emits heartbeats and structured events, and uses the verified completion RPC for run-bound browser work.
- Event system: the existing `agent_events` now carries `created_at`, `run_id`, `agent_id`, `department`, `task_id`, `event_type`, `status`, `message`, `cost_eur`, and `metadata`.
- CFO control: atomic spend reservations, cost ledger writes, daily/rolling/projected spend, absolute hard cap, burn-rate guard and strong-model justification gate.
- Watchdog: persisted detection for stale tasks, dead physical workers, failed jobs, queue backlog, missing physical-worker heartbeats and missing distribution. Recovered transient incidents are resolved on the next scan.
- Mission Control read model: `company_mission_control` exposes runs, queue, failures, incidents, workers and budget. No dashboard UI or START button was built in this run.
- GitHub Actions: dedicated control-plane workflow executes compile, unit and real Supabase integration tests. Existing closed-loop workflow invokes the Watchdog and uses the registered physical worker ID.

Primary code:

- `agent/control_plane.py`
- `agent/cost_control.py`
- `agent/llm_router.py`
- `agent/local_worker.py`
- `agent/airtable_worker.py`
- `tests/test_control_plane.py`
- `tests/integration_control_plane.py`
- `.github/workflows/control-plane-tests.yml`
- `sql/migrations/20260914_control_plane_schema.sql`
- `sql/migrations/20260914_control_plane_runtime.sql`
- `sql/migrations/20260914_control_plane_foreign_key_indexes.sql`

## TEST RESULTS

Final CI: [LOCENIX Control Plane Tests run 34871273341](https://github.com/TimonGuldner/browser-agent/actions/runs/34871273341) — **success**.

The final workflow passed:

- Python compile check for `agent` and `tests`
- 11 deterministic unit tests
- Real Supabase service-role lifecycle canary
- CREATE RUN
- CEO START
- CREATE TASK
- ASSIGN TASK
- WORKER EVENT
- COMPLETE TASK with required verification evidence
- CEO RECEIVES RESULT
- COST UPDATE through authorization/reservation/ledger
- WATCHDOG DETECTION with a persisted `STALE_TASK` incident
- Structured-event field verification
- Canary cleanup and recovered-incident resolution

A direct database canary also passed before CI: run `e3fe6511-e667-452f-82b7-b908a0f8222d`, task `7f62b1dc-bf58-4b56-ad24-e5f35a2da013`, decision `6e13b807-12c8-4b6d-b416-d3db9b75141a`.

Intermediate failures were real and corrected:

- An invalid YAML line break caused two immediate workflow parse failures.
- The first integration workflow lacked `PYTHONPATH=.`.
- Initial database canaries exposed the existing heartbeat status constraint and generated `amount_eur` column.
- Each issue was fixed and covered by the final green workflow.

No mock replaces the production control plane. Unit tests isolate deterministic policy only; lifecycle acceptance runs against the real Supabase project.

## DATABASE CHANGES

New tables:

- `company_runs`
- `company_agents`
- `company_decisions`
- `company_cost_reservations`

Extended tables:

- `agent_jobs`: run, assignee, due/completed timestamps
- `agent_events`: complete structured event envelope
- `company_experiments`: run, decision, allocation, result
- `company_metric_events`: run
- `company_cost_events`: run, agent, department, reservation
- `company_memory`: run and department

Important RPCs:

- `company_create_run`, `company_start_run`, `company_finish_run`
- `company_create_task`, `company_assign_task`, `company_complete_task`, `company_cancel_task`
- `company_record_event`, `company_heartbeat`
- `company_ceo_receive_result`, `company_ceo_strategy_review`
- `company_start_experiment`, `company_evaluate_experiment`
- `company_authorize_spend`, `company_record_cost`, `company_release_spend`
- `company_remember`
- `company_watchdog_scan`
- extended `claim_agent_job` and `enqueue_due_agent_jobs`

Applied Run-2 migrations:

1. `locenix_control_plane_schema`
2. `locenix_control_plane_rpcs`
3. `locenix_control_plane_watchdog_scheduler`
4. `locenix_control_plane_experiments_memory`
5. `locenix_control_plane_lifecycle`
6. `locenix_control_plane_heartbeat_status_adapter`
7. `locenix_control_plane_cost_generated_column_fix`
8. `locenix_watchdog_recovery_resolution`
9. `locenix_watchdog_scan_with_recovery`
10. `locenix_cfo_projected_spend_guard`
11. `locenix_control_plane_foreign_key_indexes`
12. `locenix_scheduler_physical_worker_assignment_fix`
13. `locenix_agent_registry_department_fix`
14. `locenix_event_driven_worker_health_fix`
15. `locenix_ceo_idle_state_reconcile`

All new control-plane tables use RLS with service-role-only operation. The Supabase advisor reports the expected informational “RLS enabled, no policy” notices; this is intentional for this internal service-role plane. New foreign-key access paths are indexed.

## CEO STATUS

**Implemented and tested; currently idle.**

There is no active mission run at handoff. CEO state is `idle`, not falsely “running”. Starting a run requires an explicit `company_create_run` then `company_start_run` call. The orchestrator then reads persisted business metrics and delegates the highest-priority unmet outcome. Task counts are not an objective.

The legacy `ceo_supervisor.py` remains as an Airtable measurement adapter. It is not a second queue and should be converted into a metric producer in the next run before eventual removal.

## WATCHDOG STATUS

**Detection and recovery logic operational.**

Observed at 2026-09-14 16:53 UTC:

- active runs: 0
- queued scheduled jobs: 3
- running jobs: 0
- historical failed jobs: 157
- open incidents: 87, all `FAILED_JOB`
- physical workers seen in the last five minutes: 1
- canary stale/dead/missing-distribution incidents: resolved
- false missing-heartbeat incidents for event-driven logical agents: resolved and detector corrected

The 87 failed-job incidents are intentionally left open. They reflect real recent historical job failures and require classification/deduplication, not blanket deletion.

## COST GUARD STATUS

**Absolute hard cap and projected-spend guard operational.**

Observed at 2026-09-14 16:53 UTC:

- hard limit: €30.000000 / rolling 30 days
- charged 30-day amount: €4.193706
- remaining: €25.806294
- committed reservations: €0
- daily conservative bookings: €3.151000
- projected 30-day spend at today's rate: €94.530000
- absolute hard stop: false
- non-essential burn-rate guard: active because projection exceeds €30
- strong model without a difficult-decision flag: blocked
- an essential request exceeding remaining budget: blocked by absolute cap

The €3.151 daily value consists largely of explicit per-call upper-bound bookings from the concurrently running legacy workflow, not provider invoice reconciliation. This deliberately fails conservative rather than treating unknown cost as zero.

## OPEN ISSUES

1. Triage 87 open `FAILED_JOB` incidents and group them by root cause before retrying. Do not bulk-mark resolved.
2. Three legitimate scheduled jobs are queued for `inbox`, `lead`, and `growth`; they are now unbound to a logical agent and claimable by the physical worker.
3. The burn-rate guard currently blocks non-essential paid model calls. Reduce call volume, add exact provider price/token reconciliation, then reassess the conservative booking caps.
4. Existing completion paths outside the registered run-bound browser worker still write legacy result shapes. Migrate each worker only when touched; do not fork the queue.
5. Experiment and memory RPCs are implemented but contain no production growth records yet.
6. No production Mission-Control UI or START button exists. Vercel had no LOCENIX project in Run 1.
7. The existing ten-minute GitHub workflow is still a coarse dispatcher. A future START flow must create the run and pass its ID without adding another scheduler.
8. Public dashboard access is intentionally unavailable: current RLS permits service role only. Add a narrowly scoped read API before exposing Mission Control.

## COMMIT HASH

Implementation head before this handoff: `9e85459913f92852a0309cb412c9ed72b3660cda`

Milestone commits:

- `d789a75e847275fb4bbb245763312d5361559007` — CEO client and CFO model guard
- `bf9d98532dd68732f1ec9df9ac63ffd99348887c` — scheduler/watchdog wiring and real canary
- `7beafe8a0eb9ba857df7cd6de4c93265cd2f55ec` — versioned database schema/runtime
- `589411181898d63a5e75544dc34e1966ec060ac0` — projected-spend guard and compile checks
- `9e85459913f92852a0309cb412c9ed72b3660cda` — final scheduler, worker-health and lifecycle alignment

## NEXT RUN REQUIREMENTS

1. Build the minimal Mission-Control API and START endpoint directly over `company_create_run` / `company_start_run`; do not create a second backend.
2. Show real run state, outcome metrics, queue, incidents, workers, experiments and budget from `company_mission_control` plus scoped detail queries.
3. Require idempotency and one-active-run behavior at the START boundary.
4. Convert `ceo_supervisor.py` from legacy objective reporting into a `company_metric_events` producer.
5. Triage the 87 failed-job incidents through the Run-1 failure chain: retry → diagnose → alternative → specialist → CTO.
6. Reconcile model usage with provider-specific pricing. Keep upper-bound booking until exact cost is available.
7. Execute one small production growth canary under the active burn-rate guard. Measure an actual business outcome; do not count tasks as success.
8. Only after the canary is verified, connect the real dashboard START button and live event stream.

## STOP CONDITION

Run 2 stops here because the autonomous control plane is implemented, applied, versioned, compiled and proven through a real end-to-end Supabase canary. Dashboard construction and full growth-agent rollout remain explicitly out of scope.
