-- Run 3 extends the canonical control plane. No new queue or scheduler.
alter table public.company_experiments add column if not exists deadline timestamptz;
alter table public.company_experiments add column if not exists audience text;
alter table public.company_experiments add column if not exists action text;
alter table public.company_experiments add column if not exists asset text;
alter table public.company_experiments add column if not exists decision text check(decision in ('SCALE','ITERATE','PAUSE','KILL'));
create index if not exists company_experiments_due_idx on public.company_experiments(deadline) where status='running';
insert into public.company_departments(department_key,parent_key,mission,escalation_to)
values('SEO','CMO','Bottom-of-funnel assets with verified visibility-check conversion paths','CMO') on conflict do nothing;
insert into public.company_agents(agent_id,department,worker_type,capabilities,model_tier)
values('GROWTH_EXECUTOR','CTO','worker','["growth_discover","growth_seo","growth_distribute","growth_outreach","growth_analyze","growth_convert","growth_review"]','deterministic') on conflict do nothing;

create or replace function public.company_growth_metric(p_run_id uuid,p_key text,p_value numeric,p_source text,p_dedupe text,p_dimensions jsonb,p_occurred_at timestamptz default now(),p_experiment_id uuid default null)
returns uuid language plpgsql security invoker set search_path=public as $$
declare v_id uuid;
begin
 if p_key not in ('visitors','pageviews','qualified_traffic','visibility_checks','trials','customers','revenue','clicks','impressions','cost') or p_value<0 or p_value is null then raise exception 'invalid metric';end if;
 if nullif(p_dedupe,'') is null then raise exception 'dedupe required';end if;
 if coalesce((p_dimensions->>'is_test')::boolean,false) then raise exception 'test events cannot enter business metrics';end if;
 if p_experiment_id is not null and not exists(select 1 from company_experiments where id=p_experiment_id and run_id=p_run_id) then raise exception 'experiment lineage mismatch';end if;
 insert into company_metric_events(run_id,metric_key,value,source,dedupe_key,dimensions,occurred_at,experiment_id,department,unit)
 values(p_run_id,p_key,p_value,p_source,p_dedupe,p_dimensions,p_occurred_at,p_experiment_id,'ANALYTICS',case when p_key in ('cost','revenue') then 'EUR' else 'count' end)
 on conflict(dedupe_key)where dedupe_key is not null do nothing returning id into v_id;
 if v_id is not null then perform company_record_event(p_run_id,'GROWTH_EXECUTOR','ANALYTICS',null,'analytics.ingested','completed','Verified source metric ingested',0,jsonb_build_object('metric_id',v_id,'metric',p_key));end if;
 return coalesce(v_id,(select id from company_metric_events where dedupe_key=p_dedupe));
end $$;

create or replace function public.company_growth_zero_cost(p_task_id uuid,p_service text)
returns uuid language plpgsql security invoker set search_path=public as $$
declare j agent_jobs%rowtype;v_id uuid;
begin
 select * into strict j from agent_jobs where id=p_task_id and assigned_agent_id='GROWTH_EXECUTOR' and status='running';
 insert into company_cost_events(category,provider,service,amount_usd,currency,fx_rate_to_eur,job_id,dedupe_key,run_id,agent_id,department,units)
 values('other','existing_infrastructure',p_service,0,'EUR',1,j.id,'growth-zero:'||j.id,j.run_id,'GROWTH_EXECUTOR',j.department,
 '{"incremental_paid_api_calls":0,"excludes_existing_hosting_subscription":true}')
 on conflict(dedupe_key)where dedupe_key is not null do nothing returning id into v_id;
 perform company_record_event(j.run_id,'GROWTH_EXECUTOR','CFO',j.id,'cost.recorded','completed','No incremental paid API or model call',0,'{}');return v_id;
end $$;

create or replace function public.company_growth_fail(p_task_id uuid,p_error text)
returns void language plpgsql security invoker set search_path=public as $$
declare j agent_jobs%rowtype;v_stage text;
begin
 select * into strict j from agent_jobs where id=p_task_id and assigned_agent_id='GROWTH_EXECUTOR' and status='running' for update;
 v_stage:=case when j.attempt<j.max_attempts then 'RETRY' else 'DIAGNOSE' end;
 update agent_jobs set status=case when v_stage='RETRY' then 'queued' else 'failed' end,
 attempt=least(max_attempts,attempt+1),available_at=now()+interval '10 minutes',error=p_error,
 locked_at=null,locked_by=null,updated_at=now() where id=j.id;
 insert into company_incidents(job_id,root_job_id,fingerprint,failure_code,stage,owner_department,diagnosis)
 values(j.id,j.root_job_id,'growth:'||j.id,'GROWTH_EXECUTION_FAILED',v_stage,'CTO',p_error)
 on conflict(fingerprint)where status<>'resolved' do update set stage=excluded.stage,diagnosis=excluded.diagnosis,updated_at=now();
 perform company_record_event(j.run_id,'GROWTH_EXECUTOR',j.department,j.id,'growth.failed','failed',p_error,0,jsonb_build_object('next_stage',v_stage));
end $$;

create or replace function public.company_growth_experiment(p_run_id uuid,p_hypothesis text,p_channel text,p_audience text,p_action text,p_asset text,p_metric text,p_expected numeric,p_deadline timestamptz,p_metadata jsonb default '{}')
returns uuid language plpgsql security invoker set search_path=public as $$
declare v_id uuid;
begin
 if p_deadline<=now() or p_deadline>now()+interval '30 days' or p_expected<=0 or p_metric not in ('revenue','customers','trials','visibility_checks') then raise exception 'bounded down-funnel experiment required';end if;
 if nullif(p_hypothesis,'') is null or nullif(p_channel,'') is null or nullif(p_audience,'') is null or nullif(p_action,'') is null or nullif(p_asset,'') is null then raise exception 'complete experiment contract required';end if;
 perform pg_advisory_xact_lock(hashtext('growth-experiment:'||p_run_id||p_asset));
 select id into v_id from company_experiments where run_id=p_run_id and asset=p_asset and status='running';if found then return v_id;end if;
 v_id:=company_start_experiment(p_run_id,'Growth: '||p_asset,'SEO',p_hypothesis,p_metric,p_expected,0,p_channel,p_metadata);
 update company_experiments set deadline=p_deadline,audience=p_audience,action=p_action,asset=p_asset where id=v_id;
 return v_id;
end $$;

create or replace function public.company_growth_review(p_run_id uuid)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare e company_experiments%rowtype;actual numeric;sample numeric;choice text;did uuid;decisions jsonb:='[]';tid uuid;
begin
 if not exists(select 1 from company_runs where id=p_run_id and status='running')then raise exception 'active run required';end if;
 for e in select * from company_experiments where run_id=p_run_id and status='running' and deadline is not null for update loop
  select coalesce(sum(value)filter(where metric_key=e.primary_metric),0),coalesce(sum(value)filter(where metric_key='qualified_traffic'),0) into actual,sample
  from company_metric_events where experiment_id=e.id and occurred_at>=e.started_at and occurred_at<=least(now(),e.deadline)
    and coalesce((dimensions->>'is_test')::boolean,false)=false;
  choice:=null;
  if sample>=coalesce((e.metadata->>'min_sample')::int,20) and actual>=e.success_threshold then choice:='SCALE';
  elsif now()>=e.deadline then choice:=case when sample<coalesce((e.metadata->>'min_sample')::int,20)then 'PAUSE' when actual>0 then 'ITERATE' else 'KILL' end;end if;
  if choice is null then continue;end if;
  insert into company_decisions(run_id,agent_id,department,decision_type,outcome_metric,rationale,decision,evidence)
  values(p_run_id,'CEO','CEO','growth_allocation',e.primary_metric,'Deadline and attributed downstream outcomes control allocation',
    jsonb_build_object('experiment_id',e.id,'action',choice),jsonb_build_object('actual',actual,'expected',e.success_threshold,'sample',sample))returning id into did;
  update company_experiments set decision=choice,decision_id=did,status=case choice when 'SCALE' then 'won' when 'KILL' then 'lost' else 'paused' end,
    allocation_weight=case choice when 'SCALE' then 2 when 'ITERATE' then 0.5 else 0 end,
    result=jsonb_build_object('actual',actual,'sample',sample),ended_at=now(),updated_at=now()where id=e.id;
  tid:=company_create_task(p_run_id,case when choice='SCALE' then 'OPPORTUNITY' else 'CONVERSION' end,
    case when choice='SCALE' then 'growth_discover' else 'growth_convert' end,
    'Apply '||choice||' decision for '||e.asset,case when choice='SCALE' then 850 else 600 end,
    jsonb_build_object('runtime_adapter','growth','decision_id',did,'experiment_id',e.id,'urls',jsonb_build_array(e.asset)),
    'growth-decision:'||did);
  perform company_record_event(p_run_id,'CEO','CEO',tid,'experiment.'||lower(choice),'completed','CEO reallocated experiment resources',0,jsonb_build_object('decision_id',did));
  decisions:=decisions||jsonb_build_array(jsonb_build_object('experiment',e.id,'decision',choice,'task',tid));
 end loop;
 perform company_ceo_strategy_review(p_run_id);
 return jsonb_build_object('decisions',decisions,'evidence','Attributed downstream metrics and deadlines evaluated; decisions persist and generate next tasks');
end $$;

create or replace view public.company_growth_attribution with(security_invoker=true) as
 select run_id,source,coalesce(dimensions->>'channel','unattributed')channel,
 coalesce(dimensions->>'campaign','unattributed')campaign,coalesce(dimensions->>'audience','unknown')audience,
 dimensions->>'asset' asset,dimensions->>'url' url,(occurred_at at time zone 'UTC')::date date,
 sum(value)filter(where metric_key='impressions')impressions,sum(value)filter(where metric_key='clicks')clicks,
 sum(value)filter(where metric_key='visitors')visitors,sum(value)filter(where metric_key='qualified_traffic')qualified_visitors,
 sum(value)filter(where metric_key='visibility_checks')visibility_checks,sum(value)filter(where metric_key='trials')trials,
 sum(value)filter(where metric_key='customers')customers,sum(value)filter(where metric_key='revenue')revenue,
 sum(value)filter(where metric_key='cost')cost
 from company_metric_events where coalesce((dimensions->>'is_test')::boolean,false)=false
 group by run_id,source,dimensions->>'channel',dimensions->>'campaign',dimensions->>'audience',dimensions->>'asset',dimensions->>'url',(occurred_at at time zone 'UTC')::date;
revoke all on public.company_growth_attribution from public,anon,authenticated;
grant select on public.company_growth_attribution to service_role;
do $$ declare f record;begin for f in select oid::regprocedure as sig from pg_proc where pronamespace='public'::regnamespace and proname like 'company_growth_%' loop
 execute format('revoke all on function %s from public,anon,authenticated',f.sig);
 execute format('grant execute on function %s to service_role',f.sig);
end loop;end $$;
CREATE OR REPLACE FUNCTION public.claim_agent_job(p_worker text)
 RETURNS SETOF agent_jobs
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare j public.agent_jobs%rowtype;d text;
begin
 select department into d from public.company_agents where agent_id=p_worker and enabled;
 select * into j from public.agent_jobs where status='queued'and available_at<=now()
 and ((coalesce(input->>'runtime_adapter','')='growth') = (p_worker='GROWTH_EXECUTOR'))
 and (run_id is null or exists(select 1 from company_runs r where r.id=agent_jobs.run_id and r.status='running'))
 and(assigned_agent_id is null or assigned_agent_id=p_worker)
 and(coalesce(input->>'requires_airtable','false')<>'true'or p_worker like'github-actions-airtable-%'or p_worker='GITHUB_BROWSER_WORKER')
 order by priority desc,created_at asc for update skip locked limit 1;
 if not found then return;end if;
 update public.agent_jobs set status='running',locked_at=now(),locked_by=p_worker,
 assigned_agent_id=case when exists(select 1 from public.company_agents where agent_id=p_worker)then coalesce(assigned_agent_id,p_worker)else assigned_agent_id end,
 updated_at=now()where id=j.id returning * into j;
 if exists(select 1 from public.company_agents where agent_id=p_worker)then
  perform public.company_heartbeat(p_worker,'busy',j.id,jsonb_build_object('source','claim_agent_job'));
 end if;
 perform public.company_record_event(j.run_id,p_worker,coalesce(d,j.department),j.id,'worker.claimed','running','Worker claimed task',0,'{}');
 return next j;
end$function$
;
