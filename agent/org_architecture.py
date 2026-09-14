from __future__ import annotations

"""Canonical LOCENIX org model.

Agent 0 manages outcomes through Heads. Heads coordinate agents/workers.
Deterministic workers execute side effects; LLMs are reserved for language
or ambiguous interpretation.
"""

ORG = {
    "AGENT_0_CEO": {
        "responsibility": ["targets", "kpis", "priorities", "delegation", "verification", "escalation"],
        "forbidden": ["write_email", "send_email", "research_lead", "write_dm", "handle_inbox", "publish_content"],
        "reports": ["SALES_HEAD", "GROWTH_HEAD", "OPS_HEAD"],
    },
    "SALES_HEAD": {
        "outcome": "qualified_lead_to_paid_customer",
        "pipelines": {
            "research": ["MAPS_OUTSCRAPER_WORKER", "CONTACT_ENRICHMENT_WORKER", "QUALIFICATION_WORKER"],
            "email": ["EMAIL_COPY_AGENT", "COMPLIANCE_HARD_GATE", "EMAIL_SENDER_WORKER"],
            "linkedin": ["LINKEDIN_RESEARCH_WORKER", "CONNECTION_WORKER", "DM_AGENT", "ENGAGEMENT_WORKER"],
            "conversation": ["CONVERSATION_AGENT", "REPLY_CLASSIFIER", "FOLLOWUP_WORKER"],
        },
    },
    "GROWTH_HEAD": {"reports": ["CONTENT_AGENT", "CHANNEL_TRAFFIC_AGENT", "EXPERIMENT_AGENT", "ANALYTICS_LEARNING_AGENT"]},
    "OPS_HEAD": {"reports": ["QUEUE_MANAGER", "SCHEDULER", "WATCHDOG", "RETRY_CONTROLLER", "REPAIR_AGENT", "LLM_ROUTER"]},
}

ACTIVITY_TARGETS = {"qualified_leads": 20, "emails": 20, "connections": 10, "dms": 5, "comments": 3}
FUNNEL_METRICS = ("replies", "positive_replies", "checks_offered", "checks_requested", "trials", "paid_customers")

PRIORITY_CLASSES = {
    "P0_REVENUE_NOW": 100,
    "P1_ACTIVE_CONVERSATION": 80,
    "P2_QUALIFIED_OUTREACH": 60,
    "P3_PIPELINE_CREATION": 40,
    "P4_GROWTH_SUPPORT": 20,
}

LEAD_STATES = (
    "DISCOVERED", "ENRICHED", "QUALIFIED", "OUTREACH_READY", "CONTACTED", "REPLIED", "INTERESTED",
    "CHECK_OFFERED", "CHECK_REQUESTED", "CHECK_COMPLETED", "TRIAL", "PAID", "NOT_INTERESTED", "DO_NOT_CONTACT", "INVALID", "BLOCKED",
)
TERMINAL_STATES = {"PAID", "NOT_INTERESTED", "DO_NOT_CONTACT", "INVALID"}

ESCALATION = {
    1: "WORKER_RETRY",
    2: "HEAD_ALTERNATIVE",
    3: "REPAIR_AGENT",
    4: "AGENT_0_REALLOCATE_OR_ESCALATE",
    5: "HUMAN_REQUIRED",
}

BLOCKER_CODES = {
    "AUTH_REQUIRED", "CAPTCHA", "API_LIMIT", "NO_APPROVED_EMAIL_LEADS", "NO_QUALIFIED_LEADS",
    "PROVIDER_FAILURE", "COMPLIANCE_BLOCK", "BROWSER_FAILURE",
}


# Run 1 canonical company taxonomy. Existing Head/worker names stay valid through
# LEGACY_ROLE_MAP so current jobs keep running while orchestration migrates.
CANONICAL_COMPANY = {
    "CEO": {"parent": None, "owns": ("mission", "priorities", "human_gates")},
    "CMO": {"parent": "CEO", "owns": ("growth_portfolio", "go_to_market")},
    "CTO": {"parent": "CEO", "owns": ("runtime", "repairs", "deployments")},
    "CFO": {"parent": "CEO", "owns": ("budget", "cost_ledger")},
    "OPPORTUNITY": {"parent": "CMO", "owns": ("demand_signals", "experiments")},
    "DISTRIBUTION": {"parent": "CMO", "owns": ("channels", "qualified_traffic")},
    "OUTREACH": {"parent": "CMO", "owns": ("permission_gates", "conversations")},
    "CONVERSION": {"parent": "CMO", "owns": ("checks", "trials", "paid")},
    "ANALYTICS": {"parent": "CMO", "owns": ("metrics", "attribution", "learning")},
    "WATCHDOG": {"parent": "CEO", "owns": ("health", "incidents", "escalation")},
}

LEGACY_ROLE_MAP = {
    "AGENT_0_CEO": "CEO",
    "SALES_HEAD": "CMO",
    "GROWTH_HEAD": "CMO",
    "OPS_HEAD": "CTO",
    "MAPS_OUTSCRAPER_WORKER": "OPPORTUNITY",
    "CONTACT_ENRICHMENT_WORKER": "OPPORTUNITY",
    "QUALIFICATION_WORKER": "OPPORTUNITY",
    "CHANNEL_TRAFFIC_AGENT": "DISTRIBUTION",
    "LINKEDIN_RESEARCH_WORKER": "DISTRIBUTION",
    "ENGAGEMENT_WORKER": "DISTRIBUTION",
    "EMAIL_COPY_AGENT": "OUTREACH",
    "EMAIL_SENDER_WORKER": "OUTREACH",
    "DM_AGENT": "OUTREACH",
    "CONVERSATION_AGENT": "CONVERSION",
    "FOLLOWUP_WORKER": "CONVERSION",
    "ANALYTICS_LEARNING_AGENT": "ANALYTICS",
    "LLM_ROUTER": "CFO",
    "QUEUE_MANAGER": "CTO",
    "SCHEDULER": "CTO",
    "RETRY_CONTROLLER": "WATCHDOG",
    "REPAIR_AGENT": "CTO",
    "WATCHDOG": "WATCHDOG",
}


def canonical_role(role: str) -> str:
    key = str(role or "").strip().upper()
    canonical = LEGACY_ROLE_MAP.get(key, key)
    if canonical not in CANONICAL_COMPANY:
        raise ValueError(f"Unknown LOCENIX company role: {role}")
    return canonical


def validate_company_model() -> None:
    for role, config in CANONICAL_COMPANY.items():
        parent = config["parent"]
        if role == "CEO" and parent is not None:
            raise RuntimeError("CEO must be the root")
        if parent is not None and parent not in CANONICAL_COMPANY:
            raise RuntimeError(f"{role} references unknown parent {parent}")


validate_company_model()
