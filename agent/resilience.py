from __future__ import annotations

"""Deterministic incident classification and bounded escalation policy."""

import hashlib
import re
from dataclasses import dataclass

LEVELS = ("L0", "L1", "L2", "L3", "L4", "L5")
HUMAN_GATES = {
    "captcha": "CAPTCHA", "2fa": "TWO_FACTOR_AUTH", "human verification": "HUMAN_VERIFICATION",
    "payment approval": "PAYMENT_APPROVAL", "legal approval": "LEGAL_APPROVAL",
    "account restriction": "ACCOUNT_RESTRICTION", "checkpoint": "ACCOUNT_CHECKPOINT",
}
RUNBOOKS = (
    (("deployment_not_yet_verified", "deployment not yet verified"), "DEPLOYMENT_PENDING", "publishing", "verify_after_backoff"),
    (("timed out", "timeout", "connection reset"), "TRANSIENT_TIMEOUT", "runtime", "exponential_backoff"),
    (("stale", "heartbeat"), "STALE_WORKER_OR_TASK", "worker", "restart_or_requeue"),
    (("429", "rate limit", "quota"), "PROVIDER_RATE_LIMIT", "ai_provider", "configured_provider_fallback"),
    (("502", "503", "504", "service unavailable"), "PROVIDER_UNAVAILABLE", "api_provider", "circuit_breaker_then_fallback"),
    (("session", "browser"), "BROWSER_SESSION_FAILURE", "browser", "recreate_session"),
    (("token expired", "401 unauthorized"), "TOKEN_EXPIRED", "authentication", "configured_token_refresh"),
)


@dataclass(frozen=True)
class RecoveryPlan:
    failure_code: str
    component: str
    action: str
    escalation_level: str
    owner: str
    retry: bool
    backoff_seconds: int
    ai_tier: str
    human_gate: bool
    fingerprint: str


def classify_failure(error: str) -> tuple[str, str, str, bool]:
    normalized = (error or "").lower()
    for marker, code in HUMAN_GATES.items():
        if marker in normalized:
            return code, "human_gate", "owner_required", True
    for markers, code, component, action in RUNBOOKS:
        if any(marker in normalized for marker in markers):
            return code, component, action, False
    return "UNKNOWN_FAILURE", "unknown", "collect_tools_logs_then_diagnose", False


def fingerprint(error: str, component: str = "") -> str:
    code, inferred, _, human = classify_failure(error)
    stable_component = component or inferred
    if code != "UNKNOWN_FAILURE" or human:
        raw = f"{stable_component}:{code}"
    else:
        normalized = re.sub(r"[0-9a-f]{8,}|\b\d+\b", "#", (error or "").lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()[:240]
        raw = f"{stable_component}:{code}:{normalized}"
    return "incident:" + hashlib.sha256(raw.encode()).hexdigest()


def plan_recovery(
    error: str, *, attempt: int, max_attempts: int, component: str = "",
    business_impact: str = "medium",
) -> RecoveryPlan:
    code, inferred, action, human = classify_failure(error)
    target_component = component or inferred
    if human:
        return RecoveryPlan(
            code, target_component, action, "L5", "OWNER", False, 0,
            "deterministic", True, fingerprint(error, target_component),
        )

    if attempt >= max_attempts:
        level = "L4"
        retry = False
    else:
        level = ("L0", "L1", "L2", "L3", "L4")[min(attempt, 4)]
        retry = True
    owner = {
        "L0": "WORKER", "L1": "DEPARTMENT", "L2": "SPECIALIST",
        "L3": "CTO" if target_component not in {"strategy", "channel"} else "CMO",
        "L4": "CEO",
    }[level]
    if code != "UNKNOWN_FAILURE":
        ai_tier = "deterministic"
    elif level in {"L0", "L1"}:
        ai_tier = "cheap"
    elif level == "L2":
        ai_tier = "standard"
    elif level in {"L3", "L4"} and business_impact in {"high", "critical"}:
        ai_tier = "strong"
    else:
        ai_tier = "standard"
    backoff = 0 if not retry else min(1800, 30 * (2 ** min(attempt, 6)))
    return RecoveryPlan(
        code, target_component, action, level, owner, retry, backoff,
        ai_tier, False, fingerprint(error, target_component),
    )


def should_circuit_break(recent_failures: int, distinct_approaches: int) -> bool:
    return recent_failures >= 3 and distinct_approaches < 2
