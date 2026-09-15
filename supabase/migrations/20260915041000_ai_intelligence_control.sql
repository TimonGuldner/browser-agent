-- Central AI telemetry/cache on the existing event, memory and cost ledgers.
alter table public.company_cost_reservations drop constraint if exists company_cost_reservations_model_tier_check;
alter table public.company_cost_reservations add constraint company_cost_reservations_model_tier_check
 check(model_tier in ('deterministic','small','cheap','standard','strong'));
alter table public.company_agents drop constraint if exists company_agents_model_tier_check;
alter table public.company_agents add constraint company_agents_model_tier_check
 check(model_tier in ('deterministic','small','cheap','standard','strong'));

update public.company_agents set model_tier='cheap',updated_at=now()
where model_tier='small' and worker_type<>'browser_worker';
update public.company_agents set model_tier='standard',updated_at=now()
where agent_id='GITHUB_BROWSER_WORKER';

create or replace function public.company_ai_event(
 p_run_id uuid,p_task_id uuid,p_agent_id text,p_department text,
 p_event_type text,p_status text,p_message text,p_cost_eur numeric default 0,p_metadata jsonb default '{}')
returns bigint language plpgsql security invoker set search_path=public as $$
begin
 if p_event_type not in (
  'AI_ROUTING_TIER_0','AI_ROUTING_CHEAP','AI_ESCALATED_STANDARD','AI_ESCALATED_STRONG',
  'AI_OUTPUT_INVALID','AI_CACHE_HIT','AI_PROVIDER_FAILED','AI_FALLBACK_USED','AI_CALL_COMPLETED'
 ) then raise exception 'unsupported AI event';end if;
 return company_record_event(p_run_id,p_agent_id,p_department,p_task_id,p_event_type,p_status,p_message,p_cost_eur,
  coalesce(p_metadata,'{}')||jsonb_build_object('intelligence_layer','central'));
end $$;

create or replace function public.company_ai_finalize_call(
 p_reservation_id uuid,p_result_status text,p_usage jsonb default '{}')
returns uuid language plpgsql security invoker set search_path=public as $$
declare c company_cost_events%rowtype;event_name text;
begin
 update company_cost_events set units=coalesce(units,'{}')||jsonb_strip_nulls(jsonb_build_object(
  'result_status',p_result_status,
  'model_tier',p_usage->>'model_tier',
  'input_tokens',coalesce(p_usage->'prompt_tokens',p_usage->'input_tokens'),
  'output_tokens',coalesce(p_usage->'completion_tokens',p_usage->'output_tokens'),
  'cached_tokens',p_usage->'cached_tokens',
  'latency_ms',coalesce(p_usage->'llm_latency_ms',p_usage->'total_latency_ms'),
  'purpose',coalesce(p_usage->>'purpose',p_usage->>'llm_task_type'),
  'workflow',p_usage->>'workflow',
  'actual_cost_eur',p_usage->'actual_cost_eur',
  'estimated_cost_eur',p_usage->'estimated_cost_eur'
 ))
 where reservation_id=p_reservation_id returning * into c;
 if not found then return null;end if;
 event_name:=case when p_result_status='provider_failed' then 'AI_PROVIDER_FAILED' else 'AI_CALL_COMPLETED' end;
 perform company_ai_event(c.run_id,c.job_id,coalesce(c.agent_id,'LLM_ROUTER'),coalesce(c.department,'CFO'),
  event_name,p_result_status,case when p_result_status='provider_failed' then 'AI provider call failed' else 'AI call telemetry recorded' end,
  c.amount_eur,jsonb_build_object('provider',c.provider,'model',c.service,'reservation_id',p_reservation_id)||
  jsonb_strip_nulls(p_usage));
 return c.id;
end $$;

create or replace function public.company_ai_cache_get(p_cache_key text)
returns jsonb language sql stable security invoker set search_path=public as $$
 select value->'result' from company_memory
 where scope='ai_cache' and memory_key=p_cache_key and (expires_at is null or expires_at>now())
$$;

create or replace function public.company_ai_cache_put(p_cache_key text,p_value jsonb,p_ttl_seconds int)
returns uuid language plpgsql security invoker set search_path=public as $$
declare v_id uuid;
begin
 if nullif(p_cache_key,'') is null or p_ttl_seconds<60 or p_ttl_seconds>2592000 then
  raise exception 'invalid AI cache request';
 end if;
 insert into company_memory(scope,memory_key,value,confidence,source,evidence,expires_at,department)
 values('ai_cache',p_cache_key,jsonb_build_object('result',p_value),1,'central_ai_control',
        '{"stores_prompt":false,"cache_key_is_hash":true}',now()+make_interval(secs=>p_ttl_seconds),'CFO')
 on conflict(scope,memory_key) do update set value=excluded.value,confidence=1,source=excluded.source,
  evidence=excluded.evidence,expires_at=excluded.expires_at,updated_at=now()
 returning id into v_id;
 return v_id;
end $$;

create or replace view public.company_ai_cost_daily with(security_invoker=true) as
select (occurred_at at time zone 'UTC')::date date,provider,service model,
 coalesce(units->>'model_tier','legacy') model_tier,coalesce(agent_id,'unknown') agent_id,
 coalesce(department,'unknown') department,coalesce(units->>'workflow',units->>'task_type','unknown') workflow,
 count(*) calls,sum(amount_eur) estimated_cost_eur,
 sum(case when jsonb_typeof(units->'actual_cost_eur')='number' then (units->>'actual_cost_eur')::numeric end) actual_cost_eur,
 sum(coalesce((units->>'input_tokens')::bigint,0)) input_tokens,
 sum(coalesce((units->>'output_tokens')::bigint,0)) output_tokens,
 sum(coalesce((units->>'cached_tokens')::bigint,0)) cached_tokens
from company_cost_events where category='llm'
group by 1,2,3,4,5,6,7;

create or replace view public.company_ai_value_summary with(security_invoker=true) as
with costs as (
 select run_id,sum(amount_eur) ai_cost_eur from company_cost_events where category='llm' group by run_id
), outcomes as (
 select run_id,sum(value)filter(where metric_key='visibility_checks') visibility_checks,
 sum(value)filter(where metric_key='trials') trials,
 sum(value)filter(where metric_key='customers') customers,
 sum(value)filter(where metric_key='revenue') revenue
 from company_metric_events group by run_id
)
select coalesce(c.run_id,o.run_id) run_id,coalesce(c.ai_cost_eur,0) ai_cost_eur,
 coalesce(o.visibility_checks,0) visibility_checks,coalesce(o.trials,0) trials,
 coalesce(o.customers,0) customers,coalesce(o.revenue,0) revenue,
 c.ai_cost_eur/nullif(o.visibility_checks,0) cost_per_visibility_check,
 c.ai_cost_eur/nullif(o.trials,0) cost_per_trial,
 c.ai_cost_eur/nullif(o.customers,0) cost_per_customer
from costs c full join outcomes o using(run_id);

create or replace view public.company_ai_health with(security_invoker=true) as
select count(*)filter(where event_type in ('AI_ROUTING_CHEAP','AI_ESCALATED_STANDARD','AI_ESCALATED_STRONG')) routed,
 count(*)filter(where event_type='AI_ESCALATED_STRONG') strong_routed,
 count(*)filter(where event_type='AI_OUTPUT_INVALID') invalid_outputs,
 count(*)filter(where event_type='AI_PROVIDER_FAILED') provider_failures,
 count(*)filter(where event_type='AI_FALLBACK_USED') fallbacks,
 avg(case when metadata->>'llm_latency_ms'~'^[0-9]+([.][0-9]+)?$' then (metadata->>'llm_latency_ms')::numeric end) avg_latency_ms,
 sum(case when metadata->>'prompt_tokens'~'^[0-9]+$' then (metadata->>'prompt_tokens')::bigint else 0 end) input_tokens,
 sum(case when metadata->>'completion_tokens'~'^[0-9]+$' then (metadata->>'completion_tokens')::bigint else 0 end) output_tokens
from agent_events where created_at>=now()-interval '24 hours';

revoke all on function public.company_ai_event(uuid,uuid,text,text,text,text,text,numeric,jsonb) from public,anon,authenticated;
revoke all on function public.company_ai_finalize_call(uuid,text,jsonb) from public,anon,authenticated;
revoke all on function public.company_ai_cache_get(text) from public,anon,authenticated;
revoke all on function public.company_ai_cache_put(text,jsonb,int) from public,anon,authenticated;
grant execute on function public.company_ai_event(uuid,uuid,text,text,text,text,text,numeric,jsonb) to service_role;
grant execute on function public.company_ai_finalize_call(uuid,text,jsonb) to service_role;
grant execute on function public.company_ai_cache_get(text) to service_role;
grant execute on function public.company_ai_cache_put(text,jsonb,int) to service_role;
revoke all on public.company_ai_cost_daily,public.company_ai_value_summary,public.company_ai_health from public,anon,authenticated;
grant select on public.company_ai_cost_daily,public.company_ai_value_summary,public.company_ai_health to service_role;
