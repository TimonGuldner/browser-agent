# LOCENIX Autonomous Growth Company — HANDOFF RUN 1

Generated: 2026-09-14  
Scope: inventory, architecture decision, cost/failure foundation, additive implementation only.

## CURRENT STATE

LOCENIX is not one empty project. The operating system is split across two active public repositories plus two Supabase projects and four Airtable bases:

- \`TimonGuldner/browser-agent\`: canonical browser runtime and control plane. Main branch plus \`agent0-email-closed-loop\`.
- \`TimonGuldner/locenix-lead-research-agent\`: Maps research, QA, visibility, sales queue, email, Customer Success, a second CEO/Growth-manager layer, and the current static Command Center. Main branch only.
- Supabase \`automation-hub\` (\`lzqybaxpgwkqyysluivg\`, eu-central-1): existing scheduler, queue, events, browser sessions, templates and runtime state.
- Supabase product project \`tpnjaoqocbshqykcliuv\` (eu-north-1): the LOCENIX SaaS product data plane; 88 public tables were observed and left unchanged.
- Airtable remains the current business-data system of record for LinkedIn growth, Sales Pipeline, Customer Success and SEO/content.
- The connected Vercel team is on Pro but returned zero visible projects. The repository contains \`command-center/vercel.json\`, but a live Vercel deployment could not be verified through the connected account. GitHub also reports no deployments for either repository.

At inventory time, the existing Supabase browser queue contained 226 jobs: 68 completed, 153 failed and 5 cancelled. It had 770 operational events, including 94 provider failovers, 50 repair escalations, 25 repair diagnoses and 25 supervisor retries. The existing monthly LLM-cost function reported **USD 4.193706**. Until a trusted FX adapter is connected, it is conservatively charged as **€4.193706** against the euro budget.

A recursive workflow feedback loop was active in \`locenix-lead-research-agent\`: CEO dispatched departments, departments committed state and/or dispatched CEO, then CEO dispatched departments again. This produced commits and Actions runs every few seconds. The loop was stopped in commit \`d7b4fa612540fbcbda18bf8084be4f0e45ad3a8d\`; no loop-generated commit was observed after the already-queued runs drained at 15:38:41 UTC.

## WHAT EXISTS

### GitHub Actions

\`locenix-lead-research-agent\` has 12 workflows:

- CEO Agent 0
- Customer Success
- Deep QA
- Growth Manager / Department Head
- Email Conversation
- Email Sender
- Intent Department
- Lead Research
- Outreach Controller
- Outreach Drafts
- Sales Queue
- Maps Visibility

\`browser-agent\` has 11 pre-existing workflows:

- canonical hierarchical closed loop
- LinkedIn department head
- email outreach closed loop
- Airtable/connection/Outscraper probes
- Google Ads login/conversion experiments
- Gemini provider test

Run 1 added one secret-free foundation test workflow.

### Existing runtime and agents

- Supabase RPCs \`enqueue_due_agent_jobs\` and \`claim_agent_job\`
- \`agent_jobs\`, \`agent_events\`, \`agent_steps\`, \`agent_approvals\`
- \`agent_templates\`, \`agent_schedules\`, \`agent_runtime_state\`
- local Chromium/Playwright workers on GitHub Actions
- Airtable-aware worker, deterministic lead pipeline, LinkedIn control, email worker
- CEO, Sales Head, Growth Head and Ops Head
- deterministic supervisor, repair agent and bounded retry creation
- LLM router with Google/OpenAI/Anthropic/Browser Use failover
- inbox cost gate, compliance gate, dedupe/state-machine logic
- static Command Center that currently reads result JSON from GitHub

### Airtable inventory

| Base | Material tables and observed counts |
|---|---|
| LOCENIX LinkedIn Growth OS | People 92; Interactions 87; Daily Growth Queue 69; Lead Research 54; Content Engine 8; Content Opportunities 21; Growth Logs 16; CEO Objectives 8 |
| LOCENIX Sales Pipeline | Leads 258 |
| LOCENIX Customer Success | Customers, Trials, Onboarding, Customer Events and Support/Risks exist; all were empty |
| LOCENIX SEO Content Engine | Keywords 20; Content Plan 54; Pinterest Pin Variations 178; Boards 15; Traffic Tasks 4; Buyer Intent Radar 5 |

Airtable is business CRM/content state. It is not the infrastructure task queue.

### Secret configuration

Secret values were neither read nor copied. Repository code/workflows reference:

- Supabase service role
- Airtable PAT/token
- Resend send/inbox keys
- Google, OpenAI, Anthropic and Browser Use model keys
- GitHub Actions token

Presence is only inferable from successful runtime behavior; GitHub secret metadata is not exposed by the connected API. No new secret store was created.

## WHAT WAS CHANGED

### Repository: \`browser-agent\`

- Added \`agent/company_foundation.py\`
  - canonical task envelope and stable idempotency key
  - exact bounded failure path contract
  - explicit human-gate codes
  - deterministic $30 budget policy
  - strong-model justification and sub-cap
  - deterministic-first LLM decision rule
- Extended \`agent/org_architecture.py\`
  - canonical company structure: CEO, CMO, CTO, CFO, Opportunity, Distribution, Outreach, Conversion, Analytics, Watchdog
  - backward-compatible mapping from current Sales/Growth/Ops and worker names
- Added additive Supabase migrations
  - queue metadata, idempotency, correlation, delayed availability, attempts and verification status
  - departments, incidents, escalations, experiments, metric events, cost events, memory and worker heartbeats
  - Mission Control and budget read views
  - covering indexes for new foreign keys
  - \`claim_agent_job\` now ignores jobs whose \`available_at\` is in the future
- Added deterministic unit tests and a GitHub Actions test workflow.

### Repository: \`locenix-lead-research-agent\`

- Removed recursive state-file triggers from the CEO workflow.
- Removed redundant department-to-CEO dispatch steps from Growth Manager and Customer Success.
- Kept schedules, manual dispatch and CEO-to-department correction intact.

### Supabase: \`automation-hub\`

Applied migrations:

- \`locenix_autonomous_company_foundation\`
- \`locenix_autonomous_company_foundation_indexes\`

Verification result:

- all 10 departments seeded
- Mission Control view readable by service role
- queued 0, running 0, failed historical jobs 153, open new-format incidents 0
- monthly budget charge €4.193706, remaining €25.806294, hard stop false
- no unindexed-foreign-key advisory remains for the new company tables
- RLS is enabled with no public policies by design; the advisor reports this as informational because only the service role may access the control plane

## ARCHITECTURE DECISIONS

### Canonical ownership

| Component | Canonical implementation |
|---|---|
| CEO | existing \`ceo_supervisor.py\`, extended through canonical department contracts |
| Departments | code taxonomy plus \`company_departments\` |
| Task Queue | existing \`agent_jobs\`; never Airtable and no second queue |
| Workers | existing \`agent_templates\`, \`agent_schedules\`, GitHub Actions and local Chromium |
| Events | \`agent_events\` for execution; \`company_metric_events\` and \`company_cost_events\` for business facts |
| Incidents | \`company_incidents\` |
| Escalations | append-only \`company_escalations\` |
| Experiments | \`company_experiments\` with per-experiment budget cap |
| Analytics | Airtable business facts plus normalized metric events |
| Costs | existing job LLM cost plus normalized non-LLM/tool ledger |
| Company Memory | scoped, evidence-bearing \`company_memory\` |
| Watchdog | incidents, worker heartbeats and Mission Control aggregation |
| Browser Workers | existing local Chromium workers and browser sessions |
| Mission Control | existing UI shell later switched to Supabase-backed views; no second dashboard in Run 1 |

Current Sales Head and Growth Head remain operational adapters into CMO. Ops Head remains an adapter into CTO. They are not duplicated or removed in Run 1.

### Failure path

Canonical path:

\`TASK → EXECUTE → VERIFY → FAIL → RETRY → DIAGNOSE → ALTERNATIVE → SPECIALIST → CTO → FIX → TEST → DEPLOY → RETRY ORIGINAL TASK → VERIFY → RESOLVED\`

Every transition is machine-owned until an explicit human-gate code occurs: CAPTCHA, 2FA, checkpoint/authwall, payment approval, legal approval, visual approval, account restriction or a genuine owner decision. A normal failure, provider outage or exhausted retry is not by itself an owner escalation.

### €30 survival rules

- hard monthly cap: €30
- protected reserve: €3
- strong-model sub-cap: €4.50
- strong model only for a genuinely difficult decision
- deterministic code wins whenever it can decide safely
- event-driven GitHub worker wake-ups; no permanently running paid agent
- stable idempotency keys and queue uniqueness
- delayed retry through \`available_at\`
- batching/caching remain at worker and Airtable adapter boundaries
- existing USD job-result costs are conservatively charged 1:1 in EUR until a trusted FX adapter exists; new tool costs carry explicit EUR normalization

## DATABASE DECISIONS

- The product Supabase project remains isolated and unchanged.
- The autonomous-company control plane lives in \`automation-hub\`.
- Existing queue tables and RPCs are extended, not cloned.
- New tables have RLS enabled and no anon/authenticated policies.
- Secrets remain in GitHub/Supabase server-side configuration and are never stored in company memory, events or job payloads.
- Airtable is not used for locks, retries or worker claiming.
- Git-tracked JSON result files are transitional read models, not the future coordination source of truth.

## REUSED COMPONENTS

| Disposition | Component | Decision |
|---|---|---|
| KEEP | LOCENIX product Supabase schema | Production product data plane; untouched |
| KEEP | Airtable domain bases | CRM, content, sales and customer-success records |
| KEEP | compliance and Do-Not-Contact gates | Mandatory hard gates |
| REUSE | \`agent_jobs\`, events, steps, approvals | Canonical execution core |
| REUSE | templates, schedules, claim/enqueue RPCs | Canonical scheduler/worker routing |
| REUSE | local Chromium GitHub worker | Existing browser execution |
| REUSE | LLM router, cost gate, supervisor, repair agent | Existing cost/failure primitives |
| EXTEND | org architecture | Canonical 10-department taxonomy with legacy adapters |
| EXTEND | queue schema | Idempotency, lineage, delay, attempts, verification |
| EXTEND | observability | incidents, escalations, metrics, costs, memory, heartbeats |
| EXTEND | Command Center | Later consume Mission Control views and add real START |
| REPLACE | GitHub JSON commits as coordination bus | Gradually move to Supabase events/read models |
| REPLACE | duplicated cron/dispatch feedback | One bounded dispatcher plus event-driven wake-ups |
| REPLACE | free-text failure matching | typed failure codes and stages |
| REPLACE | competing CEO implementations | converge on browser-agent CEO; keep adapter during migration |
| DELETE | nothing in Run 1 | no runtime deletion before usage proof |
| DELETE LATER | obsolete probe/login experiments and legacy \`browser_jobs\` schema | only after references and retention needs are verified |

## KNOWN RISKS

1. Historical queue failure rate is high: 153 of 226 jobs were failed at inventory time. Many failures have blank top-level errors, so current diagnostics are incomplete.
2. The new incident/escalation tables are installed but existing workers do not write them yet; open incident count is therefore zero, not proof of zero failures.
3. Existing supervisor/repair logic still uses free-text markers until Run 2 integration.
4. Cost telemetry is incomplete for providers that return no price and for external tools not yet writing \`company_cost_events\`.
5. The Command Center is static and GitHub-JSON-backed; it has no authenticated START action yet.
6. Vercel deployment ownership is unresolved because the connected team exposes no projects.
7. Two repositories still contain overlapping CEO/department responsibilities.
8. GitHub result commits remain noisy even after the recursive loop fix.
9. The feature branch \`agent0-email-closed-loop\` was not merged or deleted.
10. New indexes are correctly reported as unused immediately after creation; usage needs observation under Run 2 traffic.

## NEXT IMPLEMENTATION STEPS

1. Wire \`airtable_worker\`, supervisor and repair agent to the new job lineage, typed stages, incidents, escalations and heartbeats.
2. Convert only recent/open work to department/correlation metadata; do not blindly backfill all historical jobs.
3. Enforce the budget policy directly in the LLM router and every paid tool adapter; record deduplicated cost events.
4. Replace repeated cron jobs with one cheap dispatcher and bounded event wake-ups.
5. Move GitHub result JSON from coordination state to optional snapshots; stop commits when facts have not changed.
6. Make browser execution emit structured VERIFY evidence before a task can become completed.
7. Extend the existing Command Center with authenticated START, run graph/event stream, incidents and cost display using the new views.
8. Resolve the Vercel project/account mapping before deploying Mission Control.
9. Run a small end-to-end canary: one deterministic task, one browser task, one forced retry, one repair-test chain and one explicit human gate.
10. Only after the canary passes, enable Opportunity/Distribution/Outreach/Conversion workers incrementally.

## COMMIT HASH

- Browser foundation: \`763c7225d5f95b002907a6eb9708ade09f59002a\`
- Migration syntax correction: \`688dadfdc9f54f0cb88986cb7581ed1631cb9fb7\`
- Foundation indexes / verified code head: \`cce7959b1c353bbdff60b8ae803bab0c4c4cdc09\`
- Recursive workflow-loop hotfix: \`d7b4fa612540fbcbda18bf8084be4f0e45ad3a8d\`
- Passing foundation test run: \`34863996640\`

## STOP CONDITION

Run 1 stops here because:

- the real repositories, branches, workflows, integrations, data models, schedules, secrets references, analytics stores and deployment visibility were inventoried;
- the active recursive workflow loop was fixed and observed to drain;
- the canonical minimal architecture was decided without introducing a second queue;
- deterministic contracts and database foundations were implemented;
- all three database migrations succeeded;
- Python compilation and deterministic foundation tests passed in GitHub Actions;
- no dashboard or full Growth-agent fleet was built.

This is the intended successful Run 1 boundary.
