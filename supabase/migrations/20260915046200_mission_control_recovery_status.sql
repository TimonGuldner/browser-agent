-- Recovered and final-failed incidents are terminal in the Run-4 lifecycle and
-- must not inflate Mission Control's open count. Align worker freshness with
-- the Watchdog's production threshold rather than the old five-minute value.
create or replace view public.company_mission_control with (security_invoker=true) as
select now() observed_at,
 (select count(*)::int from company_runs where status='running') active_runs,
 (select count(*)::int from agent_jobs where status='queued') queued_jobs,
 (select count(*)::int from agent_jobs where status='running') running_jobs,
 (select count(*)::int from agent_jobs where status='failed') failed_jobs,
 (select count(*)::int from company_incidents where status not in('resolved','recovered','failed_final')) open_incidents,
 (select spent_eur from company_budget_status) spend_30d_eur,
 (select daily_spend from company_budget_status) daily_spend_eur,
 (select projected_spend_eur from company_budget_status) projected_spend_eur,
 (select remaining_eur from company_budget_status) remaining_eur,
 (select hard_stop from company_budget_status) budget_hard_stop,
 (select count(*)::int from company_worker_heartbeats where last_seen_at>now()-interval '15 minutes') live_workers;

revoke all on public.company_mission_control from public,anon,authenticated;
grant select on public.company_mission_control to service_role;
