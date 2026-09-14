-- Run 2: extend the existing agent_jobs/event system; do not create a second queue.
create table if not exists public.company_runs (
 id uuid primary key default gen_random_uuid(), mission text not null,
 status text not null default 'created' check(status in('created','running','paused','completed','failed','cancelled')),
 targets jsonb not null default '{}', budget_limit_eur numeric(12,6) not null default 30 check(budget_limit_eur>0 and budget_limit_eur<=30),
 ceo_agent_id text not null default 'CEO', started_at timestamptz, ended_at timestamptz, last_result_at timestamptz,
 metadata jsonb not null default '{}', created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create unique index if not exists company_runs_one_active_uidx on public.company_runs((status)) where status='running';
create index if not exists company_runs_status_idx on public.company_runs(status,created_at desc);

create table if not exists public.company_agents (
 agent_id text primary key, department text not null references public.company_departments(department_key), role_key text,
 worker_type text not null default 'service' check(worker_type in('orchestrator','department_head','worker','browser_worker','service')),
 status text not null default 'idle' check(status in('idle','active','busy','degraded','dead','disabled')), enabled boolean not null default true,
 capabilities jsonb not null default '[]', model_tier text not null default 'deterministic' check(model_tier in('deterministic','small','strong')),
 metadata jsonb not null default '{}', created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create index if not exists company_agents_department_idx on public.company_agents(department,status);

create table if not exists public.company_decisions (
 id uuid primary key default gen_random_uuid(), run_id uuid not null references public.company_runs(id) on delete cascade,
 agent_id text not null, department text not null references public.company_departments(department_key), decision_type text not null,
 outcome_metric text, rationale text not null, decision jsonb not null default '{}', evidence jsonb not null default '{}',
 created_at timestamptz not null default now()
);
create index if not exists company_decisions_run_idx on public.company_decisions(run_id,created_at desc);

create table if not exists public.company_cost_reservations (
 id uuid primary key default gen_random_uuid(), run_id uuid references public.company_runs(id) on delete set null,
 job_id uuid references public.agent_jobs(id) on delete set null, agent_id text,
 department text references public.company_departments(department_key),
 category text not null check(category in('llm','tool','browser','api','other')), provider text not null, service text not null,
 amount_eur numeric(12,6) not null check(amount_eur>=0),
 status text not null default 'reserved' check(status in('reserved','consumed','released','expired')),
 model_tier text not null default 'small' check(model_tier in('deterministic','small','strong')),
 difficult_decision boolean not null default false, reason text not null, metadata jsonb not null default '{}',
 expires_at timestamptz not null default(now()+interval '30 minutes'), created_at timestamptz not null default now(), updated_at timestamptz not null default now()
);
create index if not exists company_cost_reservations_active_idx on public.company_cost_reservations(status,expires_at);

alter table public.agent_jobs add column if not exists run_id uuid references public.company_runs(id) on delete set null;
alter table public.agent_jobs add column if not exists assigned_agent_id text references public.company_agents(agent_id) on delete set null;
alter table public.agent_jobs add column if not exists due_at timestamptz;
alter table public.agent_jobs add column if not exists completed_at timestamptz;
create index if not exists agent_jobs_run_status_idx on public.agent_jobs(run_id,status,priority desc,created_at);
create index if not exists agent_jobs_assignee_idx on public.agent_jobs(assigned_agent_id,status) where assigned_agent_id is not null;

alter table public.agent_events add column if not exists run_id uuid references public.company_runs(id) on delete set null;
alter table public.agent_events add column if not exists agent_id text;
alter table public.agent_events add column if not exists department text references public.company_departments(department_key);
alter table public.agent_events add column if not exists task_id uuid references public.agent_jobs(id) on delete cascade;
alter table public.agent_events add column if not exists status text;
alter table public.agent_events add column if not exists cost_eur numeric(12,6) not null default 0 check(cost_eur>=0);
alter table public.agent_events add column if not exists metadata jsonb not null default '{}';
update public.agent_events set task_id=job_id where task_id is null and job_id is not null;
create index if not exists agent_events_run_created_idx on public.agent_events(run_id,created_at desc);
create index if not exists agent_events_agent_created_idx on public.agent_events(agent_id,created_at desc) where agent_id is not null;

alter table public.company_experiments add column if not exists run_id uuid references public.company_runs(id) on delete set null;
alter table public.company_experiments add column if not exists decision_id uuid references public.company_decisions(id) on delete set null;
alter table public.company_experiments add column if not exists allocation_weight numeric(8,4) not null default 1 check(allocation_weight>=0);
alter table public.company_experiments add column if not exists result jsonb not null default '{}';
alter table public.company_metric_events add column if not exists run_id uuid references public.company_runs(id) on delete set null;
alter table public.company_cost_events add column if not exists run_id uuid references public.company_runs(id) on delete set null;
alter table public.company_cost_events add column if not exists agent_id text;
alter table public.company_cost_events add column if not exists department text references public.company_departments(department_key);
alter table public.company_cost_events add column if not exists reservation_id uuid references public.company_cost_reservations(id) on delete set null;
alter table public.company_memory add column if not exists run_id uuid references public.company_runs(id) on delete set null;
alter table public.company_memory add column if not exists department text references public.company_departments(department_key);

insert into public.company_agents(agent_id,department,role_key,worker_type,capabilities,model_tier) values
('CEO','CEO','AGENT_0_CEO','orchestrator','["prioritize","delegate","experiment","strategy_review"]','deterministic'),
('CFO_GUARD','CFO','LLM_ROUTER','service','["budget_guard","cost_logging","model_routing"]','deterministic'),
('WATCHDOG','WATCHDOG','WATCHDOG','service','["queue_health","worker_health","incident_detection"]','deterministic'),
('SCHEDULER','CTO','SCHEDULER','service','["enqueue_due_jobs"]','deterministic'),
('GITHUB_BROWSER_WORKER','DISTRIBUTION','BROWSER_WORKER','browser_worker','["browser","linkedin","airtable"]','small')
on conflict(agent_id) do update set department=excluded.department,role_key=excluded.role_key,worker_type=excluded.worker_type,
capabilities=excluded.capabilities,model_tier=excluded.model_tier,updated_at=now();

insert into public.company_agents(agent_id,department,role_key,worker_type,capabilities,model_tier)
select t.role_key,case when lower(t.role_key)='inbox' then 'CONVERSION' when lower(t.role_key)='lead' then 'OPPORTUNITY'
when lower(t.role_key) in('growth','content') then 'DISTRIBUTION' when lower(t.role_key)='outreach' then 'OUTREACH'
when t.role_key='AGENT_0_CEO' then 'CEO' when t.role_key in('SALES_HEAD','GROWTH_HEAD') then 'CMO'
when t.role_key in('OPS_HEAD','QUEUE_MANAGER','SCHEDULER','REPAIR_AGENT') then 'CTO'
when t.role_key in('MAPS_OUTSCRAPER_WORKER','CONTACT_ENRICHMENT_WORKER','QUALIFICATION_WORKER') then 'OPPORTUNITY'
when t.role_key in('CHANNEL_TRAFFIC_AGENT','LINKEDIN_RESEARCH_WORKER','ENGAGEMENT_WORKER') then 'DISTRIBUTION'
when t.role_key in('EMAIL_COPY_AGENT','EMAIL_SENDER_WORKER','DM_AGENT') then 'OUTREACH'
when t.role_key in('CONVERSATION_AGENT','FOLLOWUP_WORKER') then 'CONVERSION'
when t.role_key='ANALYTICS_LEARNING_AGENT' then 'ANALYTICS' when t.role_key='LLM_ROUTER' then 'CFO'
when t.role_key in('WATCHDOG','RETRY_CONTROLLER') then 'WATCHDOG' else 'CTO' end,
t.role_key,'worker',jsonb_build_array(t.runtime_adapter),'small' from public.agent_templates t
on conflict(agent_id) do update set updated_at=now();

alter table public.company_runs enable row level security;
alter table public.company_agents enable row level security;
alter table public.company_decisions enable row level security;
alter table public.company_cost_reservations enable row level security;
revoke all on public.company_runs,public.company_agents,public.company_decisions,public.company_cost_reservations from anon,authenticated;
grant select,insert,update,delete on public.company_runs,public.company_agents,public.company_decisions,public.company_cost_reservations to service_role;
