-- A recovered task must not retain its prior error, and every linked incident
-- must close only after the original task is verified.
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
revoke all on function public.company_recovery_confirm(uuid,jsonb) from public,anon,authenticated;
grant execute on function public.company_recovery_confirm(uuid,jsonb) to service_role;
