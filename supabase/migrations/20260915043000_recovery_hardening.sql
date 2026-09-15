-- Production follow-up: updated_at is maintained by a trigger, so stale locks
-- must be detected from locked_at. Keep the detector on the existing queue.
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
revoke all on function public.company_watchdog_detect(timestamptz,int,int,int,int,int) from public,anon,authenticated;
grant execute on function public.company_watchdog_detect(timestamptz,int,int,int,int,int) to service_role;
