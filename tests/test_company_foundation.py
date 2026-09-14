from decimal import Decimal
from pathlib import Path
import unittest

from agent.company_foundation import (
    BudgetPolicy,
    FAILURE_PATH,
    FailureStage,
    TaskEnvelope,
    next_failure_action,
    requires_llm,
)
from agent.org_architecture import CANONICAL_COMPANY, canonical_role


class CompanyFoundationTests(unittest.TestCase):
    def test_canonical_org_and_legacy_adapters(self):
        self.assertEqual(len(CANONICAL_COMPANY), 10)
        self.assertEqual(canonical_role("SALES_HEAD"), "CMO")
        self.assertEqual(canonical_role("OPS_HEAD"), "CTO")
        self.assertEqual(CANONICAL_COMPANY["OUTREACH"]["parent"], "CMO")

    def test_failure_path_matches_closed_loop(self):
        self.assertEqual(FAILURE_PATH[0], FailureStage.TASK)
        self.assertEqual(FAILURE_PATH[-1], FailureStage.RESOLVED)
        decision = next_failure_action(FailureStage.FAIL)
        self.assertEqual(decision.stage, FailureStage.RETRY)
        self.assertFalse(decision.human_required)
        human = next_failure_action(FailureStage.FAIL, failure_code="CAPTCHA")
        self.assertEqual(human.stage, FailureStage.HUMAN_GATE)
        self.assertTrue(human.human_required)
        self.assertEqual(human.owner, "OWNER")

    def test_task_envelope_is_deduplicated_and_canonical(self):
        a = TaskEnvelope.build(department="SALES_HEAD", task_type="Lead Research", business_key="acme", payload={"city": "Bonn"})
        b = TaskEnvelope.build(department="CMO", task_type="Lead Research", business_key="acme", payload={"city": "Bonn"})
        self.assertEqual(a.department, "CMO")
        self.assertEqual(a.idempotency_key, b.idempotency_key)
        self.assertEqual(a.correlation_id, b.correlation_id)

    def test_budget_hard_cap_reserve_and_strong_model_gate(self):
        policy = BudgetPolicy()
        self.assertTrue(policy.authorize(spent_usd="4.19", committed_usd="0", requested_usd="0.10", essential=False).allowed)
        self.assertEqual(policy.authorize(spent_usd="29.50", committed_usd="0", requested_usd="1", essential=True).reason, "MONTHLY_HARD_CAP")
        self.assertEqual(policy.authorize(spent_usd="27.10", committed_usd="0", requested_usd="0.10", essential=False).reason, "RESERVE_PROTECTED")
        self.assertEqual(policy.authorize(spent_usd="4", committed_usd="0", requested_usd="0.10", essential=True, model_tier="strong").reason, "STRONG_MODEL_NOT_JUSTIFIED")
        self.assertTrue(policy.authorize(spent_usd=Decimal("4"), committed_usd=0, requested_usd=Decimal("0.10"), essential=True, model_tier="strong", difficult_decision=True).allowed)

    def test_llm_is_exception_not_default(self):
        self.assertFalse(requires_llm(deterministic_available=True, ambiguous=True))
        self.assertFalse(requires_llm(deterministic_available=False, ambiguous=False))
        self.assertTrue(requires_llm(deterministic_available=False, ambiguous=True))

    def test_sql_extends_existing_queue_instead_of_creating_another(self):
        sql = Path("sql/migrations/20260914_autonomous_company_foundation.sql").read_text(encoding="utf-8").lower()
        self.assertIn("alter table public.agent_jobs", sql)
        self.assertNotIn("create table public.agent_jobs", sql)
        self.assertIn("company_mission_control", sql)


if __name__ == "__main__":
    unittest.main()
