import { randomUUID } from 'node:crypto';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { rm, writeFile } from 'node:fs/promises';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright';

export interface GitHubBrowserSessionHandle {
  browser: Browser;
  context: BrowserContext;
  page: Page;
  close(): Promise<void>;
}

export class GitHubBrowserClient {
  constructor(private readonly storageStateB64: string) {}

  async start(): Promise<GitHubBrowserSessionHandle> {
    const statePath = join(tmpdir(), `locenix-reddit-state-${randomUUID()}.json`);
    const decoded = Buffer.from(this.storageStateB64, 'base64').toString('utf8');

    try {
      JSON.parse(decoded);
    } catch {
      throw new Error('REDDIT_STORAGE_STATE_B64 is not valid base64-encoded Playwright storage state JSON');
    }

    await writeFile(statePath, decoded, { encoding: 'utf8', mode: 0o600 });

    const browser = await chromium.launch({
      headless: true,
      args: ['--disable-dev-shm-usage']
    });

    try {
      const context = await browser.newContext({
        storageState: statePath,
        viewport: { width: 1440, height: 1000 },
        locale: 'de-DE',
        timezoneId: 'Europe/Berlin'
      });
      const page = await context.newPage();

      return {
        browser,
        context,
        page,
        close: async () => {
          await context.close().catch(() => undefined);
          await browser.close().catch(() => undefined);
          await rm(statePath, { force: true }).catch(() => undefined);
        }
      };
    } catch (error) {
      await browser.close().catch(() => undefined);
      await rm(statePath, { force: true }).catch(() => undefined);
      throw error;
    }
  }
}
