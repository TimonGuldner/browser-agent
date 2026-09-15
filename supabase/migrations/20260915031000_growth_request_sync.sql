create or replace function public.company_growth_request_sync()
returns jsonb language plpgsql security definer set search_path=public as $$
declare request_id bigint;token text;last_sync timestamptz;
begin
 perform pg_advisory_xact_lock(hashtext('growth-sync'));
 select updated_at into last_sync from company_memory where scope='growth_transport' and memory_key='last_request';
 if last_sync>now()-interval '30 minutes' then return jsonb_build_object('status','cached');end if;
 select decrypted_secret into token from vault.decrypted_secrets where name='locenix_growth_sync';
 if token is null then raise exception 'scoped growth transport secret missing';end if;
 select net.http_post(url:='https://tpnjaoqocbshqykcliuv.supabase.co/functions/v1/growth-export',
 body:='{}'::jsonb,headers:=jsonb_build_object('Content-Type','application/json','x-growth-token',token),timeout_milliseconds:=15000)into request_id;
 perform company_remember('growth_transport','last_request',jsonb_build_object('request_id',request_id),'pg_net',null,'ANALYTICS');
 return jsonb_build_object('status','requested','request_id',request_id);
end $$;
revoke all on function public.company_growth_request_sync() from public,anon,authenticated;
grant execute on function public.company_growth_request_sync() to service_role;
