# LOCENIX LinkedIn Browser Agent

Cloud automation for LOCENIX without a PC, VPS, or Browser Use Cloud credits.

## Architecture

`Supabase schedules/jobs -> GitHub Actions -> local Chromium + Browser Use open source -> LinkedIn`

`Airtable LOCENIX LinkedIn Growth OS <-> agent tools`

`GPT-5.6 Luna -> Browser Use reasoning only when an AI decision is actually needed`

- **Supabase automation-hub** stores the four role prompts, schedules, queue, job state, cost state and browser-session metadata.
- **GitHub Actions** wakes every 15 minutes and only starts browser setup when queued work exists.
- **Chromium** runs directly on the GitHub-hosted runner, so Browser Use Cloud credits are not required.
- **LinkedIn login state** is persisted as an encrypted lean browser-profile snapshot in private Supabase Storage.
- **Airtable** is the operational source of truth for people, interactions, content, daily queue and growth intelligence.
- **GPT-5.6 Luna** supplies the agent reasoning through Browser Use `ChatOpenAI`.

## Required GitHub Actions secrets

1. `SUPABASE_SERVICE_ROLE_KEY`
2. `AIRTABLE_PAT`
3. `OPENAI_API_KEY`

Never commit or paste these values into the repository, logs, prompts, issues, or chat.

## Four Supabase-controlled roles

The durable schedules are stored in Supabase and are independent from the GitHub cron. The GitHub cron is only a wake/check mechanism.

- `inbox`: hourly at minute 00, Europe/Berlin
- `growth`: Monday-Friday at 09:30 and 17:30, Europe/Berlin
- `lead`: Monday-Friday at 10:00, Europe/Berlin
- `content`: Monday-Friday at 11:00, Europe/Berlin

The scheduler is idempotent by role/time slot so repeated 15-minute checks do not intentionally duplicate scheduled jobs.

## Cost-control design

Quality rules and the complete four Master Prompts remain binding. Cost is reduced by eliminating unnecessary inference instead of weakening qualification.

### 1. Zero-LLM hourly inbox gate

Before a scheduled Inbox Agent run calls Luna, Playwright opens only the LinkedIn feed and reads the global Messaging navigation badge. It does **not** open a conversation. Airtable is checked directly for due follow-ups.

Luna is called when any of these are true:

- LinkedIn reports unread messages;
- Airtable contains a clearly due follow-up;
- the lightweight probe is uncertain;
- a periodic full sweep is due (default every 4 hours).

If none are true, the hourly job completes with `llm_skipped=true` and estimated LLM cost `0.0`.

### 2. Token-efficient Browser Use

- `flash_mode=True`
- visual input disabled for routine DOM navigation
- model-visible thinking disabled
- message compaction enabled
- bounded recent history (10-12 items)
- stable Master Prompt remains at the front for prompt-cache reuse
- repeat Airtable reads and browser narration are explicitly discouraged

### 3. Hard budget protection

Default monthly emergency cap: **$10** (`MONTHLY_LLM_BUDGET_USD`).

Per-run emergency ceilings are intentionally generous and are not spending targets:

- Inbox: $0.25
- Growth: $0.75
- Lead: $0.75
- Content: $0.75

The worker records observed prompt tokens, cached prompt tokens, completion tokens and an estimated Luna cost into `agent_jobs.result`. Supabase aggregates those estimates for the current Europe/Berlin calendar month. Once the monthly cap is reached, further AI jobs fail closed instead of continuing to spend.

### 4. Quality safeguards stay on

- Browser Use final judging remains enabled for real AI runs.
- Full lead/content/growth step ceilings remain high enough for complete work.
- The Inbox gate falls back to Luna whenever detection is uncertain.
- Every four hours an Inbox full sweep runs even when the badge is quiet.
- Airtable duplicate protection, `Do Not Contact`, confirmation rules and visual approval remain unchanged.

## Safety behavior

- Airtable is checked before contact/research to prevent duplicates and respect `Do Not Contact`.
- Only technically confirmed external actions may be recorded as sent/published/executed.
- CAPTCHA, 2FA, security checkpoints, rate limits and platform warnings are never bypassed.
- Content visuals retain the explicit approval gate defined in the Content Agent prompt.
- If a required capability is unavailable, the runtime reports a blocker instead of fabricating success.

## Login flow

A `login` job opens a temporary noVNC view backed by Chromium on the GitHub runner. The human completes LinkedIn authentication. After confirmation, the worker verifies the feed and stores a minimized encrypted browser profile in Supabase. Later jobs restore that profile on fresh GitHub runners.

## Main runtime

```bash
python -m agent.airtable_worker --once
```

The production adapter reuses the proven Supabase queue/profile/browser lifecycle in `agent.local_worker`, injects LOCENIX Airtable tools, performs the deterministic inbox cost gate, and swaps only the inference layer to GPT-5.6 Luna.

## Relevant files

- `agent/airtable_worker.py` — Luna adapter, token metering and budget enforcement
- `agent/cost_gate.py` — zero-LLM Inbox gate
- `agent/local_worker.py` — GitHub Chromium, encrypted profile and queue lifecycle
- `agent/airtable_tools.py` / `agent/extended_airtable_tools.py` — LOCENIX Airtable actions
- `.github/workflows/locenix-cloud-agent.yml` — 15-minute scheduler wake + up to two queued jobs per runner
- `wake.txt` — immediate manual/chat-triggered wake

## Current rollout rule

Keep the four Supabase schedules disabled until `OPENAI_API_KEY` is present and a strict read-only Luna QA run confirms Airtable + restored LinkedIn + correct no-write behavior. Only then switch production scheduling from the old ChatGPT tasks to Supabase/GitHub.
