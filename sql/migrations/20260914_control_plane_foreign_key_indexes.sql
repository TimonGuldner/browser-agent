-- Run 2: indexes for every new control-plane foreign-key access path.
create index if not exists company_cost_events_department_idx on public.company_cost_events(department) where department is not null;
create index if not exists company_cost_events_reservation_idx on public.company_cost_events(reservation_id) where reservation_id is not null;
create index if not exists company_cost_events_run_idx on public.company_cost_events(run_id,occurred_at desc) where run_id is not null;
create index if not exists company_cost_reservations_department_idx on public.company_cost_reservations(department) where department is not null;
create index if not exists company_cost_reservations_job_idx on public.company_cost_reservations(job_id) where job_id is not null;
create index if not exists company_cost_reservations_run_idx on public.company_cost_reservations(run_id,status) where run_id is not null;
create index if not exists company_decisions_department_idx on public.company_decisions(department,created_at desc);
create index if not exists company_experiments_decision_idx on public.company_experiments(decision_id) where decision_id is not null;
create index if not exists company_experiments_run_idx on public.company_experiments(run_id,status) where run_id is not null;
create index if not exists company_memory_department_idx on public.company_memory(department) where department is not null;
create index if not exists company_memory_run_idx on public.company_memory(run_id) where run_id is not null;
create index if not exists company_metric_events_run_idx on public.company_metric_events(run_id,occurred_at desc) where run_id is not null;
