import type { Page } from 'playwright-core';
import type { AirtableRecord, QueueFields, ActionExecutionResult } from './types.js';
import type { AirtableClient } from './airtable.js';
import { findExistingOnReddit } from './dedupe.js';
import { detectHumanChallenge } from './reddit-auth.js';
import { RedditUi } from './reddit.js';
import { verifyPublished } from './verifier.js';

export function validateAction(fields: QueueFields): string | null {
  if (fields.Status !== 'Approved') return 'Status is not Approved';
  if (fields['Bot May Publish'] !== true) return 'Bot May Publish is false';
  if (!fields['Ready-to-Post Copy']?.length) return 'Ready-to-Post Copy missing';
  if (fields['Published URL']) return 'Already has Published URL';

  const type = fields['Action Type'];
  if (type === 'Reply') {
    if (!fields['Target Permalink']) return 'Reply Target Permalink missing';
    if (!fields['Target Reddit ID']?.startsWith('t1_')) return 'Reply Target Reddit ID must start with t1_';
  } else if (type === 'Comment') {
    if (!(fields['Target Reddit ID']?.startsWith('t3_') || fields['Target Permalink'] || fields['Target URL'])) {
      return 'Comment target thread missing';
    }
  } else if (type === 'Own Thread') {
    if (!fields['Target Subreddit']) return 'Own Thread Target Subreddit missing';
    if (!fields['Draft Title']) return 'Own Thread Draft Title missing';
  } else {
    return 'Unsupported Action Type';
  }
  return null;
}

export interface ProcessActionDeps {
  airtable: AirtableClient;
  page: Page;
  username: string;
  publishEnabled: boolean;
}

export async function processAction(record: AirtableRecord<QueueFields>, deps: ProcessActionDeps): Promise<ActionExecutionResult> {
  const latest = await deps.airtable.getRecord(record.id);
  const invalid = validateAction(latest.fields);
  if (invalid) {
    if (latest.fields.Status === 'Approved' && latest.fields['Bot May Publish'] === true) {
      await deps.airtable.block(record.id, invalid);
      return { outcome: 'blocked', reason: invalid };
    }
    return { outcome: 'skipped', reason: invalid };
  }

  const ui = new RedditUi(deps.page);
  if (latest.fields['Action Type'] !== 'Own Thread') {
    await ui.openTarget(latest.fields);
    await ui.assertTarget(latest.fields);
  }

  const challenge = await detectHumanChallenge(deps.page);
  if (challenge) {
    await deps.airtable.block(record.id, challenge);
    return { outcome: 'blocked', reason: challenge };
  }

  const existing = await findExistingOnReddit(deps.page, latest.fields, deps.username);
  if (existing.found && existing.permalink && existing.kind) {
    await deps.airtable.markDeduped(record.id, existing.kind, existing.permalink);
    return { outcome: 'deduped', permalink: existing.permalink };
  }
  await deps.airtable.markDedupeClear(record.id);

  if (!deps.publishEnabled) {
    return { outcome: 'skipped', reason: 'REDDIT_PUBLISH_ENABLED is not true' };
  }

  try {
    await ui.publish(latest.fields);
  } catch (error) {
    const verified = await verifyPublished(deps.page, latest.fields, deps.username).catch(() => null);
    if (verified) {
      await deps.airtable.markPublished(record.id, verified);
      return { outcome: 'published', permalink: verified };
    }
    const reason = error instanceof Error ? error.message : 'Unknown publish error';
    await deps.airtable.block(record.id, `Publish status ambiguous: ${reason}`);
    return { outcome: 'blocked', reason: `Publish status ambiguous: ${reason}` };
  }

  const permalink = await verifyPublished(deps.page, latest.fields, deps.username);
  if (!permalink) {
    await deps.airtable.block(record.id, 'Publish status ambiguous');
    return { outcome: 'blocked', reason: 'Publish status ambiguous' };
  }

  await deps.airtable.markPublished(record.id, permalink);
  return { outcome: 'published', permalink };
}
