-- Extreme recovery extends existing incidents, events, memory and agent_jobs.
alter table public.company_incidents add column if not exists run_id uuid references public.company_runs(id) on delete set null;
alter table public.company_incidents add column if not exists agent_id text;
alter table public.company_incidents add column if not exists department text references public.company_departments(department_key);
alter table public.company_incidents add column if not exists component text;
alter table public.company_incidents add column if not exists error_type text;
alter table public.company_incidents add column if not exists root_cause text;
alter table public.company_incidents add column if not exists attempted_fixes jsonb not null default '[]';
alter table public.company_incidents add column if not exists escalation_level text not null default 'L0';
alter table public.company_incidents add column if not exists ai_tier_used text not null default 'deterministic';
alter table public.company_incidents add column if not exists ai_cost_eur numeric(12,6) not null default 0;
alter table public.company_incidents add column if not exists resolution text;
alter table public.company_incidents add column if not exists verification jsonb not null default '{}';
alter table public.company_incidents add column if not exists prevention_rule text;
alter table public.company_incidents drop constraint if exists company_incidents_status_check;
alter table public.company_incidents add constraint company_incidents_status_check check(status in(
 'open','diagnosing','retrying','escalated','repairing','verifying',
 'resolved','recovered','human_gate','failed_final'
));
alter table public.company_incidents add constraint company_incidents_escalation_level_check
 check(escalation_level in('L0','L1','L2','L3','L4','L5')) not valid;
alter table public.company_incidents validate constraint company_incidents_escalation_level_check;
alter table public.company_incidents add constraint company_incidents_ai_tier_check
 check(ai_tier_used in('deterministic','cheap','standard','strong')) not valid;
alter table public.company_incidents validate constraint company_incidents_ai_tier_check;

create table if not exists public.company_incident_tasks(
 incident_id uuid not null references public.company_incidents(id) on delete cascade,
 task_id uuid not null references public.agent_jobs(id) on delete cascade,
 created_at timestamptz not null default now(),
 primary key(incident_id,task_id)
);
alter table public.company_incident_tasks enable row level security;
revoke all on public.company_incident_tasks from public,anon,authenticated;
grant select,insert,update,delete on public.company_incident_tasks to service_role;
create index if not exists company_incident_tasks_task_idx on public.company_incident_tasks(task_id);
create index if not exists company_incidents_run_status_idx on public.company_incidents(run_id,status,updated_at desc);
create index if not exists company_incidents_component_idx on public.company_incidents(component,status);

drop index if exists public.company_incidents_open_fingerprint_uidx;
create unique index company_incidents_open_fingerprint_uidx on public.company_incidents(fingerprint)
 where status not in('resolved','recovered','failed_final');

create or replace function public.company_incident_open(
 p_job_id uuid,p_component text,p_error_type text,p_failure_code text,p_severity text,
 p_diagnosis text,p_fingerprint text,p_ai_tier text default 'deterministic')
returns uuid language plpgsql security invoker set search_path=public as $$
declare j agent_jobs%rowtype;i uuid;created boolean;
begin
 if p_severity not in('low','medium','high','critical') then raise exception 'invalid severity';end if;
 if p_ai_tier not in('deterministic','cheap','standard','strong') then raise exception 'invalid AI tier';end if;
 if nullif(p_fingerprint,'') is null then raise exception 'fingerprint required';end if;
 if p_job_id is not null then select * into j from agent_jobs where id=p_job_id;end if;
 insert into company_incidents(job_id,root_job_id,run_id,agent_id,department,fingerprint,failure_code,
  stage,severity,status,owner_department,diagnosis,component,error_type,ai_tier_used)
 values(p_job_id,coalesce(j.root_job_id,p_job_id),j.run_id,j.assigned_agent_id,j.department,p_fingerprint,
  p_failure_code,'FAIL',p_severity,'open',coalesce(j.department,'WATCHDOG'),p_diagnosis,p_component,p_error_type,p_ai_tier)
 on conflict(fingerprint)where status not in('resolved','recovered','failed_final')
 do update set diagnosis=excluded.diagnosis,updated_at=now()
 returning id,(xmax=0) into i,created;
 if p_job_id is not null then
  insert into company_incident_tasks(incident_id,task_id)values(i,p_job_id)on conflict do nothing;
 end if;
 if created then
  perform company_record_event(j.run_id,'WATCHDOG',coalesce(j.department,'WATCHDOG'),p_job_id,
   'INCIDENT_CREATED','open','Incident created',0,
   jsonb_build_object('incident_id',i,'failure_code',p_failure_code,'component',p_component,'severity',p_severity));
 end if;
 return i;
end $$;

create or replace function public.company_incident_transition(
 p_incident_id uuid,p_status text,p_level text,p_reason text,p_patch jsonb default '{}')
returns company_incidents language plpgsql security invoker set search_path=public as $$
declare old company_incidents%rowtype;newrow company_incidents%rowtype;to_owner text;event_name text;
begin
 select * into strict old from company_incidents where id=p_incident_id for update;
 if p_status not in('diagnosing','retrying','escalated','repairing','verifying','recovered','human_gate','failed_final') then
  raise exception 'invalid incident transition';
 end if;
 if p_level not in('L0','L1','L2','L3','L4','L5') then raise exception 'invalid escalation level';end if;
 if old.status in('resolved','recovered','failed_final') then return old;end if;
 if p_level='L5' and p_status<>'human_gate' then raise exception 'L5 reserved for human gates';end if;
 if p_status='human_gate' and p_level<>'L5' then raise exception 'human gate must be L5';end if;
 to_owner:=case p_level when'L0'then'WORKER'when'L1'then coalesce(old.department,old.owner_department)
  when'L2'then'SPECIALIST'when'L3'then case when old.department in('CMO','DISTRIBUTION','OUTREACH','CONVERSION','SEO','OPPORTUNITY')then'CMO'else'CTO'end
  when'L4'then'CEO'else'OWNER'end;
 update company_incidents set status=p_status,stage=upper(p_status),escalation_level=p_level,
  retry_count=retry_count+case when p_status='retrying'then 1 else 0 end,
  attempted_fixes=attempted_fixes||case when p_status in('retrying','repairing')then jsonb_build_array(jsonb_build_object('at',now(),'action',p_reason))else'[]'::jsonb end,
  root_cause=coalesce(p_patch->>'root_cause',root_cause),resolution=coalesce(p_patch->>'resolution',resolution),
  verification=verification||coalesce(p_patch->'verification','{}'),prevention_rule=coalesce(p_patch->>'prevention_rule',prevention_rule),
  ai_tier_used=coalesce(p_patch->>'ai_tier',ai_tier_used),
  ai_cost_eur=ai_cost_eur+coalesce((p_patch->>'ai_cost_eur')::numeric,0),
  human_gate_reason=case when p_status='human_gate'then p_reason else human_gate_reason end,
  owner_department=case when to_owner in('CEO','CTO','CMO','WATCHDOG','CFO','DISTRIBUTION','OUTREACH','CONVERSION','SEO','OPPORTUNITY')then to_owner else owner_department end,
  resolved_at=case when p_status='recovered'then now()else resolved_at end,updated_at=now()
 where id=p_incident_id returning * into newrow;
 if old.escalation_level<>p_level then
  insert into company_escalations(incident_id,from_stage,to_stage,from_owner,to_owner,reason,automatic,evidence)
  values(p_incident_id,old.escalation_level,p_level,old.owner_department,to_owner,p_reason,true,p_patch);
 end if;
 event_name:=case p_status when'retrying'then'ORIGINAL_TASK_RETRY'when'repairing'then'PATCH_CREATED'
  when'verifying'then'TEST_PASS'when'recovered'then'INCIDENT_RECOVERED'
  when'escalated'then case when p_level='L4'then'CEO_ESCALATION'else'CTO_ESCALATION'end
  when'human_gate'then'OWNER_REQUIRED'else'INCIDENT_'||upper(p_status)end;
 perform company_record_event(newrow.run_id,coalesce(newrow.agent_id,'WATCHDOG'),
  coalesce(newrow.department,newrow.owner_department),newrow.job_id,event_name,p_status,p_reason,
  coalesce((p_patch->>'ai_cost_eur')::numeric,0),jsonb_build_object('incident_id',p_incident_id,'level',p_level)||p_patch);
 return newrow;
end $$;

drop function if exists public.company_recovery_retry(uuid,int);
create or replace function public.company_recovery_retry(
 p_incident_id uuid,p_backoff_seconds int default 30,p_task_id uuid default null)
returns boolean language plpgsql security invoker set search_path=public as $$
declare i company_incidents%rowtype;j agent_jobs%rowtype;next_level text;
begin
 select * into strict i from company_incidents where id=p_incident_id for update;
 if i.status in('resolved','recovered','human_gate','failed_final') or coalesce(p_task_id,i.job_id) is null then return false;end if;
 select * into strict j from agent_jobs where id=coalesce(p_task_id,i.job_id) for update;
 if p_task_id is not null and not exists(
  select 1 from company_incident_tasks where incident_id=i.id and task_id=p_task_id
 ) and i.job_id<>p_task_id then raise exception 'task is not linked to incident';end if;
 if j.status='completed' then return false;end if;
 if j.attempt>=j.max_attempts then
  perform company_incident_transition(i.id,'escalated','L4','Blind retry limit reached; CEO must select a new execution path',
   '{"prevention_rule":"circuit_break_after_retry_limit"}');
  perform company_record_event(j.run_id,'WATCHDOG',coalesce(j.department,'WATCHDOG'),j.id,
   'FAILURE_LOOP_STOPPED','escalated','Retry limit stopped blind loop',0,jsonb_build_object('incident_id',i.id));
  return false;
 end if;
 next_level:=case least(j.attempt::int,3) when 0 then'L0'when 1 then'L1'when 2 then'L2'else'L3'end;
 update agent_jobs set status='queued',attempt=attempt+1,available_at=now()+make_interval(secs=>greatest(0,least(p_backoff_seconds,1800))),
  locked_at=null,locked_by=null,assigned_agent_id=null,verification_status='pending',updated_at=now(),
  input=coalesce(input,'{}')||jsonb_build_object('recovery_incident_id',i.id,'recovery_attempt',attempt+1)
 where id=j.id;
 perform company_incident_transition(i.id,'retrying',next_level,'Bounded retry of original task queued',
  jsonb_build_object('verification',jsonb_build_object('original_task_id',j.id)));
 return true;
end $$;

create or replace function public.company_growth_fail(p_task_id uuid,p_error text)
returns void language plpgsql security invoker set search_path=public as $$
declare j agent_jobs%rowtype;i uuid;failure_code text;component text;backoff_seconds int;known boolean;human boolean;
begin
 select * into strict j from agent_jobs where id=p_task_id and assigned_agent_id='GROWTH_EXECUTOR' and status='running' for update;
 human:=lower(coalesce(p_error,''))~'(captcha|2fa|human verification|payment approval|legal approval|account restriction|checkpoint)';
 known:=lower(coalesce(p_error,''))~'(deployment_not_yet_verified|timed out|timeout|429|rate limit|502|503|504|service unavailable|browser|session|token expired|401 unauthorized|injected_worker_failure)';
 failure_code:=case
  when human then'HUMAN_GATE'
  when lower(p_error)like'%injected_worker_failure%'then'WORKER_FAILED'
  when lower(p_error)like'%deployment_not_yet_verified%'then'DEPLOYMENT_PENDING'
  when known then'KNOWN_EXECUTION_FAILURE'
  else'UNKNOWN_EXECUTION_FAILURE'end;
 component:=case when lower(p_error)like'%deployment%'then'publishing'
  when lower(p_error)~'(browser|session)'then'browser'
  when lower(p_error)~'(429|rate limit|502|503|504|service unavailable)'then'api_provider'
  else'growth_execution'end;
 update agent_jobs set status='failed',error=left(p_error,2000),locked_at=null,locked_by=null,updated_at=now()where id=j.id;
 i:=company_incident_open(j.id,component,'execution',failure_code,
  case when human then'critical'else'high'end,left(p_error,2000),
  'GROWTH:'||md5(j.task_type||':'||regexp_replace(lower(coalesce(p_error,'')),'[0-9a-f-]{8,}|[0-9]+','#','g')),
  case when known or human then'deterministic'else'cheap'end);
 if human then
  perform company_incident_transition(i,'human_gate','L5','A real human-only security, legal, or payment gate was detected','{}');
 elsif j.attempt>=j.max_attempts then
  perform company_incident_transition(i,'escalated','L4','Retry limit reached; CEO must select a different execution path',
   '{"prevention_rule":"circuit_break_after_retry_limit"}');
 else
  backoff_seconds:=case when coalesce((j.input->>'is_test')::boolean,false)
   and j.input->>'failure_injection'='worker_failure_once'then 0
   else least(1800,30*(2^least(j.attempt::int,6)))::int end;
  perform company_recovery_retry(i,backoff_seconds,j.id);
 end if;
 perform company_record_event(j.run_id,'GROWTH_EXECUTOR',j.department,j.id,
  case when failure_code='WORKER_FAILED'then'WORKER_FAILED'else'growth.failed'end,'failed',left(p_error,500),0,
  jsonb_build_object('incident_id',i,'failure_code',failure_code,'known_runbook',known,'original_attempt',j.attempt));
end $$;

create or replace function public.company_recovery_confirm(p_task_id uuid,p_verification jsonb)
returns uuid language plpgsql security invoker set search_path=public as $$
declare j agent_jobs%rowtype;i company_incidents%rowtype;recovered_id uuid;
begin
 select * into strict j from agent_jobs where id=p_task_id;
 if j.status<>'completed' or j.verification_status<>'passed' or coalesce((p_verification->>'passed')::boolean,false)=false then
  raise exception 'original task is not verified successful';
 end if;
 update agent_jobs set error=null,updated_at=now()where id=j.id;
 for i in select distinct ci.* from company_incidents ci
  left join company_incident_tasks cit on cit.incident_id=ci.id
  where ci.status not in('resolved','recovered','failed_final') and
   (ci.job_id=j.id or ci.root_job_id=coalesce(j.root_job_id,j.id) or cit.task_id=j.id)
 loop
  perform company_incident_transition(i.id,'verifying',i.escalation_level,'Original task completed; verification evaluated',
   jsonb_build_object('verification',p_verification));
  perform company_incident_transition(i.id,'recovered',i.escalation_level,'Original business task verified successful',
   jsonb_build_object('resolution','original_task_succeeded','verification',p_verification,
    'prevention_rule',coalesce(i.prevention_rule,'reuse_verified_recovery_before_ai')));
  insert into company_memory(scope,memory_key,value,confidence,source,evidence,run_id,department)
  values('recovery_runbook',i.fingerprint,jsonb_build_object(
   'problem',i.failure_code,'symptoms',i.diagnosis,'root_cause',coalesce(i.root_cause,'bounded_failure'),
   'fix',i.attempted_fixes,'verification',p_verification,'prevention',coalesce(i.prevention_rule,'reuse_verified_recovery_before_ai'),
   'components',jsonb_build_array(i.component),'model_tier_required',i.ai_tier_used,'cost_eur',i.ai_cost_eur
  ),1,'company_incidents',jsonb_build_object('incident_id',i.id,'task_id',j.id),j.run_id,coalesce(j.department,'CTO'))
  on conflict(scope,memory_key)do update set value=excluded.value,confidence=1,source=excluded.source,
   evidence=excluded.evidence,run_id=excluded.run_id,department=excluded.department,updated_at=now();
  recovered_id:=coalesce(recovered_id,i.id);
 end loop;
 return recovered_id;
end $$;

create or replace function public.company_watchdog_detect(
 p_now timestamptz default now(),p_stale_minutes int default 30,p_dead_minutes int default 15,
 p_queue_threshold int default 25,p_distribution_hours int default 24,p_failure_hours int default 24)
returns jsonb language plpgsql security definer set search_path=public as $$
declare c int:=0;q int;r company_runs%rowtype;j agent_jobs%rowtype;h company_worker_heartbeats%rowtype;i uuid;
begin
 select * into r from company_runs where status='running'order by started_at desc limit 1;
 for j in select * from agent_jobs where status='running'and coalesce(locked_at,updated_at)<p_now-make_interval(mins=>p_stale_minutes) loop
  i:=company_incident_open(j.id,'worker','timeout','STALE_TASK','high','Running task exceeded stale threshold','STALE_TASK:'||j.id,'deterministic');c:=c+1;
 end loop;
 for j in select * from agent_jobs where status='failed'and updated_at>=p_now-make_interval(hours=>p_failure_hours) loop
  i:=company_incident_open(j.id,coalesce(j.task_type,'worker'),'execution','FAILED_JOB','high',
   coalesce(nullif(j.error,''),'Job failed without error detail'),
   'FAILED_JOB:'||md5(coalesce(j.task_type,'')||':'||regexp_replace(lower(coalesce(j.error,'')),'[0-9a-f-]{8,}|[0-9]+','#','g')),
   'deterministic');c:=c+1;
 end loop;
 for h in select wh.* from company_worker_heartbeats wh join company_agents a on a.agent_id=wh.worker_id
  where a.enabled and a.worker_type='browser_worker'and wh.last_seen_at<p_now-make_interval(mins=>p_dead_minutes)
 loop
 i:=company_incident_open(null,'worker','heartbeat','DEAD_WORKER','high','Worker heartbeat is stale','DEAD_WORKER:'||h.worker_id,'deterministic');c:=c+1;
 end loop;
 for h in select wh.* from company_worker_heartbeats wh join company_agents a on a.agent_id=wh.worker_id
  where a.enabled and a.worker_type in('service','orchestrator')and wh.last_seen_at<p_now-make_interval(mins=>p_dead_minutes)
 loop
  i:=company_incident_open(null,case when h.worker_id='SCHEDULER'then'scheduler'else'control_plane'end,
   'heartbeat','MISSING_HEARTBEAT','high','Control-plane heartbeat is stale','MISSING_HEARTBEAT:'||h.worker_id,'deterministic');c:=c+1;
 end loop;
 select count(*)into q from agent_jobs where status='queued'and available_at<=p_now;
 if q>=p_queue_threshold then
  i:=company_incident_open(null,'queue','backlog','QUEUE_BACKLOG','high','Runnable queue exceeds configured threshold','QUEUE_BACKLOG','deterministic');c:=c+1;
 end if;
 if r.id is not null and r.started_at<=p_now-make_interval(hours=>p_distribution_hours)
  and not exists(select 1 from company_metric_events where run_id=r.id and metric_key in('qualified_traffic','clicks','impressions')
   and occurred_at>=p_now-make_interval(hours=>p_distribution_hours)) then
  i:=company_incident_open(null,'distribution','business_inactivity','MISSING_DISTRIBUTION','high',
   'No measured distribution in configured window','MISSING_DISTRIBUTION:'||r.id,'deterministic');c:=c+1;
 end if;
 perform company_record_event(r.id,'WATCHDOG','WATCHDOG',null,'watchdog.scan','completed','Watchdog scan completed',0,
  jsonb_build_object('detections_evaluated',c,'queue_depth',q));
 return jsonb_build_object('detections_evaluated',c,'queue_depth',q,'run_id',r.id,
  'detectors',jsonb_build_array('stale_tasks','dead_workers','failed_jobs','queue_backlog','missing_heartbeats','missing_distribution'));
end $$;

create or replace function public.company_incident_dedupe_open()
returns jsonb language plpgsql security invoker set search_path=public as $$
declare g record;primary_id uuid;duplicate_id uuid;linked int:=0;closed int:=0;
begin
 for g in select failure_code,coalesce(component,'legacy') component_key,
   md5(regexp_replace(lower(coalesce(diagnosis,'')),'[0-9a-f-]{8,}|[0-9]+','#','g')) diagnosis_key,
   array_agg(id order by opened_at,id) ids
  from company_incidents where status not in('resolved','recovered','failed_final')
  group by failure_code,coalesce(component,'legacy'),
   md5(regexp_replace(lower(coalesce(diagnosis,'')),'[0-9a-f-]{8,}|[0-9]+','#','g')) having count(*)>1
 loop
  primary_id:=g.ids[1];
  foreach duplicate_id in array g.ids[2:array_length(g.ids,1)] loop
   insert into company_incident_tasks(incident_id,task_id)
    select primary_id,task_id from company_incident_tasks where incident_id=duplicate_id on conflict do nothing;
   insert into company_incident_tasks(incident_id,task_id)
    select primary_id,job_id from company_incidents where id=duplicate_id and job_id is not null on conflict do nothing;
   update company_incidents set status='resolved',resolution='Deduplicated into primary incident '||primary_id,
    resolved_at=now(),updated_at=now()where id=duplicate_id;
   linked:=linked+1;closed:=closed+1;
  end loop;
 end loop;
 return jsonb_build_object('duplicates_linked',linked,'duplicates_closed',closed);
end $$;

create or replace function public.company_watchdog_resolve_recovered(p_now timestamptz default now(),p_dead_minutes int default 15)
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
 );
 get diagnostics n=row_count;return n;
end $$;

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

create or replace function public.company_business_watchdog(p_run_id uuid,p_now timestamptz default now())
returns jsonb language plpgsql security invoker set search_path=public as $$
declare r company_runs%rowtype;i uuid;created int:=0;code text;hours int;severity text;task_type text;
begin
 select * into strict r from company_runs where id=p_run_id;
 for code,hours,severity,task_type in values
  ('NO_QUALIFIED_TRAFFIC_24H',24,'high','growth_discover'),
  ('NO_VISIBILITY_CHECKS_48H',48,'high','growth_convert'),
  ('NO_TRIALS_7D',168,'critical','growth_convert'),
  ('NO_CUSTOMERS_14D',336,'critical','growth_convert')
 loop
  if r.started_at<=p_now-make_interval(hours=>hours)and not exists(
   select 1 from company_metric_events m where m.run_id=r.id and m.occurred_at>=r.started_at and
    m.metric_key=case code when'NO_QUALIFIED_TRAFFIC_24H'then'qualified_traffic'
     when'NO_VISIBILITY_CHECKS_48H'then'visibility_checks'when'NO_TRIALS_7D'then'trials'else'customers'end and m.value>0
  )then
   i:=company_incident_open(null,'business','missing_outcome',code,severity,
    'Expected business outcome missing at deterministic threshold','BUSINESS:'||code||':'||r.id,'deterministic');
   update company_incidents set run_id=r.id,department='CEO',owner_department='CEO',
    escalation_level=case when hours>=168 then'L4'else'L1'end where id=i;
   perform company_create_task(r.id,'CONVERSION',task_type,'Recover missing business outcome: '||code,
    case when hours>=168 then 990 else 850 end,jsonb_build_object('runtime_adapter','growth','business_incident_id',i,'failure_code',code),
    'business-recovery:'||r.id||':'||code);
   created:=created+1;
  end if;
 end loop;
 return jsonb_build_object('incidents',created,'run_id',p_run_id);
end $$;

create or replace function public.company_watchdog_scan(
 p_now timestamptz default now(),p_stale_minutes int default 30,p_dead_minutes int default 15,
 p_queue_threshold int default 25,p_distribution_hours int default 24,p_failure_hours int default 24)
returns jsonb language plpgsql security definer set search_path=public as $$
declare recovered int;detected jsonb;ai jsonb;business jsonb:='{}';r uuid;i company_incidents%rowtype;retried int:=0;
begin
 recovered:=company_watchdog_resolve_recovered(p_now,p_dead_minutes);
 detected:=company_watchdog_detect(p_now,p_stale_minutes,p_dead_minutes,p_queue_threshold,p_distribution_hours,p_failure_hours);
 select id into r from company_runs where status='running'order by started_at desc limit 1;
 if r is not null then business:=company_business_watchdog(r,p_now);end if;
 ai:=company_ai_watchdog(p_now);
 for i in select * from company_incidents where status='open'and failure_code='STALE_TASK'and job_id is not null loop
  if company_recovery_retry(i.id,30)then retried:=retried+1;end if;
 end loop;
 return detected||jsonb_build_object('incidents_resolved',recovered,'automatic_retries',retried,'ai_health',ai,'business_health',business);
end $$;

create or replace function public.company_resilience_selftest(p_run_id uuid)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare t uuid;i uuid;result jsonb:='{}';marker text:=gen_random_uuid()::text;business jsonb;
begin
 begin
  t:=company_create_task(p_run_id,'DISTRIBUTION','growth_distribute','Transaction recovery task',999,
   '{"runtime_adapter":"growth","is_test":true}','resilience-worker:'||marker);
  perform company_assign_task(t,'GROWTH_EXECUTOR');
  update agent_jobs set status='failed',error='Injected worker timeout',verification_status='failed'where id=t;
  i:=company_incident_open(t,'worker','timeout','WORKER_FAILED','high','Injected worker timeout','selftest:worker:'||marker,'deterministic');
  if not company_recovery_retry(i,0)then raise exception 'worker retry failed';end if;
  perform company_assign_task(t,'GROWTH_EXECUTOR');
  perform company_complete_task(t,'GROWTH_EXECUTOR','{"test":true}','{"passed":true,"evidence":"transaction recovery"}');
  perform company_recovery_confirm(t,'{"passed":true,"evidence":"transaction recovery"}');
  if(select status from company_incidents where id=i)<>'recovered'then raise exception'incident not recovered';end if;
  if not exists(select 1 from company_memory where scope='recovery_runbook'and memory_key='selftest:worker:'||marker)then raise exception'learning missing';end if;
  result:=result||'{"worker_failure":true,"recovery":true,"original_task_success":true,"incident_closed":true,"learning":true}'::jsonb;

  t:=company_create_task(p_run_id,'CTO','growth_analyze','Transaction stale task',999,
   '{"runtime_adapter":"growth","is_test":true}','resilience-stale:'||marker);
  perform company_assign_task(t,'GROWTH_EXECUTOR');
  update agent_jobs set updated_at=now()-interval '2 hours',locked_at=now()-interval '2 hours'where id=t;
  perform company_watchdog_detect(now(),30,999999,999999,999999,1);
  select id into i from company_incidents where job_id=t and failure_code='STALE_TASK'and status='open';
  if i is null or not company_recovery_retry(i,0)then raise exception'stale recovery failed';end if;
  perform company_assign_task(t,'GROWTH_EXECUTOR');
  perform company_complete_task(t,'GROWTH_EXECUTOR','{"test":true}','{"passed":true,"evidence":"stale transaction recovery"}');
  perform company_recovery_confirm(t,'{"passed":true,"evidence":"stale transaction recovery"}');
  result:=result||'{"stale_detected":true,"stale_requeued":true}'::jsonb;

  t:=company_create_task(p_run_id,'CTO','growth_analyze','Transaction loop task',999,
   '{"runtime_adapter":"growth","is_test":true}','resilience-loop:'||marker);
  perform company_assign_task(t,'GROWTH_EXECUTOR');
  update agent_jobs set status='failed',attempt=1,max_attempts=1,error='Repeated unknown failure'where id=t;
  i:=company_incident_open(t,'runtime','unknown','REPEATED_FAILURE','high','Repeated unknown failure','selftest:loop:'||marker,'standard');
  if company_recovery_retry(i,0)then raise exception'blind retry not stopped';end if;
  if(select status from company_incidents where id=i)<>'escalated'then raise exception'loop not escalated';end if;
  result:=result||'{"loop_protection":true,"l4_escalation":true}'::jsonb;

  business:=company_business_watchdog(p_run_id,greatest(now(),(select started_at+interval '15 days'from company_runs where id=p_run_id)));
  if not exists(select 1 from company_incidents where run_id=p_run_id and failure_code='NO_CUSTOMERS_14D')then raise exception'business watchdog failed';end if;
 result:=result||'{"business_watchdog":true,"rolled_back":true}'::jsonb;
 raise sqlstate 'ZR001'using message='rollback resilience fixtures';
 exception when sqlstate 'ZR001'then null;
 end;
 return result;
end $$;

create or replace view public.company_agent_performance with(security_invoker=true)as
with jobs as(
 select assigned_agent_id agent_id,count(*) tasks_attempted,
  count(*)filter(where status='completed'and verification_status='passed') successful,
  count(*)filter(where status='failed') failed,coalesce(sum(attempt),0) retries
 from agent_jobs where assigned_agent_id is not null group by assigned_agent_id
),incidents as(
 select agent_id,count(*) incidents,count(*)filter(where status='recovered') incidents_recovered
 from company_incidents where agent_id is not null group by agent_id
),costs as(
 select agent_id,coalesce(sum(amount_eur)filter(where category='llm'),0) ai_cost_eur,
  coalesce(sum(amount_eur),0) total_cost_eur from company_cost_events where agent_id is not null group by agent_id
),outcomes as(
 select department,coalesce(sum(value)filter(where metric_key in('visibility_checks','trials','customers','revenue')),0) business_outcomes
 from company_metric_events where coalesce((dimensions->>'is_test')::boolean,false)=false group by department
)
select a.agent_id,a.department,a.enabled,a.status,coalesce(j.tasks_attempted,0)tasks_attempted,
 coalesce(j.successful,0)successful,coalesce(j.failed,0)failed,coalesce(j.retries,0)retries,
 coalesce(i.incidents,0)incidents,coalesce(i.incidents_recovered,0)incidents_recovered,
 case when coalesce(i.incidents,0)>0 then i.incidents_recovered::numeric/i.incidents end recovery_rate,
 coalesce(c.ai_cost_eur,0)ai_cost_eur,coalesce(c.total_cost_eur,0)total_cost_eur,
 coalesce(o.business_outcomes,0)business_outcomes
from company_agents a left join jobs j using(agent_id)left join incidents i using(agent_id)
left join costs c using(agent_id)left join outcomes o on o.department=a.department;
revoke all on public.company_agent_performance from public,anon,authenticated;
grant select on public.company_agent_performance to service_role;

-- Correct any premature legacy distribution incident created before its 24-hour window.
update company_incidents i set status='resolved',resolution='Premature detector corrected in Run 4',resolved_at=now(),updated_at=now()
from company_runs r where i.fingerprint='MISSING_DISTRIBUTION:'||r.id and i.status not in('resolved','recovered','failed_final')
 and r.started_at>now()-interval '24 hours';

revoke all on function public.company_incident_open(uuid,text,text,text,text,text,text,text) from public,anon,authenticated;
revoke all on function public.company_incident_transition(uuid,text,text,text,jsonb) from public,anon,authenticated;
revoke all on function public.company_recovery_retry(uuid,int,uuid) from public,anon,authenticated;
revoke all on function public.company_recovery_confirm(uuid,jsonb) from public,anon,authenticated;
revoke all on function public.company_ai_watchdog(timestamptz) from public,anon,authenticated;
revoke all on function public.company_business_watchdog(uuid,timestamptz) from public,anon,authenticated;
revoke all on function public.company_resilience_selftest(uuid) from public,anon,authenticated;
revoke all on function public.company_incident_dedupe_open() from public,anon,authenticated;
grant execute on function public.company_incident_open(uuid,text,text,text,text,text,text,text) to service_role;
grant execute on function public.company_incident_transition(uuid,text,text,text,jsonb) to service_role;
grant execute on function public.company_recovery_retry(uuid,int,uuid) to service_role;
grant execute on function public.company_recovery_confirm(uuid,jsonb) to service_role;
grant execute on function public.company_ai_watchdog(timestamptz) to service_role;
grant execute on function public.company_business_watchdog(uuid,timestamptz) to service_role;
grant execute on function public.company_resilience_selftest(uuid) to service_role;
grant execute on function public.company_incident_dedupe_open() to service_role;
revoke all on function public.company_watchdog_detect(timestamptz,int,int,int,int,int) from public,anon,authenticated;
revoke all on function public.company_watchdog_resolve_recovered(timestamptz,int) from public,anon,authenticated;
revoke all on function public.company_watchdog_scan(timestamptz,int,int,int,int,int) from public,anon,authenticated;
grant execute on function public.company_watchdog_detect(timestamptz,int,int,int,int,int) to service_role;
grant execute on function public.company_watchdog_resolve_recovered(timestamptz,int) to service_role;
grant execute on function public.company_watchdog_scan(timestamptz,int,int,int,int,int) to service_role;
