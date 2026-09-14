-- Run 2: canonical definitions exported from the applied Supabase control plane.\n\nCREATE OR REPLACE FUNCTION public.claim_agent_job(p_worker text)
 RETURNS SETOF agent_jobs
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare j public.agent_jobs%rowtype;d text;
begin
 select department into d from public.company_agents where agent_id=p_worker and enabled;
 select * into j from public.agent_jobs where status='queued'and available_at<=now()
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
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_assign_task(p_task_id uuid, p_agent_id text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare j public.agent_jobs%rowtype;a public.company_agents%rowtype;
begin
 select * into a from public.company_agents where agent_id=p_agent_id and enabled and status<>'disabled' for update;
 if not found then raise exception 'enabled agent required';end if;
 update public.agent_jobs set assigned_agent_id=p_agent_id,status='running',locked_by=p_agent_id,locked_at=now(),updated_at=now()
 where id=p_task_id and status='queued' and(department=a.department or a.department in('CEO','CTO','WATCHDOG'))returning * into j;
 if not found then raise exception 'queued compatible task required';end if;
 update public.company_agents set status='busy',updated_at=now()where agent_id=p_agent_id;
 perform public.company_record_event(j.run_id,p_agent_id,a.department,p_task_id,'task.assigned','running','Task assigned',0,'{}');
 return jsonb_build_object('task_id',p_task_id,'agent_id',p_agent_id,'status','running');
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_authorize_spend(p_amount_eur numeric, p_category text, p_provider text, p_service text, p_reason text, p_run_id uuid DEFAULT NULL::uuid, p_job_id uuid DEFAULT NULL::uuid, p_agent_id text DEFAULT NULL::text, p_department text DEFAULT NULL::text, p_model_tier text DEFAULT 'small'::text, p_difficult_decision boolean DEFAULT false, p_metadata jsonb DEFAULT '{}'::jsonb, p_essential boolean DEFAULT false)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare b record;id uuid;
begin
 if p_amount_eur<0 then raise exception 'amount must be non-negative';end if;
 if p_model_tier='strong'and not p_difficult_decision then return jsonb_build_object('allowed',false,'reason','STRONG_MODEL_NOT_JUSTIFIED');end if;
 perform pg_advisory_xact_lock(hashtext('locenix-company-budget-30d'));
 update public.company_cost_reservations set status='expired',updated_at=now()where status='reserved'and expires_at<=now();
 select * into b from public.company_budget_status;
 if b.spent_eur+b.committed_eur+p_amount_eur>b.limit_eur then
  perform public.company_record_event(p_run_id,coalesce(p_agent_id,'CFO_GUARD'),'CFO',p_job_id,'cost.blocked','blocked',
  'CFO absolute hard cap blocked spend',0,jsonb_build_object('requested_eur',p_amount_eur,'remaining_eur',b.remaining_eur));
  return jsonb_build_object('allowed',false,'reason','ROLLING_30_DAY_HARD_CAP','remaining_eur',b.remaining_eur);
 end if;
 if not p_essential and b.projected_spend_eur>b.limit_eur then
  perform public.company_record_event(p_run_id,coalesce(p_agent_id,'CFO_GUARD'),'CFO',p_job_id,'cost.blocked','blocked',
  'CFO burn-rate guard blocked non-essential spend',0,jsonb_build_object('requested_eur',p_amount_eur,'projected_spend_eur',b.projected_spend_eur));
  return jsonb_build_object('allowed',false,'reason','PROJECTED_30D_BUDGET_EXCEEDED','remaining_eur',b.remaining_eur,
  'projected_spend_eur',b.projected_spend_eur);
 end if;
 insert into public.company_cost_reservations(run_id,job_id,agent_id,department,category,provider,service,amount_eur,model_tier,difficult_decision,reason,metadata)
 values(p_run_id,p_job_id,p_agent_id,p_department,p_category,p_provider,p_service,p_amount_eur,p_model_tier,p_difficult_decision,p_reason,coalesce(p_metadata,'{}'))
 returning company_cost_reservations.id into id;
 return jsonb_build_object('allowed',true,'reason','AUTHORIZED','reservation_id',id,'remaining_eur',b.remaining_eur-p_amount_eur);
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_cancel_task(p_task_id uuid, p_reason text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare j public.agent_jobs%rowtype;
begin
 update public.agent_jobs set status='cancelled',error=p_reason,updated_at=now()where id=p_task_id and status in('queued','running','waiting_approval')returning * into j;
 if found then
  update public.company_agents set status='idle',updated_at=now()where agent_id=j.assigned_agent_id;
  perform public.company_record_event(j.run_id,'WATCHDOG','WATCHDOG',j.id,'task.cancelled','cancelled',p_reason,0,'{}');
 end if;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_ceo_receive_result(p_task_id uuid)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare j public.agent_jobs%rowtype;v_id uuid;
begin
 select * into j from public.agent_jobs where id=p_task_id and status='completed' and verification_status='passed';
 if not found then raise exception 'verified completed task required';end if;
 insert into public.company_decisions(run_id,agent_id,department,decision_type,outcome_metric,rationale,decision,evidence)
 values(j.run_id,'CEO','CEO','task_result_received',j.input->>'outcome_metric',
 'Verified result received; next priority is based on business outcomes, not task count.',
 jsonb_build_object('task_id',p_task_id,'action','evaluate_outcome'),coalesce(j.result,'{}'))returning id into v_id;
 update public.company_runs set last_result_at=now(),updated_at=now()where id=j.run_id;
 perform public.company_record_event(j.run_id,'CEO','CEO',p_task_id,'ceo.result_received','completed','CEO received verified result',0,jsonb_build_object('decision_id',v_id));
 return v_id;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_ceo_strategy_review(p_run_id uuid)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare did uuid;b record;metrics jsonb;experiments jsonb;
begin
 if not exists(select 1 from public.company_runs where id=p_run_id and status='running')then raise exception 'active run required';end if;
 select coalesce(jsonb_object_agg(metric_key,total),'{}')into metrics from(
  select metric_key,sum(value)total from public.company_metric_events where run_id=p_run_id group by metric_key)x;
 select coalesce(jsonb_agg(jsonb_build_object('id',id,'status',status,'weight',allocation_weight)),'[]')into experiments
 from public.company_experiments where run_id=p_run_id;
 select * into b from public.company_budget_status;
 insert into public.company_decisions(run_id,agent_id,department,decision_type,rationale,decision,evidence)
 values(p_run_id,'CEO','CEO','strategy_review','Review measured business outcomes, experiment allocation and remaining budget.',
 jsonb_build_object('priority_order',jsonb_build_array('revenue','customers','trials','visibility_checks','qualified_traffic','clicks','impressions')),
 jsonb_build_object('metrics',metrics,'experiments',experiments,'budget_remaining_eur',b.remaining_eur,'projected_spend_eur',b.projected_spend_eur))
 returning id into did;
 perform public.company_record_event(p_run_id,'CEO','CEO',null,'ceo.strategy_review','completed','Strategy review recorded',0,jsonb_build_object('decision_id',did));
 return did;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_complete_task(p_task_id uuid, p_agent_id text, p_result jsonb, p_verification jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare j public.agent_jobs%rowtype;
begin
 if jsonb_typeof(p_result)<>'object' or jsonb_typeof(p_verification)<>'object'
 or coalesce((p_verification->>'passed')::boolean,false)<>true or coalesce(trim(p_verification->>'evidence'),'')='' then
 raise exception 'verified evidence required';end if;
 update public.agent_jobs set status='completed',result=p_result,verification_status='passed',completed_at=now(),updated_at=now()
 where id=p_task_id and status='running' and assigned_agent_id=p_agent_id returning * into j;
 if not found then raise exception 'assigned running task required';end if;
 update public.company_agents set status='idle',updated_at=now()where agent_id=p_agent_id;
 perform public.company_record_event(j.run_id,p_agent_id,j.department,p_task_id,'task.completed','completed','Task completed and verified',0,jsonb_build_object('verification',p_verification));
 return jsonb_build_object('task_id',p_task_id,'status','completed','verified',true);
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_create_run(p_mission text, p_targets jsonb DEFAULT '{}'::jsonb, p_metadata jsonb DEFAULT '{}'::jsonb)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare v_id uuid;
begin
 if coalesce(trim(p_mission),'')='' then raise exception 'mission required';end if;
 insert into public.company_runs(mission,targets,metadata)values(p_mission,coalesce(p_targets,'{}'),coalesce(p_metadata,'{}'))returning id into v_id;
 perform public.company_record_event(v_id,'CEO','CEO',null,'run.created','created','Run created',0,jsonb_build_object('mission',p_mission));
 return v_id;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_create_task(p_run_id uuid, p_department text, p_task_type text, p_task text, p_priority integer, p_input jsonb DEFAULT '{}'::jsonb, p_idempotency_key text DEFAULT NULL::text, p_available_at timestamp with time zone DEFAULT now(), p_parent_task_id uuid DEFAULT NULL::uuid)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare v_id uuid;v_key text:=nullif(trim(p_idempotency_key),'');
begin
 if not exists(select 1 from public.company_runs where id=p_run_id and status='running')then raise exception 'active run required';end if;
 if not exists(select 1 from public.company_departments where department_key=p_department and enabled)then raise exception 'enabled department required';end if;
 if coalesce(trim(p_task_type),'')='' or coalesce(trim(p_task),'')='' then raise exception 'task_type and task required';end if;
 if v_key is not null then select id into v_id from public.agent_jobs where idempotency_key=v_key;if found then return v_id;end if;end if;
 begin
 insert into public.agent_jobs(task,mode,priority,input,department,task_type,correlation_id,parent_job_id,idempotency_key,available_at,run_id)
 values(p_task,'autonomous',greatest(1,least(1000,p_priority)),coalesce(p_input,'{}'),p_department,lower(trim(p_task_type)),
 p_run_id::text,p_parent_task_id,v_key,p_available_at,p_run_id)returning id into v_id;
 exception when unique_violation then select id into v_id from public.agent_jobs where idempotency_key=v_key;end;
 update public.agent_jobs set root_job_id=coalesce((select coalesce(root_job_id,id)from public.agent_jobs where id=p_parent_task_id),v_id)
 where id=v_id and root_job_id is null;
 perform public.company_record_event(p_run_id,'CEO','CEO',v_id,'task.created','queued','CEO created task',0,jsonb_build_object('department',p_department,'priority',p_priority));
 return v_id;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_evaluate_experiment(p_experiment_id uuid, p_observed numeric, p_sample_size integer)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare e public.company_experiments%rowtype;s text;w numeric;did uuid;min_sample int;
begin
 select * into e from public.company_experiments where id=p_experiment_id and status='running'for update;
 if not found then raise exception 'running experiment required';end if;
 min_sample:=coalesce((e.metadata->>'min_sample')::int,20);
 if p_sample_size<min_sample then return jsonb_build_object('status','running','reason','MIN_SAMPLE_NOT_REACHED','min_sample',min_sample);end if;
 if p_observed>=e.success_threshold then s:='won';w:=2;else s:='lost';w:=0.25;end if;
 insert into public.company_decisions(run_id,agent_id,department,decision_type,outcome_metric,rationale,decision,evidence)
 values(e.run_id,'CEO','CEO',case when s='won'then'experiment_scale'else'experiment_reduce'end,e.primary_metric,
 'Deterministic threshold evaluation after minimum sample',
 jsonb_build_object('experiment_id',e.id,'status',s,'allocation_weight',w),
 jsonb_build_object('observed',p_observed,'threshold',e.success_threshold,'sample_size',p_sample_size))returning id into did;
 update public.company_experiments set status=s,allocation_weight=w,decision_id=did,
 result=jsonb_build_object('observed',p_observed,'sample_size',p_sample_size),ended_at=now(),updated_at=now()where id=e.id;
 perform public.company_record_event(e.run_id,'CEO','CEO',null,'experiment.'||s,s,'Experiment evaluated',0,jsonb_build_object('experiment_id',e.id,'decision_id',did));
 return jsonb_build_object('status',s,'allocation_weight',w,'decision_id',did);
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_finish_run(p_run_id uuid, p_status text DEFAULT 'completed'::text, p_summary jsonb DEFAULT '{}'::jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
begin
 if p_status not in('completed','failed','cancelled')then raise exception 'invalid terminal status';end if;
 if exists(select 1 from public.agent_jobs where run_id=p_run_id and status='running')then raise exception 'running tasks must finish first';end if;
 update public.company_runs set status=p_status,ended_at=now(),metadata=metadata||jsonb_build_object('summary',p_summary),updated_at=now()
 where id=p_run_id and status in('running','paused','created');
 if not found then raise exception 'run not finishable';end if;
 perform public.company_record_event(p_run_id,'CEO','CEO',null,'run.'||p_status,p_status,'Run finished',0,p_summary);
 return jsonb_build_object('run_id',p_run_id,'status',p_status);
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_heartbeat(p_agent_id text, p_status text DEFAULT 'running'::text, p_task_id uuid DEFAULT NULL::uuid, p_metadata jsonb DEFAULT '{}'::jsonb)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare d text;hs text;
begin
 select department into d from public.company_agents where agent_id=p_agent_id and enabled;
 if not found then raise exception 'registered enabled agent required';end if;
 hs:=case when p_status in('busy','active')then'running'when p_status in('idle','degraded','blocked','offline')then p_status else'running'end;
 insert into public.company_worker_heartbeats(worker_id,department,status,current_job_id,metadata,last_seen_at,updated_at)
 values(p_agent_id,d,hs,p_task_id,coalesce(p_metadata,'{}'),now(),now())
 on conflict(worker_id)do update set department=excluded.department,status=excluded.status,current_job_id=excluded.current_job_id,
 metadata=excluded.metadata,last_seen_at=now(),updated_at=now();
 update public.company_agents set status=case when hs='running'and p_task_id is not null then'busy'
 when hs='running'then'active'when hs='offline'then'dead'else hs end,updated_at=now()where agent_id=p_agent_id;
 perform public.company_record_event(null,p_agent_id,d,p_task_id,'worker.heartbeat',hs,'Worker heartbeat',0,p_metadata);
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_record_cost(p_reservation_id uuid, p_amount_eur numeric, p_dedupe_key text, p_units jsonb DEFAULT '{}'::jsonb)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare r public.company_cost_reservations%rowtype;id uuid;
begin
 perform pg_advisory_xact_lock(hashtext('locenix-company-budget-30d'));
 select * into r from public.company_cost_reservations where company_cost_reservations.id=p_reservation_id and status='reserved'for update;
 if not found then raise exception 'active reservation required';end if;
 if p_amount_eur<0 or p_amount_eur>r.amount_eur then raise exception 'actual cost exceeds reservation';end if;
 begin
 insert into public.company_cost_events(category,provider,service,amount_usd,currency,fx_rate_to_eur,job_id,units,dedupe_key,run_id,agent_id,department,reservation_id)
 values(r.category,r.provider,r.service,p_amount_eur,'EUR',1,r.job_id,coalesce(p_units,'{}'),nullif(trim(p_dedupe_key),''),
 r.run_id,r.agent_id,r.department,r.id)returning company_cost_events.id into id;
 exception when unique_violation then select company_cost_events.id into id from public.company_cost_events where dedupe_key=p_dedupe_key;end;
 update public.company_cost_reservations set status='consumed',updated_at=now()where company_cost_reservations.id=r.id;
 perform public.company_record_event(r.run_id,coalesce(r.agent_id,'CFO_GUARD'),'CFO',r.job_id,'cost.recorded','completed','Cost recorded',
 p_amount_eur,jsonb_build_object('cost_event_id',id,'reservation_id',r.id));
 return id;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_record_event(p_run_id uuid DEFAULT NULL::uuid, p_agent_id text DEFAULT NULL::text, p_department text DEFAULT NULL::text, p_task_id uuid DEFAULT NULL::uuid, p_event_type text DEFAULT NULL::text, p_status text DEFAULT NULL::text, p_message text DEFAULT NULL::text, p_cost_eur numeric DEFAULT 0, p_metadata jsonb DEFAULT '{}'::jsonb)
 RETURNS bigint
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare v_id bigint;v_run uuid:=p_run_id;v_dept text:=p_department;
begin
 if coalesce(trim(p_event_type),'')='' then raise exception 'event_type required';end if;
 if p_cost_eur<0 then raise exception 'cost must be non-negative';end if;
 if p_task_id is not null then select coalesce(v_run,j.run_id),coalesce(v_dept,j.department) into v_run,v_dept from public.agent_jobs j where j.id=p_task_id;end if;
 insert into public.agent_events(job_id,task_id,run_id,agent_id,department,event_type,status,message,cost_eur,metadata,data)
 values(p_task_id,p_task_id,v_run,p_agent_id,v_dept,p_event_type,p_status,p_message,p_cost_eur,coalesce(p_metadata,'{}'),coalesce(p_metadata,'{}'))
 returning id into v_id;return v_id;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_release_spend(p_reservation_id uuid, p_reason text)
 RETURNS void
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
begin update public.company_cost_reservations set status='released',reason=reason||'; release: '||p_reason,updated_at=now()
where id=p_reservation_id and status='reserved';end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_remember(p_scope text, p_key text, p_value jsonb, p_source text, p_run_id uuid DEFAULT NULL::uuid, p_department text DEFAULT NULL::text, p_confidence numeric DEFAULT 1, p_evidence jsonb DEFAULT '{}'::jsonb)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare id uuid;
begin
 insert into public.company_memory(scope,memory_key,value,confidence,source,evidence,run_id,department,updated_at)
 values(p_scope,p_key,p_value,p_confidence,p_source,coalesce(p_evidence,'{}'),p_run_id,p_department,now())
 on conflict(scope,memory_key)do update set value=excluded.value,confidence=excluded.confidence,source=excluded.source,
 evidence=excluded.evidence,run_id=excluded.run_id,department=excluded.department,updated_at=now()returning company_memory.id into id;
 return id;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_start_experiment(p_run_id uuid, p_name text, p_department text, p_hypothesis text, p_primary_metric text, p_success_threshold numeric, p_budget_cap_eur numeric DEFAULT 0, p_channel text DEFAULT NULL::text, p_metadata jsonb DEFAULT '{}'::jsonb)
 RETURNS uuid
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare id uuid;
begin
 if not exists(select 1 from public.company_runs where company_runs.id=p_run_id and status='running')then raise exception 'active run required';end if;
 insert into public.company_experiments(name,department,hypothesis,channel,status,budget_cap_usd,spent_usd,primary_metric,
 success_threshold,metadata,started_at,run_id)
 values(p_name,p_department,p_hypothesis,p_channel,'running',p_budget_cap_eur,0,p_primary_metric,p_success_threshold,
 coalesce(p_metadata,'{}')||jsonb_build_object('budget_currency','EUR'),now(),p_run_id)returning company_experiments.id into id;
 perform public.company_record_event(p_run_id,'CEO','CEO',null,'experiment.started','running','Experiment started',0,jsonb_build_object('experiment_id',id));
 return id;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_start_run(p_run_id uuid)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare v public.company_runs%rowtype;
begin
 update public.company_runs set status='running',started_at=coalesce(started_at,now()),updated_at=now()
 where id=p_run_id and status in('created','paused') returning * into v;
 if not found then raise exception 'run not startable';end if;
 update public.company_agents set status='active',updated_at=now()where agent_id='CEO';
 perform public.company_record_event(p_run_id,'CEO','CEO',null,'ceo.started','running','CEO started mission',0,jsonb_build_object('targets',v.targets));
 return jsonb_build_object('run_id',p_run_id,'status','running','targets',v.targets);
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_watchdog_detect(p_now timestamp with time zone DEFAULT now(), p_stale_minutes integer DEFAULT 30, p_dead_minutes integer DEFAULT 15, p_queue_threshold integer DEFAULT 25, p_distribution_hours integer DEFAULT 24, p_failure_hours integer DEFAULT 24)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare c int:=0;n int;r uuid;q int;
begin
 select id into r from public.company_runs where status='running'order by started_at desc limit 1;
 insert into public.company_incidents(job_id,root_job_id,fingerprint,failure_code,stage,severity,status,owner_department,diagnosis)
 select j.id,coalesce(j.root_job_id,j.id),'STALE_TASK:'||j.id,'STALE_TASK','FAIL','high','open','WATCHDOG','Running task exceeded stale threshold'
 from public.agent_jobs j where j.status='running'and j.updated_at<p_now-make_interval(mins=>p_stale_minutes)on conflict do nothing;
 get diagnostics n=row_count;c:=c+n;
 insert into public.company_incidents(job_id,root_job_id,fingerprint,failure_code,stage,severity,status,owner_department,diagnosis)
 select j.id,coalesce(j.root_job_id,j.id),'FAILED_JOB:'||j.id,'FAILED_JOB','FAIL','high','open','WATCHDOG',
 coalesce(nullif(j.error,''),'Job failed without error detail')from public.agent_jobs j
 where j.status='failed'and j.updated_at>=p_now-make_interval(hours=>p_failure_hours)on conflict do nothing;
 get diagnostics n=row_count;c:=c+n;
 insert into public.company_incidents(fingerprint,failure_code,stage,severity,status,owner_department,diagnosis)
 select 'MISSING_HEARTBEAT:'||a.agent_id,'MISSING_HEARTBEAT','FAIL','medium','open','WATCHDOG','Enabled worker has never emitted a heartbeat'
 from public.company_agents a left join public.company_worker_heartbeats h on h.worker_id=a.agent_id
 where a.enabled and a.worker_type in('worker','browser_worker','service')and h.worker_id is null on conflict do nothing;
 get diagnostics n=row_count;c:=c+n;
 insert into public.company_incidents(fingerprint,failure_code,stage,severity,status,owner_department,diagnosis)
 select 'DEAD_WORKER:'||h.worker_id,'DEAD_WORKER','FAIL','high','open','WATCHDOG','Worker heartbeat is stale'
 from public.company_worker_heartbeats h join public.company_agents a on a.agent_id=h.worker_id
 where a.enabled and h.last_seen_at<p_now-make_interval(mins=>p_dead_minutes)on conflict do nothing;
 get diagnostics n=row_count;c:=c+n;
 select count(*)into q from public.agent_jobs where status='queued'and available_at<=p_now;
 if q>=p_queue_threshold then
  insert into public.company_incidents(fingerprint,failure_code,stage,severity,status,owner_department,diagnosis)
  values('QUEUE_BACKLOG','QUEUE_BACKLOG','FAIL','high','open','WATCHDOG','Runnable queue exceeds configured threshold')on conflict do nothing;
  get diagnostics n=row_count;c:=c+n;
 end if;
 if r is not null and not exists(select 1 from public.company_metric_events m where m.run_id=r
 and m.metric_key in('qualified_traffic','clicks','impressions')and m.occurred_at>=p_now-make_interval(hours=>p_distribution_hours))then
  insert into public.company_incidents(fingerprint,failure_code,stage,severity,status,owner_department,diagnosis)
  values('MISSING_DISTRIBUTION:'||r,'MISSING_DISTRIBUTION','FAIL','high','open','DISTRIBUTION','No measured distribution in configured window')on conflict do nothing;
  get diagnostics n=row_count;c:=c+n;
 end if;
 perform public.company_record_event(r,'WATCHDOG','WATCHDOG',null,'watchdog.scan','completed','Watchdog scan completed',0,jsonb_build_object('incidents_created',c,'queue_depth',q));
 return jsonb_build_object('incidents_created',c,'queue_depth',q,'run_id',r,
 'detectors',jsonb_build_array('stale_tasks','dead_workers','failed_jobs','queue_backlog','missing_heartbeats','missing_distribution'));
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_watchdog_resolve_recovered(p_now timestamp with time zone DEFAULT now(), p_dead_minutes integer DEFAULT 15)
 RETURNS integer
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare n int;
begin
 update public.company_incidents i set status='resolved',resolved_at=p_now,updated_at=p_now
 where i.status<>'resolved'and(
 (i.failure_code='STALE_TASK'and exists(select 1 from public.agent_jobs j where j.id=i.job_id and j.status<>'running'))
 or(i.failure_code='DEAD_WORKER'and exists(select 1 from public.company_worker_heartbeats h
    where i.fingerprint='DEAD_WORKER:'||h.worker_id and h.last_seen_at>=p_now-make_interval(mins=>p_dead_minutes)))
 or(i.failure_code='MISSING_HEARTBEAT'and exists(select 1 from public.company_worker_heartbeats h
    where i.fingerprint='MISSING_HEARTBEAT:'||h.worker_id))
 or(i.failure_code='MISSING_DISTRIBUTION'and not exists(select 1 from public.company_runs r
    where i.fingerprint='MISSING_DISTRIBUTION:'||r.id and r.status='running'))
 );
 get diagnostics n=row_count;return n;
end$function$;\n\nCREATE OR REPLACE FUNCTION public.company_watchdog_scan(p_now timestamp with time zone DEFAULT now(), p_stale_minutes integer DEFAULT 30, p_dead_minutes integer DEFAULT 15, p_queue_threshold integer DEFAULT 25, p_distribution_hours integer DEFAULT 24, p_failure_hours integer DEFAULT 24)
 RETURNS jsonb
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare recovered int;detected jsonb;
begin
 recovered:=public.company_watchdog_resolve_recovered(p_now,p_dead_minutes);
 detected:=public.company_watchdog_detect(p_now,p_stale_minutes,p_dead_minutes,p_queue_threshold,p_distribution_hours,p_failure_hours);
 return detected||jsonb_build_object('incidents_resolved',recovered);
end$function$;\n\nCREATE OR REPLACE FUNCTION public.enqueue_due_agent_jobs(p_now timestamp with time zone DEFAULT now(), p_lookback_minutes integer DEFAULT 120)
 RETURNS TABLE(job_id uuid, role_key text, schedule_slot timestamp with time zone)
 LANGUAGE plpgsql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$
declare s public.agent_schedules%rowtype;t public.agent_templates%rowtype;slot timestamptz;lt timestamp;
jid uuid;first_slot timestamptz;rid uuid;dept text;
begin
 if p_lookback_minutes<0 or p_lookback_minutes>1440 then raise exception 'p_lookback_minutes must be between 0 and 1440';end if;
 select id into rid from public.company_runs where status='running'order by started_at desc limit 1;
 first_slot:=date_trunc('hour',p_now-make_interval(mins=>p_lookback_minutes));
 for s in select sch.* from public.agent_schedules sch where sch.enabled loop
  select * into t from public.agent_templates x where x.role_key=s.role_key and x.enabled;
  if not found then continue;end if;
  select department into dept from public.company_agents where agent_id=t.role_key;
  for slot in select gs from generate_series(first_slot,p_now,interval'15 minutes')gs where gs>=s.active_from loop
   lt:=slot at time zone s.timezone;
   if extract(minute from lt)::int<>s.run_minute then continue;end if;
   if s.run_hours is not null and not(extract(hour from lt)::smallint=any(s.run_hours))then continue;end if;
   if s.weekdays is not null and not(extract(isodow from lt)::smallint=any(s.weekdays))then continue;end if;
   jid:=null;
   insert into public.agent_jobs(task,mode,priority,max_steps,input,schedule_id,schedule_slot,run_id,department,task_type,correlation_id,idempotency_key,assigned_agent_id)
   values(concat_ws(E'\n\n',nullif(t.runtime_adapter,''),'--- MASTER PROMPT ---',t.prompt),t.default_mode,t.priority,t.max_steps,
   jsonb_build_object('requires_airtable',t.requires_airtable,'agent_role',t.role_key,'scheduled',true,'schedule_slot',slot,'timezone',s.timezone,'source','supabase-scheduler'),
   s.id,slot,rid,dept,'scheduled_agent',coalesce(rid::text,'legacy'),'schedule:'||s.id||':'||slot,t.role_key)
   on conflict do nothing returning id into jid;
   if jid is not null then
    perform public.company_record_event(rid,'SCHEDULER','CTO',jid,'scheduler.enqueued','queued','Scheduled task enqueued',0,jsonb_build_object('role_key',t.role_key,'slot',slot));
    job_id:=jid;role_key:=t.role_key;schedule_slot:=slot;return next;
   end if;
  end loop;
 end loop;
end$function$;\n\ndrop view if exists public.company_mission_control;\ndrop view if exists public.company_budget_status;\ncreate view public.company_budget_status as
WITH l AS (
         SELECT COALESCE(sum(company_cost_events.amount_eur) FILTER (WHERE (company_cost_events.occurred_at >= (now() - '30 days'::interval))), (0)::numeric) AS spent,
            COALESCE(sum(company_cost_events.amount_eur) FILTER (WHERE (company_cost_events.occurred_at >= date_trunc('day'::text, now()))), (0)::numeric) AS daily
           FROM company_cost_events
        ), r AS (
         SELECT COALESCE(sum(company_cost_reservations.amount_eur), (0)::numeric) AS committed
           FROM company_cost_reservations
          WHERE ((company_cost_reservations.status = 'reserved'::text) AND (company_cost_reservations.expires_at > now()))
        ), t AS (
         SELECT GREATEST(agent_monthly_llm_cost(now()), l.spent) AS spent_eur,
            l.daily,
            r.committed
           FROM l,
            r
        )
 SELECT 30.0000 AS limit_eur,
    spent_eur,
    daily AS daily_spend,
    GREATEST(spent_eur, (daily * (30)::numeric)) AS projected_spend_eur,
    committed AS committed_eur,
    GREATEST((0)::numeric, (((30)::numeric - spent_eur) - committed)) AS remaining_eur,
    ((spent_eur + committed) >= (30)::numeric) AS hard_stop,
    1.000000 AS usd_to_eur_guard_rate
   FROM t;

create view public.company_mission_control as
SELECT now() AS observed_at,
    ( SELECT (count(*))::integer AS count
           FROM company_runs
          WHERE (company_runs.status = 'running'::text)) AS active_runs,
    ( SELECT (count(*))::integer AS count
           FROM agent_jobs
          WHERE (agent_jobs.status = 'queued'::text)) AS queued_jobs,
    ( SELECT (count(*))::integer AS count
           FROM agent_jobs
          WHERE (agent_jobs.status = 'running'::text)) AS running_jobs,
    ( SELECT (count(*))::integer AS count
           FROM agent_jobs
          WHERE (agent_jobs.status = 'failed'::text)) AS failed_jobs,
    ( SELECT (count(*))::integer AS count
           FROM company_incidents
          WHERE (company_incidents.status <> 'resolved'::text)) AS open_incidents,
    ( SELECT company_budget_status.spent_eur
           FROM company_budget_status) AS spend_30d_eur,
    ( SELECT company_budget_status.daily_spend
           FROM company_budget_status) AS daily_spend_eur,
    ( SELECT company_budget_status.projected_spend_eur
           FROM company_budget_status) AS projected_spend_eur,
    ( SELECT company_budget_status.remaining_eur
           FROM company_budget_status) AS remaining_eur,
    ( SELECT company_budget_status.hard_stop
           FROM company_budget_status) AS budget_hard_stop,
    ( SELECT (count(*))::integer AS count
           FROM company_worker_heartbeats
          WHERE (company_worker_heartbeats.last_seen_at > (now() - '00:05:00'::interval))) AS live_workers;

grant select on public.company_budget_status,public.company_mission_control to service_role;
do $block$
declare f record;
begin
 for f in select p.oid::regprocedure sig from pg_proc p join pg_namespace n on n.oid=p.pronamespace
 where n.nspname='public' and (p.proname like 'company_%' or p.proname in ('claim_agent_job','enqueue_due_agent_jobs'))
 loop
  execute format('revoke all on function %s from public,anon,authenticated',f.sig);
  execute format('grant execute on function %s to service_role',f.sig);
 end loop;
end
$block$;
