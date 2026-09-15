-- All fixture mutations roll back inside the exception subtransaction.
create or replace function public.company_growth_selftest(p_run_id uuid)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare t uuid;e uuid;outcome text;observed numeric;sample numeric;result jsonb:='{}';marker text:=gen_random_uuid()::text;
begin
 begin
  t:=company_create_task(p_run_id,'SEO','growth_seo','Transaction-only growth acceptance',900,'{"runtime_adapter":"growth"}','test:'||marker);
  perform company_assign_task(t,'GROWTH_EXECUTOR');
  perform company_complete_task(t,'GROWTH_EXECUTOR','{"test":true}','{"passed":true,"evidence":"transaction-only lifecycle acceptance"}');
  perform company_ceo_receive_result(t);
  result:=result||'{"task_lifecycle":true}'::jsonb;
  foreach outcome in array array['SCALE','ITERATE','PAUSE','KILL'] loop
   e:=company_growth_experiment(p_run_id,'Transaction-only test','test','test','test','https://locenix.com/test/'||marker||outcome,'trials',2,now()+interval '1 day','{"min_sample":20}');
   observed:=case outcome when 'SCALE' then 2 when 'ITERATE' then 1 else 0 end;
   sample:=case outcome when 'PAUSE' then 2 else 25 end;
   update company_experiments set started_at=now()-interval '2 days',deadline=now()-interval '1 day' where id=e;
   perform company_growth_metric(p_run_id,'trials',observed,'transaction-test',marker||outcome||'trials','{}',now()-interval '36 hours',e);
   perform company_growth_metric(p_run_id,'qualified_traffic',sample,'transaction-test',marker||outcome||'traffic','{}',now()-interval '36 hours',e);
   perform company_growth_review(p_run_id);
   if (select decision from company_experiments where id=e)<>outcome then raise exception 'wrong experiment decision';end if;
   result:=result||jsonb_build_object(outcome,true);
  end loop;
  begin
   perform company_growth_metric(p_run_id,'trials',1,'test',marker,'{"is_test":true}');
   raise exception 'test metric unexpectedly accepted';
  exception when others then if SQLERRM<>'test events cannot enter business metrics' then raise;end if;end;
  result:=result||'{"test_metrics_rejected":true,"rolled_back":true}'::jsonb;
  raise sqlstate 'ZG001' using message='rollback successful test fixtures';
 exception when sqlstate 'ZG001' then null;
 end;
 return result;
end $$;
revoke all on function public.company_growth_selftest(uuid) from public,anon,authenticated;
grant execute on function public.company_growth_selftest(uuid) to service_role;
