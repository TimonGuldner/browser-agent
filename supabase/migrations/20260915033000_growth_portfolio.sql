create or replace function public.company_growth_portfolio(p_run_id uuid)
returns jsonb language plpgsql security invoker set search_path=public as $$
declare best record;last_channel text;did uuid;candidate_count int;
begin
 with scores as (
 select dimensions->>'channel' channel,
 coalesce(sum(value)filter(where metric_key='revenue'),0) revenue,
 coalesce(sum(value)filter(where metric_key='customers'),0) customers,
 coalesce(sum(value)filter(where metric_key='trials'),0) trials,
 coalesce(sum(value)filter(where metric_key='visibility_checks'),0) checks,
 coalesce(sum(value)filter(where metric_key='qualified_traffic'),0) sample
 from company_metric_events where run_id=p_run_id and coalesce(dimensions->>'channel','unattributed')<>'unattributed'
 and coalesce((dimensions->>'is_test')::boolean,false)=false group by dimensions->>'channel'
 ) select *,count(*)over()candidates into best from scores where sample>=20 order by revenue desc,customers desc,trials desc,checks desc,channel limit 1;
 if not found or best.candidates<2 or best.revenue+best.customers+best.trials+best.checks=0 then return '{"status":"insufficient_downstream_evidence"}'::jsonb;end if;
 select value->>'channel' into last_channel from company_memory where scope='growth_portfolio' and memory_key=p_run_id::text;
 if last_channel=best.channel then return jsonb_build_object('status','unchanged','channel',best.channel);end if;
 insert into company_decisions(run_id,agent_id,department,decision_type,rationale,decision,evidence)
 values(p_run_id,'CEO','CEO','channel_reallocation','Compare revenue, customers, trials and checks in that order after minimum sample',
 jsonb_build_object('scale_channel',best.channel,'winner_weight',2,'other_weight',0.25),to_jsonb(best))returning id into did;
 update company_experiments set allocation_weight=case when channel=best.channel then 2 else 0.25 end,updated_at=now()
 where run_id=p_run_id and status in ('running','won');
 perform company_remember('growth_portfolio',p_run_id::text,jsonb_build_object('channel',best.channel,'decision_id',did),'company_metric_events',p_run_id,'CEO');
 perform company_create_task(p_run_id,'CONVERSION','growth_convert','Apply channel resource reallocation',850,
 jsonb_build_object('runtime_adapter','growth','decision_id',did,'scale_channel',best.channel),'portfolio:'||did);
 perform company_record_event(p_run_id,'CEO','CEO',null,'ceo.channel_reallocation','completed','Downstream outcome comparison changed channel allocation',0,jsonb_build_object('decision_id',did,'channel',best.channel));
 return jsonb_build_object('status','reallocated','channel',best.channel,'decision_id',did);
end $$;
revoke all on function public.company_growth_portfolio(uuid) from public,anon,authenticated;
grant execute on function public.company_growth_portfolio(uuid) to service_role;
