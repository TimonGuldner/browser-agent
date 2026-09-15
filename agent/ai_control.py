from __future__ import annotations

"""Central cost-aware intelligence policy for LOCENIX."""

import hashlib
import json
from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Any, Callable, Mapping

from agent import cost_control, llm_router

TIER_0 = "deterministic"
TIER_1 = llm_router.TIER_CHEAP
TIER_2 = llm_router.TIER_STANDARD
TIER_3 = llm_router.TIER_STRONG

DETERMINISTIC_TASKS = {
    "heartbeat", "cron", "scheduler", "queue_processing", "retry_counter",
    "timeout_detection", "stale_task_detection", "budget_limit", "cost_addition",
    "event_logging", "status_update", "kpi_calculation", "known_api_call",
    "database_query", "known_runbook", "deduplication", "circuit_breaker",
}
STRONG_TASKS = {
    "major_strategy_pivot", "major_business_incident",
    "unknown_production_error", "architecture_decision",
}


@dataclass(frozen=True)
class AIRequest:
    task_type: str
    purpose: str
    complexity: float = 0.3
    risk: str = "low"
    expected_value_eur: Decimal = Decimal("0")
    confidence_required: float = 0.7
    previous_attempts: int = 0
    previous_model: str | None = None
    estimated_cost_eur: Decimal = Decimal("0.01")
    remaining_budget_eur: Decimal = Decimal("30")
    urgency: str = "normal"
    deterministic_result: Mapping[str, Any] | None = None
    cache_ttl_seconds: int = 0
    batch_size: int = 1
    allow_strong: bool = True

    def __post_init__(self) -> None:
        if not 0 <= self.complexity <= 1 or not 0 <= self.confidence_required <= 1:
            raise ValueError("complexity and confidence_required must be between 0 and 1")
        if self.risk not in {"low", "medium", "high", "critical"}:
            raise ValueError("invalid risk")
        if self.urgency not in {"low", "normal", "high", "critical"}:
            raise ValueError("invalid urgency")
        if self.batch_size < 1 or self.batch_size > 100:
            raise ValueError("batch_size must be 1..100")


@dataclass(frozen=True)
class RouteDecision:
    tier: str
    reason: str
    expected_value_to_cost: Decimal
    allowed_tiers: tuple[str, ...]


def route_request(request: AIRequest) -> RouteDecision:
    ratio = (
        request.expected_value_eur / request.estimated_cost_eur
        if request.estimated_cost_eur > 0 else Decimal("999999")
    )
    if request.deterministic_result is not None or request.task_type in DETERMINISTIC_TASKS:
        return RouteDecision(TIER_0, "deterministic_solution_available", ratio, (TIER_0,))
    if request.estimated_cost_eur > request.remaining_budget_eur:
        raise cost_control.BudgetBlocked("CFO_BUDGET_BLOCKED: request exceeds remaining budget")

    tier, reason = TIER_1, "cheap_first"
    if request.complexity >= 0.70 or request.confidence_required >= 0.90 or request.previous_attempts > 0:
        tier, reason = TIER_2, "complexity_or_confidence_requires_standard"
    strong_justified = (
        request.allow_strong
        and request.complexity >= 0.85
        and request.risk in {"high", "critical"}
        and request.task_type in STRONG_TASKS
        and ratio >= Decimal("10")
    )
    if strong_justified and request.previous_attempts >= 2:
        tier, reason = TIER_3, "repeated_high_impact_failure_justifies_strong"
    if request.remaining_budget_eur <= Decimal("3") and request.risk != "critical":
        tier, reason = TIER_1, "protected_reserve_forces_cheap"

    ordered = (TIER_1, TIER_2, TIER_3)
    chain = ordered if strong_justified else ordered[:2]
    start = ordered.index(tier)
    return RouteDecision(tier, reason, ratio, tuple(x for x in chain if ordered.index(x) >= start))


def _clean_json(text: str) -> Mapping[str, Any]:
    value = text.strip()
    fence = chr(96) * 3
    if value.startswith(fence):
        value = value.strip(chr(96))
        if value.startswith("json"):
            value = value[4:].lstrip()
    parsed = json.loads(value)
    if not isinstance(parsed, dict):
        raise ValueError("AI output must be one JSON object")
    return parsed


def validate_output(value: Mapping[str, Any], schema: Mapping[str, Any], confidence_required: float) -> list[str]:
    errors: list[str] = []
    for field, requirement in schema.items():
        if field not in value:
            errors.append(f"missing:{field}")
            continue
        actual = value[field]
        if isinstance(requirement, tuple) and requirement and all(isinstance(x, str) for x in requirement):
            if actual not in requirement:
                errors.append(f"enum:{field}")
        elif isinstance(requirement, type) and not isinstance(actual, requirement):
            errors.append(f"type:{field}")
        elif isinstance(requirement, tuple) and requirement and all(isinstance(x, type) for x in requirement):
            if not isinstance(actual, requirement):
                errors.append(f"type:{field}")
    confidence = value.get("confidence")
    if confidence is not None and (
        not isinstance(confidence, (int, float)) or isinstance(confidence, bool) or not 0 <= confidence <= 1
    ):
        errors.append("range:confidence")
    if isinstance(confidence, (int, float)) and confidence < confidence_required:
        errors.append("confidence:insufficient")
    for banned in ("chain_of_thought", "private_reasoning", "hidden_reasoning"):
        if banned in value:
            errors.append(f"forbidden:{banned}")
    return errors


ProviderCall = Callable[[str, str, str, int, bool], tuple[str, Mapping[str, Any]]]


class AIControl:
    def __init__(self, provider_call: ProviderCall | None = None):
        self.provider_call = provider_call or self._provider_call

    @staticmethod
    def _provider_call(
        prompt: str, task_type: str, tier: str, max_tokens: int, essential: bool,
    ) -> tuple[str, Mapping[str, Any]]:
        return llm_router.text_complete(
            prompt, task_type=task_type, model_tier=tier, max_output_tokens=max_tokens, essential=essential,
        )

    @staticmethod
    def _cache_key(prompt: str, request: AIRequest, schema: Mapping[str, Any]) -> str:
        shape = {
            key: getattr(value, "__name__", [getattr(x, "__name__", x) for x in value]
                         if isinstance(value, tuple) else str(value))
            for key, value in schema.items()
        }
        raw = json.dumps(
            {"v": 1, "task": request.task_type, "prompt": prompt, "schema": shape},
            sort_keys=True, ensure_ascii=False,
        )
        return hashlib.sha256(raw.encode()).hexdigest()

    def execute(
        self, prompt: str, request: AIRequest, schema: Mapping[str, Any], max_tokens: int = 900,
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        budget = cost_control.budget_status()
        if budget and request.remaining_budget_eur == Decimal("30"):
            request = replace(request, remaining_budget_eur=Decimal(str(budget.get("remaining_eur", "0"))))
        route = route_request(request)
        route_meta = {
            "tier": route.tier, "reason": route.reason, "task_type": request.task_type,
            "complexity": request.complexity, "risk": request.risk,
            "expected_value_to_cost": str(route.expected_value_to_cost),
            "purpose": request.purpose, "batch_size": request.batch_size,
            "previous_attempts": request.previous_attempts, "previous_model": request.previous_model,
            "urgency": request.urgency,
        }
        if route.tier == TIER_0:
            result = dict(request.deterministic_result or {})
            errors = validate_output(result, schema, request.confidence_required)
            if errors:
                raise ValueError(f"deterministic output invalid: {','.join(errors)}")
            cost_control.ai_event("AI_ROUTING_TIER_0", "completed", "Deterministic path selected", route_meta)
            return result, {**route_meta, "llm_called": False, "model_tier": TIER_0}

        cache_key = self._cache_key(prompt, request, schema)
        if request.cache_ttl_seconds:
            cached = cost_control.ai_cache_get(cache_key)
            if cached is not None and not validate_output(cached, schema, request.confidence_required):
                cost_control.ai_event("AI_CACHE_HIT", "completed", "Validated cached AI result used", route_meta)
                return cached, {**route_meta, "llm_called": False, "cache_hit": True}

        last_errors: list[str] = []
        for index, tier in enumerate(route.allowed_tiers):
            event = {
                TIER_1: "AI_ROUTING_CHEAP",
                TIER_2: "AI_ESCALATED_STANDARD",
                TIER_3: "AI_ESCALATED_STRONG",
            }[tier]
            cost_control.ai_event(event, "running", "AI request routed", {**route_meta, "tier": tier})
            try:
                essential = request.risk == "critical" and request.task_type in STRONG_TASKS
                text, provider_meta = self.provider_call(prompt, request.task_type, tier, max_tokens, essential)
                for attempt in provider_meta.get("provider_attempts", []):
                    if attempt.get("status") == "failed":
                        cost_control.ai_event(
                            "AI_PROVIDER_FAILED", "failed", "Configured provider failed", {**attempt, "tier": tier},
                        )
                if provider_meta.get("provider_failover_used"):
                    cost_control.ai_event(
                        "AI_FALLBACK_USED", "completed", "Configured provider fallback succeeded",
                        {"provider": provider_meta.get("provider"), "model": provider_meta.get("model"), "tier": tier},
                    )
                value = _clean_json(text)
                errors = validate_output(value, schema, request.confidence_required)
            except cost_control.BudgetBlocked:
                cost_control.ai_event(
                    "AI_BUDGET_BLOCKED", "blocked", "CFO guard stopped AI routing without tier escalation",
                    {"tier": tier, "task_type": request.task_type},
                )
                raise
            except Exception as exc:
                value, provider_meta, errors = {}, {}, [f"provider_or_parse:{type(exc).__name__}"]
            if not errors:
                if request.cache_ttl_seconds:
                    cost_control.ai_cache_put(cache_key, value, request.cache_ttl_seconds)
                return value, {
                    **route_meta, **provider_meta, "model_tier": tier,
                    "validation": "passed", "cache_hit": False,
                }
            last_errors = errors
            cost_control.ai_event(
                "AI_OUTPUT_INVALID", "failed", "AI output rejected by deterministic validation",
                {"tier": tier, "errors": errors, "task_type": request.task_type},
            )
            if index + 1 < len(route.allowed_tiers):
                prompt = (
                    "Return only one valid JSON object. Correct these validation errors: "
                    + ",".join(errors) + "\n" + prompt
                )
        raise ValueError(f"AI output invalid after allowed tiers: {','.join(last_errors)}")

    def execute_batch(
        self, prompt: str, items: list[Mapping[str, Any]], request: AIRequest,
        item_schema: Mapping[str, Any], max_tokens: int = 1800,
    ) -> tuple[list[Mapping[str, Any]], Mapping[str, Any]]:
        if not items or len(items) > 100:
            raise ValueError("batch must contain 1..100 items")
        batch_request = replace(request, batch_size=len(items))
        value, meta = self.execute(
            prompt + "\nITEMS=" + json.dumps(items, ensure_ascii=False),
            batch_request, {"items": list, "confidence": (int, float)}, max_tokens,
        )
        output = value["items"]
        if len(output) != len(items):
            raise ValueError("batch output count mismatch")
        errors = [
            validate_output(row, item_schema, request.confidence_required)
            for row in output if isinstance(row, dict)
        ]
        if len(errors) != len(output) or any(errors):
            raise ValueError("batch item validation failed")
        return output, meta
