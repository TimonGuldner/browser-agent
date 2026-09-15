-- The live advisor showed that these service-role control views had reverted
-- to definer semantics.  Keep RLS/privilege evaluation on the invoking role.
alter view public.company_budget_status set (security_invoker=true);
alter view public.company_mission_control set (security_invoker=true);
