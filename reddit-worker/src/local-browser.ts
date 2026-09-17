import { mkdir } from 'node:fs/promises';
import { chromium, type BrowserContext, type Page } from 'playwright';

export interface LocalBrowserSessionHandle {
  context: BrowserContext;
  page: Page;
  close(): Promise<void>;
}

export class LocalBrowserClient {
  constructor(
    private readonly profileDir: string,
    private readonly headless: boolean
  ) {}

  async start(): Promise<LocalBrowserSessionHandle> {
    await mkdir(this.profileDir, { recursive: true });

    const context = await chromium.launchPersistentContext(this.profileDir, {
      headless: this.headless,
      viewport: { width: 1440, height: 1000 },
      locale: 'de-DE',
      timezoneId: 'Europe/Berlin',
      args: ['--disable-dev-shm-usage']
    });

    const page = context.pages()[0] ?? (await context.newPage());

    return {
      context,
      page,
      close: async () => {
        await context.close().catch(() => undefined);
      }
    };
  }
}
