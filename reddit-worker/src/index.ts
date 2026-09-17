import { loadConfig } from './config.js';
import { AirtableClient } from './airtable.js';
import { GitHubBrowserClient } from './github-browser.js';
import { getLoggedInUsername } from './reddit-auth.js';
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
  const browser = new GitHubBrowserClient(config.redditStorageStateB64);
  const session = await browser.start();

  try {
    const username = await getLoggedInUsername(session.page);
    if (!username) {
      logger.warn('LOGIN_REQUIRED: Reddit session from REDDIT_STORAGE_STATE_B64 is no longer valid. Regenerate it locally and replace the GitHub secret. No Reddit action was attempted.');
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
