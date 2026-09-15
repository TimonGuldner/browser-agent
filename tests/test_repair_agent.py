from __future__ import annotations

import os
import sys
import types
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

os.environ.setdefault("SUPABASE_URL", "http://supabase.invalid")
os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "test-only")
if "supabase" not in sys.modules:
    supabase_stub = types.ModuleType("supabase")
    supabase_stub.Client = object
    supabase_stub.create_client = lambda *_args, **_kwargs: None
    sys.modules["supabase"] = supabase_stub

from agent.repair_agent import incident_is_at, is_hard_block  # noqa: E402


class RepairAgentGuardTests(unittest.TestCase):
    def test_budget_guard_is_a_deterministic_hard_block(self):
        self.assertTrue(is_hard_block({"error": "CFO_BUDGET_BLOCKED: PROJECTED_30D_BUDGET_EXCEEDED"}))

    def test_shared_incident_escalation_is_idempotent(self):
        db = MagicMock()
        execute = (
            db.table.return_value.select.return_value.eq.return_value
            .single.return_value.execute
        )
        execute.return_value = SimpleNamespace(data={"status": "escalated", "escalation_level": "L1"})
        self.assertTrue(incident_is_at(db, "incident", "escalated", "L1"))
        self.assertFalse(incident_is_at(db, "incident", "human_gate", "L5"))


if __name__ == "__main__":
    unittest.main()
