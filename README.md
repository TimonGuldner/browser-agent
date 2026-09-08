# Browser Agent

Self-hosted browser automation worker built around the open-source **Browser Use** Python library and a **Supabase** job queue.

## Architecture

`Client / ChatGPT integration -> Supabase browser_jobs -> Python worker -> Browser Use -> Chromium -> result -> Supabase`

The browser runs on your own machine/VPS. Browser Use Cloud is optional.

## Requirements

- Python 3.12 recommended
- Windows or Linux VPS/local machine
- Supabase project
- One supported LLM provider key (OpenAI, Browser Use, Google, or Anthropic)

## Install on Windows

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
pip install -e .
browser-use install
```

## Install on Linux

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e .
browser-use install
```

## Configure

Copy `.env.example` to `.env` and fill in your own secrets. Never commit `.env` or real API keys.

Apply `sql/schema.sql` in the Supabase SQL editor.

## Run

```bash
python -m agent.worker
```

Insert a job from a trusted server-side component:

```sql
insert into public.browser_jobs (task)
values ('Open example.com and return the page title.');
```

The worker claims the task, runs it in Browser Use, then writes the result back to the same row.

## Security model

- Supabase service-role key belongs only on the worker/server, never in browser/frontend code.
- RLS is enabled and no public table policy is created by default.
- LLM/API credentials are environment variables only.
- Do not commit browser profiles, cookies, session data, screenshots containing private data, or `.env` files.
- Add an allowlist/approval layer before letting untrusted users submit arbitrary browser tasks.

## Scaling

The first worker is intentionally simple. `sql/schema.sql` also contains an atomic `claim_browser_job()` function using `FOR UPDATE SKIP LOCKED` for the next step when multiple workers are introduced.

## Upstream

Browser Use: https://github.com/browser-use/browser-use

This project consumes Browser Use as a dependency instead of copying its source. That keeps upstream updates maintainable and preserves a clean separation between the browser framework and this agent's orchestration layer.
