-- LOCENIX Autonomous Growth Company: additive Run 1 foundation.
-- This migration EXTENDS the existing agent_jobs queue; it deliberately does
-- not create a second scheduler, queue, browser runtime, or secrets store.

alter table public.agent_jobs
  add column if not exists department text,
  add column if not exists task_type text,
  add column if not exists correlation_id text,
  add column if not exists parent_job_id uuid references public.agent_jobs(id) on delete set null,
  add column if not exists root_job_id uuid references public.agent_jobs(id) on delete set null,
  add column if not exists idempotency_key text,
  add column if not exists available_at timestamptz not null default now(),
  add column if not exists attempt smallint not null default 0,
  add column if not exists max_attempts smallint not null default 3,
  add column if not exists verification_status text not null default 'pending',
  add column if not exists human_gate_required boolean not null default false;

do $$
begin
  alter table public.agent_jobs add constraint agent_jobs_attempt_bounds
    check (attempt >= 0 and max_attempts between 0 and 12 and attempt <= max_attempts);
exception when duplicate_object then null;
end $$;

do $$
begin
  alter table public.agent_jobs add constraint agent_jobs_verification_status_check
    check (verification_status in ('pending','passed','failed','not_required'));
exception when duplicate_object then null;
end $$;

create unique index if not exists agent_jobs_idempotency_uidx
  on public.agent_jobs(idempotency_key) where idempotency_key is not null;
create index if not exists agent_jobs_available_idx
  on public.agent_jobs(status, available_at, priority desc, created_at);
create index if not exists agent_jobs_correlation_idx
  on public.agent_jobs(correlation_id, created_at) where correlation_id is not null;
create index if not exists agent_jobs_parent_idx
  on public.agent_jobs(parent_job_id) where parent_job_id is not null;

create table if not exists public.company_departments (
  department_key text primary key,
  parent_key text references public.company_departments(department_key),
  mission text not null,
  escalation_to text,
  monthly_budget_usd numeric(10,4) check (monthly_budget_usd is null or monthly_budget_usd >= 0),
  enabled boolean not null default true,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

insert into public.company_departments(department_key,parent_key,mission,escalation_to)
values
  ('CEO',null,'Mission, priorities and true human gates',null),
  ('CMO','CEO','Growth portfolio and go-to-market','CEO'),
  ('CTO','CEO','Runtime, repair, test and deployment','CEO'),
  ('CFO','CEO','Budget policy and cost ledger','CEO'),
  ('OPPORTUNITY','CMO','Demand signals and experiments','CMO'),
  ('DISTRIBUTION','CMO','Channels and qualified traffic','CMO'),
  ('OUTREACH','CMO','Permission-gated conversations','CMO'),
  ('CONVERSION','CMO','Visibility checks, trials and paid conversion','CMO'),
  ('ANALYTICS','CMO','Metrics, attribution and learning','CMO'),
  ('WATCHDOG','CEO','Health, incidents and escalations','CEO')
on conflict (department_key) do update
set parent_key=excluded.parent_key, mission=excluded.mission,
    escalation_to=excluded.escalation_to, enabled=true, updated_at=now();

create table if not exists public.company_incidents (
  id uuid primary key default gen_random_uuid(),
  job_id uuid references public.agent_jobs(id) on delete set null,
  root_job_id uuid references public.agent_jobs(id) on delete set null,
  fingerprint text not null,
  failure_code text not null,
  stage text not null default 'FAIL',
  severity text not null default 'medium'
    check (severity in ('low','medium','high','critical')),
  status text not null default 'open'
    check (status in ('open','diagnosing','repairing','verifying','resolved','human_gate')),
  owner_department text references public.company_departments(department_key),
  diagnosis text,
  alternative text,
  human_gate_reason text,
  retry_count smallint not null default 0 check (retry_count between 0 and 12),
  opened_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  resolved_at timestamptz
);
create unique index if not exists company_incidents_open_fingerprint_uidx
  on public.company_incidents(fingerprint) where status <> 'resolved';
create index if not exists company_incidents_status_idx
  on public.company_incidents(status,severity,updated_at desc);

create table if not exists public.company_escalations (
  id uuid primary key default gen_random_uuid(),
  incident_id uuid not null references public.company_incidents(id) on delete cascade,
  from_stage text not null,
  to_stage text not null,
  from_owner text,
  to_owner text,
  reason text not null,
  automatic boolean not null default true,
  evidence jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);
create index if not exists company_escalations_incident_idx
  on public.company_escalations(incident_id,created_at);

create table if not exists public.company_experiments (
  id uuid primary key default gen_random_uuid(),
  name text not null,
  department text not null references public.company_departments(department_key),
  hypothesis text not null,
  channel text,
  status text not null default 'draft'
    check (status in ('draft','queued','running','paused','won','lost','stopped')),
  budget_cap_usd numeric(10,4) not null default 0 check (budget_cap_usd >= 0),
  spent_usd numeric(10,4) not null default 0 check (spent_usd >= 0 and spent_usd <= budget_cap_usd),
  primary_metric text not null,
  success_threshold numeric,
  metadata jsonb not null default '{}'::jsonb,
  started_at timestamptz,
  ended_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.company_metric_events (
  id uuid primary key default gen_random_uuid(),
  metric_key text not null,
  value numeric not null,
  unit text not null default 'count',
  department text references public.company_departments(department_key),
  job_id uuid references public.agent_jobs(id) on delete set null,
  experiment_id uuid references public.company_experiments(id) on delete set null,
  source text not null,
  dimensions jsonb not null default '{}'::jsonb,
  dedupe_key text,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);
create unique index if not exists company_metric_events_dedupe_uidx
  on public.company_metric_events(dedupe_key) where dedupe_key is not null;
create index if not exists company_metric_events_lookup_idx
  on public.company_metric_events(metric_key,occurred_at desc);

create table if not exists public.company_cost_events (
  id uuid primary key default gen_random_uuid(),
  category text not null check (category in ('llm','tool','browser','api','other')),
  provider text not null,
  service text not null,
  amount_usd numeric(12,6) not null check (amount_usd >= 0),
  job_id uuid references public.agent_jobs(id) on delete set null,
  experiment_id uuid references public.company_experiments(id) on delete set null,
  units jsonb not null default '{}'::jsonb,
  dedupe_key text,
  occurred_at timestamptz not null default now(),
  created_at timestamptz not null default now()
);
create unique index if not exists company_cost_events_dedupe_uidx
  on public.company_cost_events(dedupe_key) where dedupe_key is not null;
create index if not exists company_cost_events_month_idx
  on public.company_cost_events(occurred_at,category);

create table if not exists public.company_memory (
  id uuid primary key default gen_random_uuid(),
  scope text not null,
  memory_key text not null,
  value jsonb not null,
  confidence numeric(4,3) not null default 1 check (confidence between 0 and 1),
  source text not null,
  evidence jsonb not null default '{}'::jsonb,
  expires_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique(scope,memory_key)
);

create table if not exists public.company_worker_heartbeats (
  worker_id text primary key,
  department text references public.company_departments(department_key),
  status text not null check (status in ('idle','running','degraded','blocked','offline')),
  current_job_id uuid references public.agent_jobs(id) on delete set null,
  capabilities jsonb not null default '[]'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  last_seen_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

alter table public.company_departments enable row level security;
alter table public.company_incidents enable row level security;
alter table public.company_escalations enable row level security;
alter table public.company_experiments enable row level security;
alter table public.company_metric_events enable row level security;
alter table public.company_cost_events enable row level security;
alter table public.company_memory enable row level security;
alter table public.company_worker_heartbeats enable row level security;

revoke all on public.company_departments, public.company_incidents,
  public.company_escalations, public.company_experiments,
  public.company_metric_events, public.company_cost_events,
  public.company_memory, public.company_worker_heartbeats
from anon, authenticated;

create or replace function public.claim_agent_job(p_worker text)
returns setof public.agent_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  v_job public.agent_jobs%rowtype;
begin
  select * into v_job
  from public.agent_jobs
  where status = 'queued'
    and available_at <= now()
    and (
      coalesce(input->>'requires_airtable', 'false') <> 'true'
      or p_worker like 'github-actions-airtable-%'
    )
  order by priority desc, created_at asc
  for update skip locked
  limit 1;

  if not found then return; end if;

  update public.agent_jobs
  set status='running', locked_at=now(), locked_by=p_worker, updated_at=now()
  where id=v_job.id
  returning * into v_job;
  return next v_job;
end;
$$;
revoke all on function public.claim_agent_job(text) from public, anon, authenticated;

create or replace view public.company_budget_status
with (security_invoker=true) as
with window as (
  select
    date_trunc('month', now() at time zone 'Europe/Berlin') at time zone 'Europe/Berlin' as starts_at,
    (date_trunc('month', now() at time zone 'Europe/Berlin') + interval '1 month') at time zone 'Europe/Berlin' as ends_at
), ledger as (
  select
    coalesce(sum(amount_usd) filter (where category='llm'),0) as llm_ledger,
    coalesce(sum(amount_usd) filter (where category<>'llm'),0) as non_llm
  from public.company_cost_events, window
  where occurred_at >= starts_at and occurred_at < ends_at
), totals as (
  select greatest(public.agent_monthly_llm_cost(now()),llm_ledger)+non_llm as spent_usd
  from ledger
)
select 30.0000::numeric as limit_usd,
       spent_usd,
       greatest(0::numeric,30.0000-spent_usd) as remaining_usd,
       spent_usd >= 30.0000 as hard_stop
from totals;

create or replace view public.company_mission_control
with (security_invoker=true) as
select
  now() as observed_at,
  count(*) filter (where j.status='queued')::int as queued_jobs,
  count(*) filter (where j.status='running')::int as running_jobs,
  count(*) filter (where j.status='failed')::int as failed_jobs,
  (select count(*)::int from public.company_incidents where status <> 'resolved') as open_incidents,
  (select spent_usd from public.company_budget_status) as monthly_spend_usd,
  (select remaining_usd from public.company_budget_status) as monthly_remaining_usd,
  (select count(*)::int from public.company_worker_heartbeats where last_seen_at > now()-interval '5 minutes') as live_workers
from public.agent_jobs j;

revoke all on public.company_budget_status, public.company_mission_control from anon, authenticated;
