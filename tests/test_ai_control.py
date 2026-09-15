from __future__ import annotations

import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from agent.ai_control import (
    AIControl, AIRequest, TIER_0, TIER_1, TIER_2, TIER_3,
    route_request, validate_output,
)
from agent import llm_router
from agent.cost_control import BudgetBlocked


SCHEMA = {
    "decision": ("use", "pause"), "confidence": (int, float),
    "reason_summary": str, "recommended_action": str, "requires_escalation": bool,
}
VALID = {
    "decision": "use", "confidence": 0.91, "reason_summary": "Validated facts",
    "recommended_action": "continue", "requires_escalation": False,
}


class AIControlTests(unittest.TestCase):
    def test_no_llm_routing(self):
        request = AIRequest(
            task_type="heartbeat", purpose="worker health",
            deterministic_result=VALID, confidence_required=0.9,
        )
        with patch("agent.ai_control.cost_control.ai_event") as event:
            result, meta = AIControl(lambda *_: self.fail("provider called")).execute("unused", request, SCHEMA)
        self.assertEqual(result["decision"], "use")
        self.assertEqual(meta["model_tier"], TIER_0)
        self.assertFalse(meta["llm_called"])
        self.assertEqual(event.call_args.args[0], "AI_ROUTING_TIER_0")

    def test_cheap_first_and_standard_escalation(self):
        tiers = []
        def provider(_prompt, _task, tier, _max, _essential):
            tiers.append(tier)
            if tier == TIER_1:
                return json.dumps({**VALID, "confidence": 0.2}), {"provider_attempts": []}
            return json.dumps(VALID), {"provider": "configured", "model": "standard", "provider_attempts": []}
        with patch("agent.ai_control.cost_control.ai_event"), patch("agent.ai_control.cost_control.ai_cache_get", return_value=None):
            _, meta = AIControl(provider).execute(
                "classify", AIRequest(task_type="classification", purpose="intent", confidence_required=0.8), SCHEMA,
            )
        self.assertEqual(tiers, [TIER_1, TIER_2])
        self.assertEqual(meta["model_tier"], TIER_2)

    def test_strong_requires_impact_and_failed_standard(self):
        tiers = []
        def provider(_prompt, _task, tier, _max, _essential):
            tiers.append(tier)
            if tier == TIER_2:
                return "{}", {}
            return json.dumps(VALID), {"provider": "configured", "model": "strong", "provider_attempts": []}
        request = AIRequest(
            task_type="unknown_production_error", purpose="restore revenue workflow",
            complexity=0.95, risk="critical", expected_value_eur=Decimal("100"),
            estimated_cost_eur=Decimal("0.20"), confidence_required=0.9,
        )
        with patch("agent.ai_control.cost_control.ai_event"):
            _, meta = AIControl(provider).execute("diagnose", request, SCHEMA)
        self.assertEqual(tiers, [TIER_2, TIER_3])
        self.assertEqual(meta["model_tier"], TIER_3)

    def test_routine_never_routes_strong(self):
        route = route_request(AIRequest(
            task_type="kpi_calculation", purpose="sum metrics", complexity=1,
            risk="critical", expected_value_eur=Decimal("1000"), deterministic_result=VALID,
        ))
        self.assertEqual(route.tier, TIER_0)
        self.assertNotIn(TIER_3, route.allowed_tiers)

    def test_budget_and_value_guard(self):
        route = route_request(AIRequest(
            task_type="major_strategy_pivot", purpose="channel choice", complexity=0.95,
            risk="high", expected_value_eur=Decimal("0.10"), estimated_cost_eur=Decimal("0.20"),
        ))
        self.assertNotIn(TIER_3, route.allowed_tiers)

    def test_runtime_budget_block_does_not_escalate_tier(self):
        tiers = []
        def provider(_prompt, _task, tier, _max, _essential):
            tiers.append(tier)
            raise BudgetBlocked("projected spend guard")
        request = AIRequest(
            task_type="unknown_production_error", purpose="repair", complexity=0.95,
            risk="high", expected_value_eur=Decimal("100"), confidence_required=0.9,
        )
        with patch("agent.ai_control.cost_control.ai_event") as event:
            with self.assertRaises(BudgetBlocked):
                AIControl(provider).execute("diagnose", request, SCHEMA)
        self.assertEqual(tiers, [TIER_2])
        self.assertIn("AI_BUDGET_BLOCKED", [call.args[0] for call in event.call_args_list])

    def test_provider_router_does_not_fail_over_after_cfo_denial(self):
        slots = [
            llm_router.ProviderSlot("google", "GOOGLE_API_KEY", "google_1", "cheap-a", TIER_1),
            llm_router.ProviderSlot("openai", "OPENAI_API_KEY", "openai_1", "cheap-b", TIER_1),
        ]
        with patch.object(llm_router, "configured_slots", return_value=slots), \
             patch("agent.llm_router.cost_control.authorize_and_book_estimate", side_effect=BudgetBlocked("denied")) as authorize:
            with self.assertRaises(BudgetBlocked):
                llm_router.text_complete("classify", model_tier=TIER_1)
        self.assertEqual(authorize.call_count, 1)

    def test_output_validation_rejects_private_reasoning(self):
        errors = validate_output({**VALID, "chain_of_thought": "secret"}, SCHEMA, 0.8)
        self.assertIn("forbidden:chain_of_thought", errors)

    def test_cache_hit_avoids_provider(self):
        request = AIRequest(task_type="classification", purpose="stable", cache_ttl_seconds=3600)
        with patch("agent.ai_control.cost_control.ai_cache_get", return_value=VALID), \
             patch("agent.ai_control.cost_control.ai_event") as event:
            _, meta = AIControl(lambda *_: self.fail("provider called")).execute("same", request, SCHEMA)
        self.assertTrue(meta["cache_hit"])
        self.assertEqual(event.call_args.args[0], "AI_CACHE_HIT")

    def test_batch_calls_provider_once(self):
        calls = []
        def provider(_prompt, _task, tier, _max, _essential):
            calls.append(tier)
            return json.dumps({
                "items": [{"decision": "use"}, {"decision": "pause"}],
                "confidence": 0.9,
            }), {"provider_attempts": []}
        with patch("agent.ai_control.cost_control.ai_event"):
            rows, meta = AIControl(provider).execute_batch(
                "classify batch", [{"id": 1}, {"id": 2}],
                AIRequest(task_type="classification", purpose="batch", confidence_required=0.8),
                {"decision": ("use", "pause")},
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(rows), 2)
        self.assertEqual(meta["batch_size"], 2)

    def test_provider_fallback_events(self):
        meta = {
            "provider": "fallback", "model": "allowed", "provider_failover_used": True,
            "provider_attempts": [
                {"provider": "primary", "status": "failed", "error_class": "cooldown"},
                {"provider": "fallback", "status": "completed"},
            ],
        }
        with patch("agent.ai_control.cost_control.ai_event") as event:
            AIControl(lambda *_: (json.dumps(VALID), meta)).execute(
                "classify", AIRequest(task_type="classification", purpose="fallback"), SCHEMA,
            )
        names = [call.args[0] for call in event.call_args_list]
        self.assertIn("AI_PROVIDER_FAILED", names)
        self.assertIn("AI_FALLBACK_USED", names)

    def test_model_names_are_configurable_by_tier(self):
        with patch.dict(os.environ, {
            "OPENAI_API_KEY": "configured", "OPENAI_STRONG_MODEL": "configured-strong",
            "LOCENIX_STRONG_PROVIDER_ORDER": "openai",
        }, clear=False):
            with patch.object(llm_router, "OPENAI_STRONG_MODEL", "configured-strong"):
                slots = llm_router.configured_slots(llm_router.TASK_HIGH_REASONING, TIER_3)
        self.assertTrue(slots)
        self.assertEqual(slots[0].model, "configured-strong")
        self.assertEqual(slots[0].model_tier, TIER_3)

    def test_standard_google_model_is_never_mislabeled_strong(self):
        with patch.dict(os.environ, {
            "GOOGLE_API_KEY": "configured", "LOCENIX_STRONG_PROVIDER_ORDER": "google",
        }, clear=False), patch.object(llm_router, "GOOGLE_STRONG_MODEL", ""):
            slots = llm_router.configured_slots(llm_router.TASK_HIGH_REASONING, TIER_3)
        self.assertEqual(slots, [])


if __name__ == "__main__":
    unittest.main()
