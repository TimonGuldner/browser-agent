from __future__ import annotations

"""Deterministic contracts for the LOCENIX autonomous company foundation."""

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any, Mapping

from agent.org_architecture import CANONICAL_COMPANY, canonical_role


class FailureStage(str, Enum):
    TASK = "TASK"
    EXECUTE = "EXECUTE"
    VERIFY_INITIAL = "VERIFY_INITIAL"
    FAIL = "FAIL"
    RETRY = "RETRY"
    DIAGNOSE = "DIAGNOSE"
    ALTERNATIVE = "ALTERNATIVE"
    SPECIALIST = "SPECIALIST"
    CTO = "CTO"
    FIX = "FIX"
    TEST = "TEST"
    DEPLOY = "DEPLOY"
    RETRY_ORIGINAL = "RETRY_ORIGINAL"
    VERIFY_FINAL = "VERIFY_FINAL"
    RESOLVED = "RESOLVED"
    HUMAN_GATE = "HUMAN_GATE"


FAILURE_PATH = (
    FailureStage.TASK,
    FailureStage.EXECUTE,
    FailureStage.VERIFY_INITIAL,
    FailureStage.FAIL,
    FailureStage.RETRY,
    FailureStage.DIAGNOSE,
    FailureStage.ALTERNATIVE,
    FailureStage.SPECIALIST,
    FailureStage.CTO,
    FailureStage.FIX,
    FailureStage.TEST,
    FailureStage.DEPLOY,
    FailureStage.RETRY_ORIGINAL,
    FailureStage.VERIFY_FINAL,
    FailureStage.RESOLVED,
)

HUMAN_GATE_CODES = frozenset(
    {
        "CAPTCHA",
        "2FA",
        "CHECKPOINT",
        "AUTHWALL",
        "PAYMENT_APPROVAL",
        "LEGAL_APPROVAL",
        "VISUAL_APPROVAL",
        "ACCOUNT_RESTRICTION",
        "OWNER_DECISION",
    }
)


@dataclass(frozen=True)
class FailureDecision:
    stage: FailureStage
    owner: str
    human_required: bool
    reason_code: str


def next_failure_action(
    current: FailureStage | str,
    *,
    failure_code: str = "",
) -> FailureDecision:
    """Return one bounded transition; only explicit gates reach the owner."""
    stage = FailureStage(current)
    code = str(failure_code or "").strip().upper()
    if code in HUMAN_GATE_CODES:
        return FailureDecision(FailureStage.HUMAN_GATE, "OWNER", True, code)
    if stage in {FailureStage.RESOLVED, FailureStage.HUMAN_GATE}:
        return FailureDecision(stage, "OWNER" if stage is FailureStage.HUMAN_GATE else "WATCHDOG", stage is FailureStage.HUMAN_GATE, code or "NOOP")
    index = FAILURE_PATH.index(stage)
    next_stage = FAILURE_PATH[index + 1]
    if next_stage in {FailureStage.SPECIALIST}:
        owner = "SPECIALIST"
    elif next_stage in {FailureStage.CTO, FailureStage.FIX, FailureStage.TEST, FailureStage.DEPLOY}:
        owner = "CTO"
    elif next_stage is FailureStage.RESOLVED:
        owner = "WATCHDOG"
    else:
        owner = "WATCHDOG"
    return FailureDecision(next_stage, owner, False, code or "AUTOMATED_CHAIN")


def stable_dedupe_key(*parts: object) -> str:
    normalized = json.dumps(parts, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class TaskEnvelope:
    department: str
    task_type: str
    business_key: str
    payload: Mapping[str, Any]
    correlation_id: str
    idempotency_key: str

    @classmethod
    def build(
        cls,
        *,
        department: str,
        task_type: str,
        business_key: str,
        payload: Mapping[str, Any] | None = None,
        correlation_id: str = "",
    ) -> "TaskEnvelope":
        canonical = canonical_role(department)
        task = str(task_type or "").strip().lower()
        key = str(business_key or "").strip()
        if not task or not key:
            raise ValueError("task_type and business_key are required")
        body = dict(payload or {})
        correlation = correlation_id.strip() or stable_dedupe_key("correlation", canonical, key)[:32]
        idem = stable_dedupe_key("locenix-v1", canonical, task, key, body)
        return cls(canonical, task, key, body, correlation, idem)


@dataclass(frozen=True)
class SpendDecision:
    allowed: bool
    reason: str
    projected_eur: Decimal
    remaining_eur: Decimal


@dataclass(frozen=True)
class BudgetPolicy:
    monthly_limit_eur: Decimal = Decimal("30.00")
    reserve_eur: Decimal = Decimal("3.00")
    strong_model_limit_eur: Decimal = Decimal("4.50")

    def authorize(
        self,
        *,
        spent_eur: Decimal | str | float,
        committed_eur: Decimal | str | float,
        requested_eur: Decimal | str | float,
        essential: bool,
        model_tier: str = "deterministic",
        difficult_decision: bool = False,
        strong_model_spent_eur: Decimal | str | float = Decimal("0"),
    ) -> SpendDecision:
        spent = Decimal(str(spent_eur))
        committed = Decimal(str(committed_eur))
        requested = Decimal(str(requested_eur))
        strong_spent = Decimal(str(strong_model_spent_eur))
        if min(spent, committed, requested, strong_spent) < 0:
            raise ValueError("cost values must not be negative")
        projected = spent + committed + requested
        remaining = self.monthly_limit_eur - projected
        if projected > self.monthly_limit_eur:
            return SpendDecision(False, "MONTHLY_HARD_CAP", projected, remaining)
        tier = str(model_tier or "").strip().lower()
        if tier == "strong" and not difficult_decision:
            return SpendDecision(False, "STRONG_MODEL_NOT_JUSTIFIED", projected, remaining)
        if tier == "strong" and strong_spent + requested > self.strong_model_limit_eur:
            return SpendDecision(False, "STRONG_MODEL_CAP", projected, remaining)
        if not essential and projected > self.monthly_limit_eur - self.reserve_eur:
            return SpendDecision(False, "RESERVE_PROTECTED", projected, remaining)
        return SpendDecision(True, "AUTHORIZED", projected, remaining)


def requires_llm(*, deterministic_available: bool, ambiguous: bool) -> bool:
    """Default to code/rules; LLM use needs both ambiguity and no adequate rule."""
    return bool(ambiguous and not deterministic_available)


def validate_foundation() -> None:
    required = {
        "CEO", "CMO", "CTO", "CFO", "OPPORTUNITY", "DISTRIBUTION",
        "OUTREACH", "CONVERSION", "ANALYTICS", "WATCHDOG",
    }
    if set(CANONICAL_COMPANY) != required:
        raise RuntimeError("Canonical LOCENIX department set is incomplete")
    if FAILURE_PATH[-1] is not FailureStage.RESOLVED:
        raise RuntimeError("Failure path must end in verified resolution")


validate_foundation()
