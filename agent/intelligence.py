from __future__ import annotations

import json
from typing import Any

from agent import llm_router


def _json_call(prompt: str, task_type: str, max_tokens: int = 900) -> tuple[dict[str, Any], dict[str, Any]]:
    text, meta = llm_router.text_complete(prompt, task_type=task_type, max_output_tokens=max_tokens)
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:].lstrip()
    data = json.loads(cleaned)
    if not isinstance(data, dict):
        raise ValueError("Routed intelligence response must be a JSON object")
    return data, meta


def score_lead(facts: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Assess this LOCENIX lead using ONLY supplied facts. Return JSON with icp_fit 0-100, purchase_intent 0-100, priority low|medium|high, evidence (array), inferred_pain_points (array), next_best_action, and truth_state='AI_INFERRED'. Never invent company facts.\nFACTS={json.dumps(facts, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_CHEAP_CLASSIFICATION)


def personalize_outreach(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Create concise natural German LOCENIX outreach from supplied context. Return JSON with message, personalization_basis, confidence 0-1. No fabricated facts, no pressure, no company name unless explicitly useful, and no claim of an audit that was not performed.\nCONTEXT={json.dumps(context, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_PERSONALIZATION)


def classify_email_reply(email: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Classify an inbound LOCENIX email. Return JSON with classification, sales_intent 0-100, do_not_contact_recommended boolean, summary, reply_needed boolean, reply_draft. Do not make legal determinations and never authorize sending.\nEMAIL={json.dumps(email, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_CHEAP_CLASSIFICATION, 1000)


def draft_email(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Draft a personalized LOCENIX email using only supplied verified facts. Return JSON with subject, body, personalization_basis. This is drafting only: do not infer legal basis or sending approval.\nCONTEXT={json.dumps(context, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_PERSONALIZATION, 1000)


def analyze_gbp(facts: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Analyze supplied Google Business Profile/local SEO facts for LOCENIX. Return JSON with observed_issues, opportunities, prioritized_actions, sales_angle, inferred_items. Keep observed facts separate from inference and never invent rankings/reviews/categories.\nFACTS={json.dumps(facts, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_HIGH_REASONING, 1300)


def create_content(context: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    prompt = f"""Create LOCENIX LinkedIn content from supplied context and brand facts. Return JSON with hook, post, cta, rationale. Natural German, no fabricated data, no fake customer claims.\nCONTEXT={json.dumps(context, ensure_ascii=False)}"""
    return _json_call(prompt, llm_router.TASK_CONTENT, 1400)
