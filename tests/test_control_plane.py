from decimal import Decimal
import unittest

from agent.control_plane import OUTCOME_ORDER, OUTCOME_PRIORITY, CEOOrchestrator, ControlPlaneClient, select_priority


class ControlPlanePolicyTests(unittest.TestCase):
    def test_outcome_order_is_business_order(self):
        self.assertEqual(OUTCOME_ORDER, (
            "revenue", "customers", "trials", "visibility_checks",
            "qualified_traffic", "clicks", "impressions",
        ))
        self.assertGreater(OUTCOME_PRIORITY["revenue"], OUTCOME_PRIORITY["impressions"])

    def test_ceo_chooses_revenue_gap_before_task_volume(self):
        decision = select_priority(
            {"revenue": "100", "customers": 10, "impressions": 10000},
            {"revenue": "0", "customers": 0, "impressions": 0, "tasks_completed": 999},
        )
        self.assertEqual(decision.outcome, "revenue")
        self.assertEqual(decision.department, "CONVERSION")
        self.assertEqual(decision.gap, Decimal("100"))

    def test_ceo_falls_through_to_distribution(self):
        decision = select_priority(
            {"revenue": 1, "customers": 1, "trials": 2, "visibility_checks": 5, "qualified_traffic": 20},
            {"revenue": 1, "customers": 1, "trials": 2, "visibility_checks": 5, "qualified_traffic": 3},
        )
        self.assertEqual(decision.outcome, "qualified_traffic")
        self.assertEqual(decision.department, "DISTRIBUTION")

    def test_delegate_uses_existing_queue_rpc(self):
        calls = []
        def transport(name, payload):
            calls.append((name, payload))
            return "task-1"
        result = CEOOrchestrator(ControlPlaneClient(transport=transport)).delegate_gap(
            "run-1", {"trials": 2}, {"trials": 0}
        )
        self.assertEqual(result["task_id"], "task-1")
        self.assertEqual(calls[0][0], "company_create_task")
        self.assertEqual(calls[0][1]["p_department"], "CONVERSION")

    def test_no_task_when_targets_met(self):
        calls = []
        def transport(name, payload):
            calls.append((name, payload))
            return 1
        result = CEOOrchestrator(ControlPlaneClient(transport=transport)).delegate_gap(
            "run-1", {"revenue": 1}, {"revenue": 1}
        )
        self.assertEqual(result["status"], "targets_met")
        self.assertEqual(calls[0][0], "company_record_event")


if __name__ == "__main__":
    unittest.main()
