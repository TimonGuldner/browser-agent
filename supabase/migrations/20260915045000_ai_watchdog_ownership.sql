-- Route AI health incidents to the responsible department, while all health
-- arithmetic and thresholds remain deterministic.
create or replace function public.company_ai_watchdog(p_now timestamptz default now())
returns jsonb language plpgsql security invoker set search_path=public as $$
declare routed int;strong_n int;invalid_n int;provider_n int;tokens bigint;latency numeric;llm_today numeric;i uuid;created int:=0;
begin
 select count(*)filter(where event_type in('AI_ROUTING_CHEAP','AI_ESCALATED_STANDARD','AI_ESCALATED_STRONG')),
  count(*)filter(where event_type='AI_ESCALATED_STRONG'),count(*)filter(where event_type='AI_OUTPUT_INVALID'),
  count(*)filter(where event_type='AI_PROVIDER_FAILED'),
  sum(case when metadata->>'prompt_tokens'~'^[0-9]+$'then(metadata->>'prompt_tokens')::bigint else 0 end),
  avg(case when metadata->>'llm_latency_ms'~'^[0-9]+([.][0-9]+)?$'then(metadata->>'llm_latency_ms')::numeric end)
 into routed,strong_n,invalid_n,provider_n,tokens,latency from agent_events where created_at>=p_now-interval '1 hour';
 select coalesce(sum(amount_eur),0)into llm_today from company_cost_events where category='llm'and occurred_at>=date_trunc('day',p_now);
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
 return jsonb_build_object('incidents',created,'routed',routed,'strong',strong_n,'invalid',invalid_n,
  'provider_failures',provider_n,'tokens',coalesce(tokens,0),'avg_latency_ms',latency,'llm_cost_today_eur',llm_today);
end $$;
revoke all on function public.company_ai_watchdog(timestamptz) from public,anon,authenticated;
grant execute on function public.company_ai_watchdog(timestamptz) to service_role;
