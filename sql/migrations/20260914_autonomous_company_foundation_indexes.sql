-- Cover foreign keys introduced by the Run 1 company foundation.
create index if not exists agent_jobs_root_idx
  on public.agent_jobs(root_job_id) where root_job_id is not null;
create index if not exists company_departments_parent_idx
  on public.company_departments(parent_key) where parent_key is not null;
create index if not exists company_incidents_job_idx
  on public.company_incidents(job_id) where job_id is not null;
create index if not exists company_incidents_root_job_idx
  on public.company_incidents(root_job_id) where root_job_id is not null;
create index if not exists company_incidents_owner_idx
  on public.company_incidents(owner_department,status);
create index if not exists company_experiments_department_idx
  on public.company_experiments(department,status);
create index if not exists company_metric_events_department_idx
  on public.company_metric_events(department,occurred_at desc);
create index if not exists company_metric_events_job_idx
  on public.company_metric_events(job_id) where job_id is not null;
create index if not exists company_metric_events_experiment_idx
  on public.company_metric_events(experiment_id) where experiment_id is not null;
create index if not exists company_cost_events_job_idx
  on public.company_cost_events(job_id) where job_id is not null;
create index if not exists company_cost_events_experiment_idx
  on public.company_cost_events(experiment_id) where experiment_id is not null;
create index if not exists company_worker_heartbeats_department_idx
  on public.company_worker_heartbeats(department,last_seen_at desc);
create index if not exists company_worker_heartbeats_job_idx
  on public.company_worker_heartbeats(current_job_id) where current_job_id is not null;
