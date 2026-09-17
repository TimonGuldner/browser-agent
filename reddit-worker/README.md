# LOCENIX Reddit Posting Worker

A small, non-LLM worker that treats Airtable as the source of truth, runs Playwright + Chromium on GitHub-hosted Actions, checks for duplicates before every write, publishes only pre-approved `Ready-to-Post Copy`, verifies the result, and writes the permalink/status back to Airtable.

## Safety model

The worker only considers an Airtable row when all of these are true:

- `Status = Approved`
- `Bot May Publish = true`
- `Ready-to-Post Copy` is not empty
- `Published URL` is empty

Publishing is additionally disabled unless GitHub secret `REDDIT_PUBLISH_ENABLED` is exactly `true`.

For a `Reply`, `Target Permalink` must be present and `Target Reddit ID` must start with `t1_`. For a thread `Comment`, a `t3_` target or an unambiguous thread URL is required. An `Own Thread` needs `Target Subreddit` and `Draft Title`.

## Architecture

Airtable → GitHub-hosted Actions → Playwright/Chromium → Reddit → verification → Airtable.

No VPS, no self-hosted runner, no Browserbase and no LLM are required. `Ready-to-Post Copy` is never rewritten.

## 1. GitHub Secrets

Repository → **Settings → Secrets and variables → Actions**.

Required:

- `AIRTABLE_TOKEN`
- `REDDIT_STORAGE_STATE_B64`

Recommended:

- `EXPECTED_REDDIT_USERNAME` — exact Reddit username, preventing accidental posting from the wrong account

Safety switch:

- `REDDIT_PUBLISH_ENABLED=false` initially

`REDDIT_STORAGE_STATE_B64` is a base64 representation of Playwright storage state. Base64 itself is NOT encryption; the value is protected because it is stored as a GitHub Secret. Never commit or share the generated state files.

## 2. Create the Reddit login state once on your own computer

From the `reddit-worker` folder on your own Windows/macOS/Linux computer:

```bash
npm install
npx playwright install chromium
npm run login:reddit
```

A visible Chromium window opens.

1. Log in to Reddit manually.
2. Return to the terminal and press ENTER only after Reddit is fully logged in.
3. The helper verifies the logged-in Reddit account.
4. It writes two ignored local files:
   - `.secrets/reddit-storage-state.json`
   - `.secrets/REDDIT_STORAGE_STATE_B64.txt`
5. Open the TXT file locally and copy its full content into GitHub Secret `REDDIT_STORAGE_STATE_B64`.

The Reddit password is never stored in the repository or GitHub. The storage state contains authenticated browser cookies/session state and must therefore be treated like a password.

If Reddit later logs the account out, run `npm run login:reddit` again and replace the GitHub Secret with the newly generated value.

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

## 4. How each GitHub run works

The workflow runs on `ubuntu-latest`.

1. Reads `REDDIT_STORAGE_STATE_B64` from GitHub Secrets.
2. Decodes it only into a temporary file on the ephemeral runner.
3. Starts Playwright Chromium headlessly with that storage state.
4. Confirms which Reddit account is logged in.
5. If login is missing, logs `LOGIN_REQUIRED` and performs no Reddit action.
6. Reads approved Airtable actions.
7. Runs duplicate checks before any submit.
8. Publishes only when `REDDIT_PUBLISH_ENABLED=true`.
9. Verifies the published permalink.
10. Updates Airtable and disables `Bot May Publish` for completed actions.
11. Deletes the temporary storage-state file when the browser session closes.

The GitHub runner itself is disposable; the login survives because the reusable storage state is supplied from the GitHub Secret on every run.

## 5. Safe first test

1. Set `AIRTABLE_TOKEN`.
2. Generate and set `REDDIT_STORAGE_STATE_B64`.
3. Set `EXPECTED_REDDIT_USERNAME`.
4. Keep `REDDIT_PUBLISH_ENABLED=false`.
5. Run **Actions → LOCENIX Reddit Worker → Run workflow** manually once.
6. Confirm the workflow recognizes the expected Reddit account and exits without publishing new content.
7. Pick exactly one Airtable row with a verified target/permalink.
8. Confirm `Ready-to-Post Copy` is final.
9. Set that row to `Status = Approved` and `Bot May Publish = true`.
10. Set `REDDIT_PUBLISH_ENABLED=true`.
11. Run the workflow manually once.
12. Confirm Airtable receives `Published At`, `Published URL`, `Existing Reddit URL`, and `Bot May Publish=false`.

Only after this test should scheduled publishing remain enabled.

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

## Local validation

From `reddit-worker`:

```bash
npm install
npm run typecheck
npm test
npm run build
```

## Scheduling

The workflow supports:

- manual `workflow_dispatch`
- cron every 10 minutes
- concurrency group `locenix-reddit-worker`, `cancel-in-progress: false`

A normal run processes at most 3 actions sequentially and waits 20–45 seconds between successful publications.
