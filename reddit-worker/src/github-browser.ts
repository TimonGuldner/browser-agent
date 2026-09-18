import Browserbase from '@browserbasehq/sdk';
import { chromium, type Browser, type BrowserContext, type Page } from 'playwright-core';

export interface GitHubBrowserSessionHandle {
  browser: Browser;
  context: BrowserContext;
  page: Page;
  sessionId: string;
  liveViewUrl: string;
  close(): Promise<void>;
}

export class GitHubBrowserClient {
  constructor(
    private readonly apiKey: string,
    private readonly contextId: string,
    private readonly projectId?: string
  ) {}

  async start(): Promise<GitHubBrowserSessionHandle> {
    const bb = new Browserbase({ apiKey: this.apiKey });
    const session = await bb.sessions.create({
      ...(this.projectId ? { projectId: this.projectId } : {}),
      browserSettings: {
        context: { id: this.contextId, persist: true }
      }
    });

    const browser = await chromium.connectOverCDP(session.connectUrl);
    const context = browser.contexts()[0];
    if (!context) throw new Error('Browserbase session did not expose a browser context');
    const page = context.pages()[0] || await context.newPage();
    const debug = await bb.sessions.debug(session.id);

    return {
      browser,
      context,
      page,
      sessionId: session.id,
      liveViewUrl: debug.debuggerFullscreenUrl || debug.debuggerUrl,
      close: async () => {
        await browser.close().catch(() => undefined);
      }
    };
  }
}
