CREATE OR REPLACE FUNCTION public.company_growth_import(p_run_id uuid, p_events jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO 'public'
AS $function$
declare e jsonb;n int:=0;x uuid;
begin
 if jsonb_typeof(p_events)<>'array' or jsonb_array_length(p_events)>5000 then raise exception 'bounded event array required';end if;
 for e in select value from jsonb_array_elements(p_events) loop
 select id into x from company_experiments where run_id=p_run_id and metadata->>'campaign'=e->'dimensions'->>'campaign' limit 1;
 perform company_growth_metric(case when (e->>'occurred_at')::timestamptz >= (select started_at from company_runs where id=p_run_id) then p_run_id else null end,e->>'metric_key',(e->>'value')::numeric,'locenix_product',e->>'dedupe_key',e->'dimensions',(e->>'occurred_at')::timestamptz,x);n:=n+1;
 end loop;return jsonb_build_object('processed',n);
end $function$
;
revoke all on function company_growth_import(uuid,jsonb) from public,anon,authenticated;
grant execute on function company_growth_import(uuid,jsonb) to service_role;

CREATE OR REPLACE FUNCTION public.locenix_growth_transport_secret()
 RETURNS text
 LANGUAGE sql
 SECURITY DEFINER
 SET search_path TO 'public'
AS $function$ select decrypted_secret from vault.decrypted_secrets where name='locenix_growth_sync' $function$
;
revoke all on function locenix_growth_transport_secret() from public,anon,authenticated;
grant execute on function locenix_growth_transport_secret() to service_role;

