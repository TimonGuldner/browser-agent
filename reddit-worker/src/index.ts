import { loadConfig } from './config.js';
import { AirtableClient } from './airtable.js';
import { BrowserbaseClient } from './browserbase.js';
import { getLoggedInUsername, waitForManualLogin } from './reddit-auth.js';
import { processAction } from './publisher.js';
import { logger } from './logger.js';

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function randomPauseMs(): number {
  return 20_000 + Math.floor(Math.random() * 25_001);
}

async function main(): Promise<void> {
  const config = loadConfig();
  const airtable = new AirtableClient(config.airtableToken, config.airtableBaseId, config.airtableTableName);
  const browserbase = new BrowserbaseClient(config.browserbaseApiKey, config.browserbaseContextId, config.browserbaseProjectId);
  const session = await browserbase.start();

  try {
    let username = await getLoggedInUsername(session.page);
    if (!username) {
      logger.warn('LOGIN_REQUIRED');
      if (session.liveUrl) logger.warn(`Browserbase Live Session: ${session.liveUrl}`);
      if (config.githubEventName === 'workflow_dispatch') {
        logger.info(`Waiting up to ${config.loginWaitSeconds}s for manual Reddit login...`);
        username = await waitForManualLogin(session.page, config.loginWaitSeconds);
        if (username) {
          logger.info(`LOGIN_COMPLETE user=${username}`);
          return;
        }
      }
      logger.warn('Login was not completed. No Reddit action was attempted.');
      return;
    }

    if (config.expectedRedditUsername && username.toLowerCase() !== config.expectedRedditUsername.toLowerCase()) {
      logger.error('Wrong Reddit account logged in', { expected: config.expectedRedditUsername, actual: username });
      return;
    }

    const candidates = await airtable.listCandidates(config.maxActionsPerRun);
    logger.info(`Airtable candidates: ${candidates.length}`);
    if (!candidates.length) return;

    let processed = 0;
    let published = 0;
    let blocked = 0;
    let deduped = 0;

    for (const record of candidates) {
      const result = await processAction(record, {
        airtable,
        page: session.page,
        username,
        publishEnabled: config.publishEnabled
      });
      processed += 1;
      if (result.outcome === 'published') published += 1;
      if (result.outcome === 'blocked') blocked += 1;
      if (result.outcome === 'deduped') deduped += 1;
      logger.info(`Action ${record.id}: ${result.outcome}`, { reason: result.reason, permalink: result.permalink });

      if (result.outcome === 'published' && processed < candidates.length) {
        const pause = randomPauseMs();
        logger.info(`Rate-limit pause ${Math.round(pause / 1000)}s`);
        await sleep(pause);
      }
    }

    logger.info('Run complete', { processed, published, blocked, deduped });
  } finally {
    await session.close();
  }
}

main().catch((error) => {
  logger.error('Worker failed', { message: error instanceof Error ? error.message : String(error) });
  process.exitCode = 1;
});
