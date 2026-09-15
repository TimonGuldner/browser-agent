-- Live verification found these Run-2 views had lost security_invoker.
alter view public.company_budget_status set(security_invoker=true);
alter view public.company_mission_control set(security_invoker=true);
