from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from agent import llm_router
from agent.ai_control import AIControl, AIRequest


def _json_call(
    prompt: str, task_type: str, schema: dict[str, Any], max_tokens: int = 900,
    *, complexity: float = 0.3, risk: str = "low", expected_value_eur: str = "1",
    confidence_required: float = 0.7, cache_ttl_seconds: int = 0,
) -> tuple[dict[str, Any], dict[str, Any]]:
    data, meta = AIControl().execute(
        prompt,
        AIRequest(
            task_type=task_type, purpose=task_type, complexity=complexity, risk=risk,
            expected_value_eur=Decimal(expected_value_eur),
            confidence_required=confidence_required, cache_ttl_seconds=cache_ttl_seconds,
        ),
        schema, max_tokens,
    )
    return dict(data), dict(meta)


def score_lead(facts: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Assess this LOCENIX lead using ONLY supplied facts. Return JSON with icp_fit 0-100, purchase_intent 0-100, priority low|medium|high, evidence (array), inferred_pain_points (array), next_best_action, truth_state='AI_INFERRED', confidence 0-1, and reason_summary. Never invent company facts.\nFACTS={json.dumps(facts, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_CHEAP_CLASSIFICATION, {
        "icp_fit": (int, float), "purchase_intent": (int, float),
        "priority": ("low", "medium", "high"), "evidence": list,
        "inferred_pain_points": list, "next_best_action": str,
        "truth_state": ("AI_INFERRED",), "confidence": (int, float), "reason_summary": str,
    })


def personalize_outreach(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Create concise natural German LOCENIX outreach from supplied context. Return JSON with message, personalization_basis, confidence 0-1, reason_summary and requires_escalation. No fabricated facts, no pressure, no company name unless explicitly useful, and no claim of an audit that was not performed.\nCONTEXT={json.dumps(context, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_PERSONALIZATION, {
        "message": str, "personalization_basis": str, "confidence": (int, float),
        "reason_summary": str, "requires_escalation": bool,
    }, confidence_required=0.75)


def classify_email_reply(email: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Classify an inbound LOCENIX email. Return JSON with classification, sales_intent 0-100, do_not_contact_recommended boolean, summary, reply_needed boolean, reply_draft, confidence 0-1 and reason_summary. Do not make legal determinations and never authorize sending.\nEMAIL={json.dumps(email, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_CHEAP_CLASSIFICATION, {
        "classification": str, "sales_intent": (int, float),
        "do_not_contact_recommended": bool, "summary": str, "reply_needed": bool,
        "reply_draft": str, "confidence": (int, float), "reason_summary": str,
    }, 1000)


def draft_email(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Draft a personalized LOCENIX email using only supplied verified facts. Return JSON with subject, body, personalization_basis, confidence 0-1 and reason_summary. This is drafting only: do not infer legal basis or sending approval.\nCONTEXT={json.dumps(context, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_PERSONALIZATION, {
        "subject": str, "body": str, "personalization_basis": str,
        "confidence": (int, float), "reason_summary": str,
    }, 1000, confidence_required=0.75)


def analyze_gbp(facts: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Analyze supplied Google Business Profile/local SEO facts for LOCENIX. Return JSON with observed_issues, opportunities, prioritized_actions, sales_angle, inferred_items, confidence 0-1 and reason_summary. Keep observed facts separate from inference and never invent rankings/reviews/categories.\nFACTS={json.dumps(facts, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_HIGH_REASONING, {
        "observed_issues": list, "opportunities": list, "prioritized_actions": list,
        "sales_angle": str, "inferred_items": list,
        "confidence": (int, float), "reason_summary": str,
    }, 1300, complexity=0.78, risk="medium", expected_value_eur="10",
        confidence_required=0.8, cache_ttl_seconds=86400)


def create_content(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Create LOCENIX LinkedIn content from supplied context and brand facts. Return JSON with hook, post, cta, rationale, confidence 0-1 and reason_summary. Natural German, no fabricated data, no fake customer claims.\nCONTEXT={json.dumps(context, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_CONTENT, {
        "hook": str, "post": str, "cta": str, "rationale": str,
        "confidence": (int, float), "reason_summary": str,
    }, 1400, complexity=0.55)
