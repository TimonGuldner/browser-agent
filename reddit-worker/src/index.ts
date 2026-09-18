import { appendFile } from 'node:fs/promises';
import { loadConfig } from './config.js';
import { AirtableClient } from './airtable.js';
import { GitHubBrowserClient } from './github-browser.js';
import { getLoggedInUsername } from './reddit-auth.js';
import { processAction } from './publisher.js';
import { logger } from './logger.js';

function sleep(ms: number): Promise<void> { return new Promise((resolve) => setTimeout(resolve, ms)); }
function randomPauseMs(): number { return 20_000 + Math.floor(Math.random() * 25_001); }

async function writeLiveViewSummary(sessionId: string, liveViewUrl: string): Promise<void> {
  const summaryPath = process.env.GITHUB_STEP_SUMMARY;
  if (!summaryPath) return;
  await appendFile(summaryPath, [
    '## Reddit Login – Browserbase Live View',
    '',
    `**[LIVE-BROWSER JETZT ÖFFNEN](${liveViewUrl})**`,
    '',
    `Session: \`${sessionId}\``,
    '',
    'Login-Modus: Es wird nichts veröffentlicht.',
    ''
  ].join('\n')).catch(() => undefined);
}

async function safeHeartbeat(airtable: AirtableClient, status: string, loginStatus?: string): Promise<void> {
  await airtable.setWorkerHeartbeat(status, loginStatus).catch((error) => {
    logger.warn('Worker heartbeat update failed', { message: error instanceof Error ? error.message : String(error) });
  });
}

async function main(): Promise<void> {
  const config = loadConfig();
  const airtable = new AirtableClient(config.airtableToken, config.airtableBaseId, config.airtableTableName);
  const browser = new GitHubBrowserClient(config.browserbaseApiKey, config.browserbaseContextId, config.browserbaseProjectId);
  const session = await browser.start();

  try {
    if (config.loginOnly) {
      await session.page.goto('https://www.reddit.com/', { waitUntil: 'domcontentloaded', timeout: 45_000 }).catch(() => undefined);
      await writeLiveViewSummary(session.sessionId, session.liveViewUrl);
      logger.info(`LIVE_VIEW_URL=${session.liveViewUrl}`);
      const initialUser = await getLoggedInUsername(session.page);
      if (initialUser) {
        await safeHeartbeat(airtable, 'login_ok', 'ok');
        logger.info(`LOGIN_COMPLETE user=${initialUser}`);
        return;
      }
      await safeHeartbeat(airtable, 'login_required', 'login_required');
      logger.warn(`LOGIN_REQUIRED_OPEN_THIS_LINK=${session.liveViewUrl}`);
      await sleep(config.loginWindowMinutes * 60 * 1000);
      const loggedIn = await getLoggedInUsername(session.page);
      await safeHeartbeat(airtable, loggedIn ? 'login_ok' : 'login_required', loggedIn ? 'ok' : 'login_required');
      logger.info(loggedIn ? `LOGIN_COMPLETE user=${loggedIn}` : 'LOGIN_TIMEOUT');
      return;
    }

    await session.page.goto('https://www.reddit.com/', { waitUntil: 'domcontentloaded', timeout: 45_000 }).catch(() => undefined);
    const username = await getLoggedInUsername(session.page);
    if (!username) {
      await safeHeartbeat(airtable, 'login_required', 'login_required');
      logger.warn('LOGIN_REQUIRED: persistent Browserbase context is not logged in. Worker exits immediately.');
      return;
    }

    if (config.expectedRedditUsername && username.toLowerCase() !== config.expectedRedditUsername.toLowerCase()) {
      await safeHeartbeat(airtable, 'wrong_account', `wrong_account:${username}`);
      logger.error('Wrong Reddit account logged in', { expected: config.expectedRedditUsername, actual: username });
      return;
    }

    let publishingPaused: boolean;
    try { publishingPaused = await airtable.isPublishingPaused(); }
    catch (error) {
      await safeHeartbeat(airtable, 'control_plane_error', 'ok');
      logger.error('Could not read reddit_publishing_paused. Failing closed: no Reddit action attempted.', { message: error instanceof Error ? error.message : String(error) });
      return;
    }

    if (publishingPaused && config.publishEnabled) {
      await safeHeartbeat(airtable, 'paused', 'ok');
      logger.info('Reddit publishing is paused by LOCENIX admin control plane. No Reddit action was attempted.');
      return;
    }

    if (publishingPaused && !config.publishEnabled) {
      logger.info('DRY_RUN: publishing is paused, but safe target/dedupe validation will continue. Publishing remains disabled.');
    }

    await safeHeartbeat(airtable, config.publishEnabled ? 'running' : 'dry_run', 'ok');
    const candidates = await airtable.listCandidates(config.maxActionsPerRun);
    logger.info(`Airtable candidates: ${candidates.length}`);
    if (!candidates.length) { await safeHeartbeat(airtable, 'idle', 'ok'); return; }

    let processed = 0, published = 0, blocked = 0, deduped = 0;
    for (const record of candidates) {
      const result = await processAction(record, { airtable, page: session.page, username, publishEnabled: config.publishEnabled });
      processed += 1;
      if (result.outcome === 'published') published += 1;
      if (result.outcome === 'blocked') blocked += 1;
      if (result.outcome === 'deduped') deduped += 1;
      logger.info(`Action ${record.id}: ${result.outcome}`, { reason: result.reason, permalink: result.permalink });
      if (result.outcome === 'published' && processed < candidates.length) await sleep(randomPauseMs());
    }
    await safeHeartbeat(airtable, blocked > 0 ? 'completed_with_blockers' : 'ok', 'ok');
    logger.info('Run complete', { processed, published, blocked, deduped });
  } finally {
    await session.close();
  }
}

main().catch((error) => {
  logger.error('Worker failed', { message: error instanceof Error ? error.message : String(error) });
  process.exitCode = 1;
});
