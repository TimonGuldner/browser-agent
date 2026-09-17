import Browserbase from '@browserbasehq/sdk';
import { chromium, type Browser, type Page } from 'playwright-core';

export interface BrowserbaseSessionHandle {
  browser: Browser;
  page: Page;
  sessionId: string;
  liveUrl?: string;
  close(): Promise<void>;
}

export class BrowserbaseClient {
  private readonly bb: Browserbase;

  constructor(
    apiKey: string,
    private readonly contextId: string,
    private readonly projectId?: string
  ) {
    this.bb = new Browserbase({ apiKey });
  }

  async start(): Promise<BrowserbaseSessionHandle> {
    const createInput: Record<string, unknown> = {
      browserSettings: {
        context: {
          id: this.contextId,
          persist: true
        }
      }
    };
    if (this.projectId) createInput.projectId = this.projectId;

    const session = await (this.bb.sessions.create as (input?: unknown) => Promise<{ id: string; connectUrl: string }>)(createInput);
    const browser = await chromium.connectOverCDP(session.connectUrl);
    const context = browser.contexts()[0] ?? (await browser.newContext());
    const page = context.pages()[0] ?? (await context.newPage());
    const liveUrl = await this.getLiveUrl(session.id).catch(() => undefined);

    return {
      browser,
      page,
      sessionId: session.id,
      liveUrl,
      close: async () => {
        await browser.close().catch(() => undefined);
      }
    };
  }

  private async getLiveUrl(sessionId: string): Promise<string | undefined> {
    const sessions = this.bb.sessions as unknown as { debug?: (id: string) => Promise<Record<string, unknown>> };
    if (!sessions.debug) return undefined;
    const debug = await sessions.debug(sessionId);
    for (const key of ['debuggerFullscreenUrl', 'debuggerUrl', 'liveUrl']) {
      const value = debug[key];
      if (typeof value === 'string' && value.startsWith('http')) return value;
    }
    return undefined;
  }
}
