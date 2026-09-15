import unittest

from agent.resilience import (
    classify_failure, fingerprint, plan_recovery, should_circuit_break,
)


class ResiliencePolicyTests(unittest.TestCase):
    def test_known_error_uses_runbook_without_llm(self):
        plan = plan_recovery("HTTP 503 service unavailable", attempt=0, max_attempts=3)
        self.assertEqual(plan.action, "circuit_breaker_then_fallback")
        self.assertEqual(plan.ai_tier, "deterministic")
        self.assertTrue(plan.retry)

    def test_unknown_failure_escalates_l0_through_l4(self):
        levels = [
            plan_recovery("novel opaque failure", attempt=n, max_attempts=4, business_impact="high").escalation_level
            for n in range(5)
        ]
        self.assertEqual(levels, ["L0", "L1", "L2", "L3", "L4"])
        self.assertFalse(plan_recovery("novel opaque failure", attempt=4, max_attempts=4).retry)

    def test_owner_only_for_real_gate(self):
        ordinary = plan_recovery("novel production error", attempt=10, max_attempts=3)
        gate = plan_recovery("Account requires 2FA", attempt=0, max_attempts=3)
        self.assertEqual(ordinary.escalation_level, "L4")
        self.assertNotEqual(ordinary.owner, "OWNER")
        self.assertEqual(gate.escalation_level, "L5")
        self.assertTrue(gate.human_gate)

    def test_incident_deduplication_ignores_volatile_ids(self):
        self.assertEqual(
            fingerprint("Unknown failure request abcdef1234567890 id 123"),
            fingerprint("Unknown failure request fedcba0987654321 id 999"),
        )

    def test_failure_loop_opens_circuit(self):
        self.assertTrue(should_circuit_break(3, 1))
        self.assertFalse(should_circuit_break(3, 2))

    def test_unknown_high_impact_uses_strong_only_after_escalation(self):
        self.assertEqual(
            plan_recovery("novel", attempt=0, max_attempts=5, business_impact="high").ai_tier,
            "cheap",
        )
        self.assertEqual(
            plan_recovery("novel", attempt=3, max_attempts=5, business_impact="high").ai_tier,
            "strong",
        )

    def test_classification_is_deterministic(self):
        self.assertEqual(classify_failure("Deployment not yet verified")[0], "DEPLOYMENT_PENDING")

    def test_budget_guard_is_a_runbook_not_an_owner_gate(self):
        plan = plan_recovery("Monthly LLM budget hard stop", attempt=0, max_attempts=3)
        self.assertEqual(plan.failure_code, "CFO_BUDGET_GUARD")
        self.assertEqual(plan.ai_tier, "deterministic")
        self.assertFalse(plan.human_gate)


if __name__ == "__main__":
    unittest.main()
