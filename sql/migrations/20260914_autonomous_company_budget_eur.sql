-- Align the live Run 1 ledger with the mission's EUR 30 hard cap.
alter table public.company_cost_events
  add column if not exists currency text not null default 'USD',
  add column if not exists fx_rate_to_eur numeric(12,6) not null default 1.000000,
  add column if not exists amount_eur numeric(12,6)
    generated always as (amount_usd * fx_rate_to_eur) stored;

do $$
begin
  alter table public.company_cost_events add constraint company_cost_events_fx_positive
    check (fx_rate_to_eur > 0);
exception when duplicate_object then null;
end $$;

insert into public.agent_config(key,value)
values ('locenix_company_budget_v1',
  '{"limit_eur":30,"reserve_eur":3,"strong_model_limit_eur":4.5,"usd_to_eur_guard_rate":1.0,"policy":"conservative_until_trusted_fx"}'::jsonb)
on conflict (key) do update set value=excluded.value, updated_at=now();

drop view if exists public.company_mission_control;
drop view if exists public.company_budget_status;

create view public.company_budget_status
with (security_invoker=true) as
with month_window as (
  select
    date_trunc('month', now() at time zone 'Europe/Berlin') at time zone 'Europe/Berlin' as starts_at,
    (date_trunc('month', now() at time zone 'Europe/Berlin') + interval '1 month') at time zone 'Europe/Berlin' as ends_at
), ledger as (
  select
    coalesce(sum(amount_eur) filter (where category='llm'),0) as llm_ledger_eur,
    coalesce(sum(amount_eur) filter (where category<>'llm'),0) as non_llm_eur
  from public.company_cost_events, month_window
  where occurred_at >= starts_at and occurred_at < ends_at
), totals as (
  select greatest(public.agent_monthly_llm_cost(now()),llm_ledger_eur)+non_llm_eur as spent_eur
  from ledger
)
select 30.0000::numeric as limit_eur,
       spent_eur,
       greatest(0::numeric,30.0000-spent_eur) as remaining_eur,
       spent_eur >= 30.0000 as hard_stop,
       1.000000::numeric as usd_to_eur_guard_rate
from totals;

create view public.company_mission_control
with (security_invoker=true) as
select
  now() as observed_at,
  count(*) filter (where j.status='queued')::int as queued_jobs,
  count(*) filter (where j.status='running')::int as running_jobs,
  count(*) filter (where j.status='failed')::int as failed_jobs,
  (select count(*)::int from public.company_incidents where status <> 'resolved') as open_incidents,
  (select spent_eur from public.company_budget_status) as monthly_spend_eur,
  (select remaining_eur from public.company_budget_status) as monthly_remaining_eur,
  (select count(*)::int from public.company_worker_heartbeats where last_seen_at > now()-interval '5 minutes') as live_workers
from public.agent_jobs j;

revoke all on public.company_budget_status, public.company_mission_control from anon, authenticated;
