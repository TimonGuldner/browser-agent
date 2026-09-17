# LOCENIX Reddit Posting Worker

A small, non-LLM worker that treats Airtable as the source of truth, uses a persistent Browserbase browser context for Reddit login, checks for duplicates before every write, publishes only pre-approved `Ready-to-Post Copy`, verifies the result, and writes the permalink/status back to Airtable.

## Safety model

The worker only considers an Airtable row when all of these are true:

- `Status = Approved`
- `Bot May Publish = true`
- `Ready-to-Post Copy` is not empty
- `Published URL` is empty

Publishing is additionally disabled unless GitHub secret `REDDIT_PUBLISH_ENABLED` is exactly `true`. Leave it unset/false while setting up and testing.

For a `Reply`, `Target Permalink` must be present and `Target Reddit ID` must start with `t1_`. For a thread `Comment`, a `t3_` target or an unambiguous thread URL is required. An `Own Thread` needs `Target Subreddit` and `Draft Title`.

## Architecture

GitHub Actions → Airtable → Browserbase persistent Context → Playwright → Reddit → verification → Airtable.

The worker has no LLM and never rewrites `Ready-to-Post Copy`.

## 1. Browserbase

1. Create/sign in to Browserbase.
2. Create a project if your Browserbase account/workflow still uses project IDs.
3. Create a persistent Browserbase Context in the Browserbase dashboard.
4. Copy the API key and Context ID.

All sessions reuse `BROWSERBASE_CONTEXT_ID` with context persistence enabled, so Reddit cookies/session state are stored in Browserbase rather than GitHub Actions.

## 2. GitHub Secrets

Repository → **Settings → Secrets and variables → Actions → New repository secret**.

Required:

- `BROWSERBASE_API_KEY`
- `BROWSERBASE_CONTEXT_ID`
- `AIRTABLE_TOKEN`

Recommended:

- `BROWSERBASE_PROJECT_ID`
- `EXPECTED_REDDIT_USERNAME` — exact Reddit username to prevent posting from the wrong account

Safety switch:

- `REDDIT_PUBLISH_ENABLED=false` initially

Do not commit any of these values.

## 3. Airtable

Base: `appEpBPsuKXOFLxQD`

Table: `Action Queue`

Expected fields:

- Action
- Action Type
- Target URL
- Target Permalink
- Target Reddit ID
- Thread Reddit ID
- Ready-to-Post Copy
- Draft Title
- Status
- Bot May Publish
- Dedupe Status
- Dedupe Checked At
- Existing Reddit URL
- Published At
- Published URL
- Last Error
- Target Subreddit

The Airtable token needs read/write access to this base.

## 4. First Reddit login

Keep `REDDIT_PUBLISH_ENABLED=false`.

1. Go to **Actions → LOCENIX Reddit Worker → Run workflow**.
2. The worker creates a Browserbase session using your persistent Context.
3. If Reddit is not logged in it prints `LOGIN_REQUIRED` plus the Browserbase Live Session URL.
4. Open that URL while the workflow is still running.
5. Log in to Reddit manually. Do not store the Reddit password in GitHub.
6. The worker polls for up to 10 minutes, detects the logged-in username, prints `LOGIN_COMPLETE`, then exits **without posting anything**.

Later runs reuse the same Browserbase Context. If Reddit expires the session, repeat this manual-login workflow.

## 5. Safe first test

Before enabling real publishing:

1. Confirm `EXPECTED_REDDIT_USERNAME` is set.
2. Leave `REDDIT_PUBLISH_ENABLED=false` and run the workflow manually. It should read/dedupe but not submit new content.
3. Pick exactly one Airtable test row with an exact Reddit target.
4. Ensure its text is final and its target IDs/permalink are correct.
5. Set that row to `Status = Approved` and `Bot May Publish = true`.
6. Set GitHub secret `REDDIT_PUBLISH_ENABLED=true`.
7. Run the workflow manually once.
8. Confirm Airtable receives `Published At`, `Published URL`, `Existing Reddit URL`, `Bot May Publish=false`.

Only after this test should scheduled publishing remain enabled.

## Dedupe rules

Before every write the worker checks recent activity for the authenticated account:

- Reply: own comment whose `parent_id` equals the exact `t1_` target.
- Comment: any own comment whose `link_id` equals the exact `t3_` thread.
- Own Thread: same subreddit + same normalized title + same normalized body.

If a duplicate is found, nothing is posted. Airtable is repaired to `Published` and the existing permalink is stored.

## Timeout / ambiguous writes

If the UI submit throws or the network times out, the worker does **not** retry immediately. It verifies Reddit first. If the content exists, Airtable is marked Published. If the result is still ambiguous, the row becomes `Blocked`, `Bot May Publish=false`, and `Last Error=Publish status ambiguous`.

This also repairs the case where Reddit succeeded but the Airtable update failed: the next run finds the existing Reddit item during dedupe instead of posting a second copy.

## CAPTCHA / security challenge

The worker never attempts to bypass CAPTCHA, security checks, account verification, or unusual-activity challenges. It stops that action and records the blocker for human intervention.

## Local development

```bash
cp .env.example .env
npm install
npm run typecheck
npm test
npm run build
```

Do not point local development at a live Approved+publishable Airtable row unless you deliberately intend to test it. Keep `REDDIT_PUBLISH_ENABLED=false` during development.

## Scheduling

The GitHub workflow supports:

- manual `workflow_dispatch`
- cron every 10 minutes
- concurrency group `locenix-reddit-worker`, `cancel-in-progress: false`

A run processes at most 3 actions sequentially and waits 20–45 seconds between successful publications.
