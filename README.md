# LOCENIX Cloud Browser Agent

Cloud-only browser automation for LOCENIX. No local PC, VPS, or always-on server is required.

## Architecture

`ChatGPT -> Supabase agent_jobs -> GitHub Actions -> Browser Use Cloud -> LinkedIn -> result/live URL -> Supabase -> ChatGPT`

- **Supabase automation-hub** is the control plane and memory.
- **GitHub Actions** is the disposable runner. It wakes on `wake.txt`, manually, and every 15 minutes.
- **Browser Use Cloud** hosts the actual browser and persistent login profile.
- **ChatGPT** can queue jobs, wake the runner, read status/results, and return a Browser Use live URL when human login is required.

## Supported job modes

- `view` — read-only browser work. No external changes.
- `act` — perform the explicitly requested action.
- `autonomous` — complete a bounded browser task autonomously.
- `login` — open a persistent LinkedIn login browser and return a live URL for manual sign-in.
- `stop_session` — stop the login browser so the profile/cookies are persisted for later runs.

Security checkpoints, CAPTCHA, and 2FA are left for human interaction instead of being bypassed.

## One-time GitHub configuration

The workflow intentionally refuses to run until these repository Actions secrets exist:

1. `SUPABASE_SERVICE_ROLE_KEY` — service-role key for the existing `automation-hub` Supabase project.
2. `BROWSER_USE_API_KEY` — Browser Use Cloud API key.

Do not commit either value to the repository, `.env`, issues, logs, or chat prompts.

The public Supabase project URL is already configured in `.github/workflows/locenix-cloud-agent.yml`.

## LinkedIn login flow

1. Queue an `agent_jobs` row with `mode = 'login'`.
2. Wake the GitHub Action by updating `wake.txt`.
3. The worker creates or reuses the Browser Use profile `locenix-linkedin`.
4. The Browser Use live URL is written to `agent_jobs.result.live_url` and `agent_browser_sessions.live_url`.
5. Open that live URL and sign in to LinkedIn yourself, including any 2FA/security check.
6. Queue a `stop_session` job and wake the runner.
7. Browser Use stops the session and persists the profile state.

After that, normal LOCENIX jobs reuse the same profile automatically.

## Queue examples

Read-only:

```sql
insert into public.agent_jobs (task, mode)
values ('Open LinkedIn and summarize the newest inbox conversations.', 'view');
```

Bounded action:

```sql
insert into public.agent_jobs (task, mode)
values ('Open the specified LinkedIn conversation and draft the next natural reply. Do not send it.', 'act');
```

Login:

```sql
insert into public.agent_jobs (task, mode)
values ('Prepare LinkedIn login.', 'login');
```

Stop the active login browser:

```sql
insert into public.agent_jobs (task, mode)
values ('Persist LinkedIn login and close the browser.', 'stop_session');
```

## Cloud worker

The main runtime is:

```bash
python -m agent.cloud_worker --once
```

The worker:

1. atomically claims one queued job using `claim_agent_job()`;
2. creates/reuses the persistent Browser Use profile;
3. starts the cloud browser/agent session;
4. saves the live URL and progress in Supabase;
5. polls the cloud session;
6. writes the final result, status, cost, and errors back to Supabase.

The GitHub workflow processes up to five queued jobs per invocation.

## Cost controls

The default Browser Use per-task cap is `1.00 USD` and can be overridden per job in `agent_jobs.input.max_cost_usd`. Login preparation uses a lower default cap.

## Files

- `agent/cloud_worker.py` — cloud runner and login/session lifecycle
- `.github/workflows/locenix-cloud-agent.yml` — GitHub Actions runtime
- `wake.txt` — immediate ChatGPT/GitHub wake trigger
- `agent/worker.py` / `agent/browser_runner.py` — legacy self-hosted baseline, no longer used by the cloud workflow
