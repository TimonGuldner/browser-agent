import type { Page } from 'playwright';
import type { QueueFields } from './types.js';
import { findExistingOnReddit } from './dedupe.js';

export async function verifyPublished(page: Page, fields: QueueFields, username: string, timeoutMs = 45_000): Promise<string | null> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const existing = await findExistingOnReddit(page, fields, username);
    if (existing.found && existing.permalink) return existing.permalink;
    await page.waitForTimeout(3000);
  }
  return null;
}
