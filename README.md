# LOCENIX LinkedIn Browser Agent

Cloud automation for LOCENIX without a PC, VPS, or Browser Use Cloud credits.

## Architecture

`Supabase schedules/jobs -> GitHub Actions -> local Chromium + Browser Use open source -> LinkedIn`

`Airtable LOCENIX LinkedIn Growth OS <-> agent tools`

`Google Gemini Flash -> Browser Use reasoning`

- **Supabase automation-hub** stores the four role prompts, schedules, queue, job state and browser-session metadata.
- **GitHub Actions** wakes every 15 minutes (and on `wake.txt`) and only starts the heavier browser setup if queued work exists.
- **Chromium** runs directly on the GitHub-hosted runner, so Browser Use Cloud credits are not required.
- **LinkedIn login state** is persisted as an encrypted lean browser-profile snapshot in the private Supabase storage bucket.
- **Airtable** remains the operational source of truth for people, interactions, content, daily queue and growth intelligence.
- **Gemini Flash** supplies the agent reasoning via Browser Use `ChatGoogle`; the former tiny local Qwen/Ollama model is not used for production agent decisions.

## Required GitHub Actions secrets

1. `SUPABASE_SERVICE_ROLE_KEY`
2. `AIRTABLE_PAT`
3. `GOOGLE_API_KEY`

Never commit or paste these values into the repository, logs, prompts, issues, or chat.

## Four Supabase-controlled roles

The durable schedules are stored in Supabase and are intentionally independent from the GitHub cron. The GitHub cron is only a wake/check mechanism.

- `inbox`: hourly at minute 00, Europe/Berlin
- `growth`: Monday-Friday at 09:30 and 17:30, Europe/Berlin
- `lead`: Monday-Friday at 10:00, Europe/Berlin
- `content`: Monday-Friday at 11:00, Europe/Berlin

The four schedules can remain disabled while a new inference configuration is being QA-tested. The scheduler function is idempotent by role/time slot, so repeated 15-minute checks do not intentionally create duplicate scheduled jobs.

## Safety behavior

- Airtable is checked before contact/research to prevent duplicates and respect `Do Not Contact`.
- Only technically confirmed external actions may be recorded as sent/published/executed.
- CAPTCHA, 2FA, security checkpoints, rate limits and platform warnings are never bypassed.
- Content visuals retain the explicit approval gate defined in the Content Agent prompt.
- If a required capability is unavailable, the runtime must report a blocker instead of fabricating success.

## Login flow

A `login` job opens a temporary noVNC view backed by Chromium on the GitHub runner. The human completes LinkedIn authentication. After confirmation, the worker verifies the feed and stores a minimized encrypted browser profile in Supabase. Later jobs restore that profile on fresh GitHub runners.

## Main runtime

```bash
python -m agent.airtable_worker --once
```

The Airtable-enabled worker reuses the proven Supabase queue/profile/browser lifecycle in `agent.local_worker`, injects the LOCENIX Airtable tools, and swaps the inference layer to Google Gemini Flash.

## Relevant files

- `agent/airtable_worker.py` — production worker adapter, Airtable tools + Gemini LLM
- `agent/local_worker.py` — GitHub Chromium, encrypted profile, queue lifecycle
- `agent/airtable_tools.py` / `agent/extended_airtable_tools.py` — LOCENIX Airtable actions
- `.github/workflows/locenix-cloud-agent.yml` — 15-minute scheduler wake + job runner
- `wake.txt` — immediate manual/chat-triggered wake

## Cost model

The standard GitHub-hosted runner is free for this public repository. Google currently offers a Gemini Developer API free tier for supported models. Free-tier limits still apply and can change. The free tier may use submitted content to improve Google products; use a paid/provider configuration instead if that data-use policy is not acceptable for the workload.
