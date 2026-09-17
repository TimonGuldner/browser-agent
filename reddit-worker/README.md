# LOCENIX Reddit Posting Worker

A small, non-LLM worker that treats Airtable as the source of truth, uses Playwright + a persistent local Chromium profile, checks for duplicates before every write, publishes only pre-approved `Ready-to-Post Copy`, verifies the result, and writes the permalink/status back to Airtable.

## Safety model

The worker only considers an Airtable row when all of these are true:

- `Status = Approved`
- `Bot May Publish = true`
- `Ready-to-Post Copy` is not empty
- `Published URL` is empty

Publishing is additionally disabled unless GitHub secret `REDDIT_PUBLISH_ENABLED` is exactly `true`.

For a `Reply`, `Target Permalink` must be present and `Target Reddit ID` must start with `t1_`. For a thread `Comment`, a `t3_` target or an unambiguous thread URL is required. An `Own Thread` needs `Target Subreddit` and `Draft Title`.

## Architecture

GitHub Actions → self-hosted Windows runner → Playwright → persistent Chromium profile → Reddit → Airtable.

There is no Browserbase account and no LLM in the worker. `Ready-to-Post Copy` is never rewritten.

## 1. Self-hosted GitHub runner on Windows

Use a Windows machine/VPS that stays online.

1. In the repository open **Settings → Actions → Runners → New self-hosted runner**.
2. Choose **Windows / x64**.
3. Run the GitHub setup commands on the Windows machine.
4. For the first Reddit login, run the runner interactively under the Windows user that will keep using the Chromium profile.
5. After login, either keep that runner running interactively or install/run the runner service under the SAME Windows account. Do not switch the worker to another Windows account, because Chromium's stored login state belongs to that profile/user context.

The workflow expects the standard labels:

- `self-hosted`
- `Windows`
- `X64`

## 2. Persistent Chromium profile

The workflow stores Reddit's browser state here:

`C:\locenix\reddit-profile`

Playwright launches Chromium with `launchPersistentContext`, so cookies and the Reddit login survive future workflow runs on the same self-hosted machine.

Do not delete this folder unless you deliberately want to reset the Reddit login.

## 3. GitHub Secrets

Repository → **Settings → Secrets and variables → Actions**.

Required:

- `AIRTABLE_TOKEN`

Recommended:

- `EXPECTED_REDDIT_USERNAME` — exact Reddit username, preventing accidental posting from the wrong account

Safety switch:

- `REDDIT_PUBLISH_ENABLED=false` initially

No Browserbase credentials are required.

## 4. Airtable

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

## 5. One-time Reddit login

Keep `REDDIT_PUBLISH_ENABLED=false`.

1. Connect to the Windows machine/VPS with its desktop visible.
2. Make sure the self-hosted GitHub runner is running interactively under the intended Windows account.
3. In GitHub open **Actions → LOCENIX Reddit Worker → Run workflow**.
4. Choose `mode = login`.
5. The workflow starts Chromium visibly on the Windows desktop using `C:\locenix\reddit-profile`.
6. Log in to Reddit manually.
7. The worker detects the account, prints `LOGIN_COMPLETE`, saves the browser state by closing the persistent profile normally, and exits WITHOUT posting.

Later scheduled runs use the same profile headlessly. If Reddit logs the account out in the future, repeat `mode = login`.

## 6. Safe first test

1. Confirm `EXPECTED_REDDIT_USERNAME` is set.
2. Keep `REDDIT_PUBLISH_ENABLED=false` and run `mode = worker`. It may read/dedupe, but it cannot submit new content.
3. Pick exactly one Airtable row with a verified target/permalink.
4. Confirm `Ready-to-Post Copy` is final.
5. Set that row to `Status = Approved` and `Bot May Publish = true`.
6. Set `REDDIT_PUBLISH_ENABLED=true`.
7. Run `mode = worker` once manually.
8. Confirm Airtable receives `Published At`, `Published URL`, `Existing Reddit URL`, and `Bot May Publish=false`.

Only after this test should automatic scheduled publishing remain enabled.

## Dedupe rules

Before every write the worker checks recent activity for the authenticated account:

- Reply: own comment whose `parent_id` equals the exact `t1_` target.
- Comment: any own comment whose `link_id` equals the exact `t3_` thread.
- Own Thread: same subreddit + same normalized title + same normalized body.

If a duplicate is found, nothing is posted. Airtable is repaired to `Published` and the existing permalink is stored.

## Timeout / ambiguous writes

If the UI submit throws or the network times out, the worker does not retry immediately. It verifies Reddit first. If the content exists, Airtable is marked Published. If the result is still ambiguous, the row becomes `Blocked`, `Bot May Publish=false`, and `Last Error=Publish status ambiguous`.

## CAPTCHA / security challenge

The worker never attempts to bypass CAPTCHA, security checks, account verification, or unusual-activity challenges. It stops that action and records the blocker for human intervention.

## Local commands

From `reddit-worker`:

```powershell
npm install
npx playwright install chromium
npm run typecheck
npm test
npm run build
```

For a manual local login test, configure `.env` from `.env.example`, set `REDDIT_LOGIN_MODE=true`, `REDDIT_HEADLESS=false`, and run `npm run dev`.

## Scheduling

The workflow supports:

- manual `workflow_dispatch` with `mode=worker` or `mode=login`
- cron every 10 minutes
- concurrency group `locenix-reddit-worker`, `cancel-in-progress: false`

A normal run processes at most 3 actions sequentially and waits 20–45 seconds between successful publications.
