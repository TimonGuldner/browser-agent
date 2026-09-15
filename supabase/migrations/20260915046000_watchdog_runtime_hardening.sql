-- Close the remaining production gaps found by the Run-4 live verification:
-- durable database-side watchdog cadence, explicit CFO routing telemetry, and
-- deterministic recovery of health incidents after their signal clears.

create or replace function public.company_ai_event(
 p_run_id uuid,p_task_id uuid,p_agent_id text,p_department text,
 p_event_type text,p_status text,p_message text,p_cost_eur numeric default 0,p_metadata jsonb default '{}')
returns bigint language plpgsql security invoker set search_path=public as $$
begin
 if p_event_type not in (
  'AI_ROUTING_TIER_0','AI_ROUTING_CHEAP','AI_ESCALATED_STANDARD','AI_ESCALATED_STRONG',
  'AI_OUTPUT_INVALID','AI_CACHE_HIT','AI_PROVIDER_FAILED','AI_FALLBACK_USED','AI_CALL_COMPLETED',
  'AI_BUDGET_BLOCKED'
 ) then raise exception 'unsupported AI event';end if;
 return company_record_event(p_run_id,p_agent_id,p_department,p_task_id,p_event_type,p_status,p_message,p_cost_eur,
  coalesce(p_metadata,'{}')||jsonb_build_object('intelligence_layer','central'));
end $$;

create or replace function public.company_watchdog_resolve_recovered(
 p_now timestamptz default now(),p_dead_minutes int default 15)
returns int language plpgsql security definer set search_path=public as $$
declare n int;
begin
 update company_incidents i set status='recovered',resolved_at=p_now,updated_at=p_now,
  resolution=coalesce(resolution,'Detector verified recovery')
 where i.status not in('resolved','recovered','failed_final')and(
  (i.failure_code='STALE_TASK'and exists(select 1 from agent_jobs j where j.id=i.job_id and j.status='completed'and j.verification_status='passed'))
  or(i.failure_code='DEAD_WORKER'and exists(select 1 from company_worker_heartbeats h where i.fingerprint='DEAD_WORKER:'||h.worker_id and h.last_seen_at>=p_now-make_interval(mins=>p_dead_minutes)))
  or(i.failure_code='MISSING_HEARTBEAT'and exists(select 1 from company_worker_heartbeats h
   where i.fingerprint='MISSING_HEARTBEAT:'||h.worker_id and h.last_seen_at>=p_now-make_interval(mins=>p_dead_minutes)))
  or(i.failure_code='MISSING_DISTRIBUTION'and(
   not exists(select 1 from company_runs r where i.fingerprint='MISSING_DISTRIBUTION:'||r.id and r.status='running')
   or exists(select 1 from company_runs r join company_metric_events m on m.run_id=r.id
    where i.fingerprint='MISSING_DISTRIBUTION:'||r.id and m.metric_key in('qualified_traffic','clicks','impressions')
     and m.occurred_at>=p_now-interval '24 hours')))
  or(i.failure_code='CFO_BUDGET_GUARD'and exists(select 1 from company_budget_status b
   where not b.hard_stop and b.projected_spend_eur<=b.limit_eur))
  or(i.failure_code in('NO_QUALIFIED_TRAFFIC_24H','NO_VISIBILITY_CHECKS_48H','NO_TRIALS_7D','NO_CUSTOMERS_14D')
   and exists(select 1 from company_metric_events m where m.run_id=i.run_id and m.value>0 and
    m.metric_key=case i.failure_code when'NO_QUALIFIED_TRAFFIC_24H'then'qualified_traffic'
     when'NO_VISIBILITY_CHECKS_48H'then'visibility_checks'when'NO_TRIALS_7D'then'trials'else'customers'end))
 );
 get diagnostics n=row_count;return n;
end $$;

create or replace function public.company_ai_watchdog(p_now timestamptz default now())
returns jsonb language plpgsql security invoker set search_path=public as $$
declare routed int;strong_n int;invalid_n int;provider_n int;tokens bigint;latency numeric;llm_today numeric;
 i uuid;created int:=0;recovered int:=0;open_inc company_incidents%rowtype;healthy boolean;
begin
 select count(*)filter(where event_type in('AI_ROUTING_CHEAP','AI_ESCALATED_STANDARD','AI_ESCALATED_STRONG')),
  count(*)filter(where event_type='AI_ESCALATED_STRONG'),count(*)filter(where event_type='AI_OUTPUT_INVALID'),
  count(*)filter(where event_type='AI_PROVIDER_FAILED'),
  sum(case when metadata->>'prompt_tokens'~'^[0-9]+$'then(metadata->>'prompt_tokens')::bigint else 0 end),
  avg(case when metadata->>'llm_latency_ms'~'^[0-9]+([.][0-9]+)?$'then(metadata->>'llm_latency_ms')::numeric end)
 into routed,strong_n,invalid_n,provider_n,tokens,latency from agent_events where created_at>=p_now-interval '1 hour';
 select coalesce(sum(amount_eur),0)into llm_today from company_cost_events where category='llm'and occurred_at>=date_trunc('day',p_now);

 for open_inc in select * from company_incidents where status not in('resolved','recovered','failed_final')
  and failure_code in('AI_PROVIDER_ERROR_RATE','AI_INVALID_OUTPUT_SPIKE','AI_STRONG_USAGE_SPIKE','AI_TOKEN_SPIKE','AI_LATENCY_SPIKE','AI_DAILY_COST_SPIKE')
 loop
  healthy:=case open_inc.failure_code
   when'AI_PROVIDER_ERROR_RATE'then provider_n<3
   when'AI_INVALID_OUTPUT_SPIKE'then routed<5 or invalid_n::numeric/nullif(routed,0)<0.2
   when'AI_STRONG_USAGE_SPIKE'then routed<5 or strong_n::numeric/nullif(routed,0)<0.8
   when'AI_TOKEN_SPIKE'then coalesce(tokens,0)<=100000
   when'AI_LATENCY_SPIKE'then coalesce(latency,0)<=30000
   else llm_today<=1 end;
  if healthy then
   perform company_incident_transition(open_inc.id,'recovered',open_inc.escalation_level,
    'AI health signal returned below deterministic threshold',jsonb_build_object(
     'resolution','health_threshold_cleared','verification',jsonb_build_object(
      'routed',routed,'strong',strong_n,'invalid',invalid_n,'provider_failures',provider_n,
      'tokens',coalesce(tokens,0),'avg_latency_ms',latency,'llm_cost_today_eur',llm_today),
     'prevention_rule',coalesce(open_inc.prevention_rule,'continue_deterministic_ai_health_monitoring')));
   recovered:=recovered+1;
  end if;
 end loop;

 if provider_n>=3 then
  i:=company_incident_open(null,'ai_provider','provider_errors','AI_PROVIDER_ERROR_RATE','high','At least three AI provider failures in one hour','AI_PROVIDER_ERROR_RATE','deterministic');
  update company_incidents set department='CTO',owner_department='CTO'where id=i and status='open';
  if found then perform company_incident_transition(i,'escalated','L3','CTO must diagnose configured-provider health before more AI calls','{}');end if;created:=created+1;
 end if;
 if routed>=5 and invalid_n::numeric/routed>=0.2 then
  i:=company_incident_open(null,'ai_validation','invalid_output_rate','AI_INVALID_OUTPUT_SPIKE','high','Invalid AI output rate exceeded 20 percent','AI_INVALID_OUTPUT_SPIKE','deterministic');
  update company_incidents set department='CTO',owner_department='CTO'where id=i and status='open';
  if found then perform company_incident_transition(i,'escalated','L2','Specialist must inspect schemas, inputs and provider responses','{}');end if;created:=created+1;
 end if;
 if routed>=5 and strong_n::numeric/routed>=0.8 then
  i:=company_incident_open(null,'llm_router','strong_usage_rate','AI_STRONG_USAGE_SPIKE','high','Strong tier exceeded 80 percent of routed calls','AI_STRONG_USAGE_SPIKE','deterministic');
  update company_incidents set department='CFO',owner_department='CFO'where id=i and status='open';
  if found then perform company_incident_transition(i,'escalated','L2','CFO and specialist must inspect over-routing to strong models','{}');end if;created:=created+1;
 end if;
 if coalesce(tokens,0)>100000 then i:=company_incident_open(null,'ai_provider','token_spike','AI_TOKEN_SPIKE','high','AI input tokens exceeded hourly threshold','AI_TOKEN_SPIKE','deterministic');created:=created+1;end if;
 if coalesce(latency,0)>30000 then i:=company_incident_open(null,'ai_provider','latency','AI_LATENCY_SPIKE','medium','Average AI latency exceeded 30 seconds','AI_LATENCY_SPIKE','deterministic');created:=created+1;end if;
 if llm_today>1 then
  i:=company_incident_open(null,'cfo','cost_spike','AI_DAILY_COST_SPIKE','high','Daily conservative AI charge exceeded EUR 1','AI_DAILY_COST_SPIKE:'||p_now::date,'deterministic');
  update company_incidents set department='CFO',owner_department='CFO'where id=i and status='open';
  if found then perform company_incident_transition(i,'escalated','L1','CFO burn-rate guard is blocking non-essential paid calls','{"prevention_rule":"enforce_projected_spend_guard"}');end if;created:=created+1;
 end if;
 return jsonb_build_object('incidents',created,'recovered',recovered,'routed',routed,'strong',strong_n,'invalid',invalid_n,
  'provider_failures',provider_n,'tokens',coalesce(tokens,0),'avg_latency_ms',latency,'llm_cost_today_eur',llm_today);
end $$;

revoke all on function public.company_ai_event(uuid,uuid,text,text,text,text,text,numeric,jsonb) from public,anon,authenticated;
revoke all on function public.company_watchdog_resolve_recovered(timestamptz,int) from public,anon,authenticated;
revoke all on function public.company_ai_watchdog(timestamptz) from public,anon,authenticated;
grant execute on function public.company_ai_event(uuid,uuid,text,text,text,text,text,numeric,jsonb) to service_role;
grant execute on function public.company_watchdog_resolve_recovered(timestamptz,int) to service_role;
grant execute on function public.company_ai_watchdog(timestamptz) to service_role;

-- Detection must continue if the external GitHub scheduler is delayed.  This
-- invokes the existing watchdog only; it does not create a second task queue.
do $$
begin
 perform cron.unschedule('locenix-company-watchdog');
exception when others then null;
end $$;
select cron.schedule('locenix-company-watchdog','*/5 * * * *',$cron$
 select public.company_watchdog_scan();
$cron$);
