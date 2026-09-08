create extension if not exists pgcrypto;

create table if not exists public.browser_jobs (
  id uuid primary key default gen_random_uuid(),
  task text not null check (char_length(task) between 1 and 20000),
  status text not null default 'pending' check (status in ('pending','running','completed','failed','cancelled')),
  result jsonb,
  error text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  started_at timestamptz,
  finished_at timestamptz
);

create index if not exists browser_jobs_status_created_idx
  on public.browser_jobs(status, created_at);

alter table public.browser_jobs enable row level security;

-- Intentionally no public RLS policy. The worker uses the server-side service role.
-- Add narrowly scoped authenticated-user policies only when a UI/API is introduced.

create or replace function public.claim_browser_job()
returns setof public.browser_jobs
language plpgsql
security definer
set search_path = public
as $$
declare
  claimed_id uuid;
begin
  select id into claimed_id
  from public.browser_jobs
  where status = 'pending'
  order by created_at
  for update skip locked
  limit 1;

  if claimed_id is null then
    return;
  end if;

  return query
  update public.browser_jobs
  set status = 'running', started_at = now()
  where id = claimed_id
  returning *;
end;
$$;

revoke all on function public.claim_browser_job() from public, anon, authenticated;
