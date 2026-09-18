import type { Locator, Page } from 'playwright-core';
import type { QueueFields } from './types.js';
import { normalizeRedditText } from './dedupe.js';

async function firstVisible(locators: Locator[]): Promise<Locator | null> {
  for (const locator of locators) {
    if (await locator.first().isVisible().catch(() => false)) return locator.first();
  }
  return null;
}

async function fillExact(editor: Locator, text: string): Promise<void> {
  await editor.fill(text);
  const actual = await editor.inputValue().catch(async () => editor.evaluate((el: Element) => (el as HTMLElement).innerText || el.textContent || ''));
  if (normalizeRedditText(actual) !== normalizeRedditText(text)) {
    throw new Error('Composer text does not match Ready-to-Post Copy');
  }
}

export class RedditUi {
  constructor(private readonly page: Page) {}

  async openTarget(fields: QueueFields): Promise<void> {
    const target = fields['Target Permalink'] || fields['Target URL'];
    if (!target) throw new Error('Target URL missing');
    await this.page.goto(target, { waitUntil: 'domcontentloaded', timeout: 45_000 });
    await this.page.waitForTimeout(1200);
  }

  async assertTarget(fields: QueueFields): Promise<void> {
    const targetId = fields['Target Reddit ID'];
    if (!targetId) return;
    const shortId = targetId.replace(/^t[13]_/, '');
    if (fields['Action Type'] === 'Reply' && !this.page.url().includes(`/comment/${shortId}`)) {
      const scoped = this.page.locator(`shreddit-comment[thingid="${targetId}"], shreddit-comment[id*="${shortId}"]`);
      if (!(await scoped.first().isVisible().catch(() => false))) throw new Error(`Target Reddit ID mismatch: ${targetId}`);
    }
    if (targetId.startsWith('t3_') && !this.page.url().includes(shortId)) {
      throw new Error(`Thread Reddit ID mismatch: ${targetId}`);
    }
  }

  async publish(fields: QueueFields): Promise<void> {
    if (fields['Action Type'] === 'Reply') return this.reply(fields);
    if (fields['Action Type'] === 'Comment') return this.comment(fields);
    if (fields['Action Type'] === 'Own Thread') return this.ownThread(fields);
    throw new Error(`Unsupported Action Type: ${fields['Action Type']}`);
  }

  private async reply(fields: QueueFields): Promise<void> {
    const targetId = fields['Target Reddit ID']!;
    const copy = fields['Ready-to-Post Copy']!;
    const shortId = targetId.replace(/^t1_/, '');
    const comment = this.page.locator(`shreddit-comment[thingid="${targetId}"], shreddit-comment[id*="${shortId}"]`).first();
    const scope = (await comment.isVisible().catch(() => false)) ? comment : this.page.locator('main');
    const reply = await firstVisible([
      scope.getByRole('button', { name: /^reply$/i }),
      scope.getByRole('button', { name: /^antworten$/i }),
      scope.getByText(/^reply$/i),
      scope.getByText(/^antworten$/i)
    ]);
    if (!reply) throw new Error('Reply button not found for target comment');
    await reply.click();
    const editor = await this.findComposer(scope);
    if (!editor) throw new Error('Reply composer not found');
    await fillExact(editor, copy);
    const submit = await firstVisible([
      scope.getByRole('button', { name: /^reply$/i }),
      scope.getByRole('button', { name: /^antworten$/i }),
      scope.getByRole('button', { name: /^comment$/i }),
      scope.getByRole('button', { name: /^kommentieren$/i })
    ]);
    if (!submit) throw new Error('Reply submit button not found');
    await submit.click({ timeout: 15_000 });
  }

  private async comment(fields: QueueFields): Promise<void> {
    const copy = fields['Ready-to-Post Copy']!;
    const trigger = await firstVisible([
      this.page.getByText(/add a comment|kommentar hinzufügen|kommentar hinzufugen/i),
      this.page.getByRole('textbox').first()
    ]);
    if (trigger && !(await trigger.getAttribute('contenteditable').catch(() => null))) await trigger.click().catch(() => undefined);
    const editor = await this.findComposer(this.page.locator('main'));
    if (!editor) throw new Error('Comment composer not found');
    await fillExact(editor, copy);
    const submit = await firstVisible([
      this.page.getByRole('button', { name: /^comment$/i }),
      this.page.getByRole('button', { name: /^kommentieren$/i }),
      this.page.getByRole('button', { name: /^post$/i }),
      this.page.getByRole('button', { name: /^senden$/i })
    ]);
    if (!submit) throw new Error('Comment submit button not found');
    await submit.click({ timeout: 15_000 });
  }

  private async ownThread(fields: QueueFields): Promise<void> {
    const subreddit = fields['Target Subreddit']!.replace(/^r\//i, '');
    await this.page.goto(`https://www.reddit.com/r/${encodeURIComponent(subreddit)}/submit/?type=TEXT`, { waitUntil: 'domcontentloaded', timeout: 45_000 });
    await this.page.waitForTimeout(1200);

    const title = await firstVisible([
      this.page.getByRole('textbox', { name: /title|titel/i }),
      this.page.locator('input[name="title"]')
    ]);
    if (!title) throw new Error('Post title editor not found');
    await title.fill(fields['Draft Title']!);

    const body = await this.findComposer(this.page.locator('main'), true);
    if (!body) throw new Error('Post body editor not found');
    await fillExact(body, fields['Ready-to-Post Copy']!);

    const submit = await firstVisible([
      this.page.getByRole('button', { name: /^post$/i }),
      this.page.getByRole('button', { name: /^senden$/i })
    ]);
    if (!submit) throw new Error('Post submit button not found');
    await submit.click({ timeout: 15_000 });
  }

  private async findComposer(scope: Locator, preferBody = false): Promise<Locator | null> {
    const locators = preferBody ? [
      scope.locator('textarea[name="body"]'),
      scope.locator('[contenteditable="true"][role="textbox"]'),
      scope.locator('textarea'),
      scope.getByRole('textbox').last()
    ] : [
      scope.locator('textarea[placeholder*="comment" i]'),
      scope.locator('[contenteditable="true"][role="textbox"]'),
      scope.locator('textarea'),
      scope.getByRole('textbox').last()
    ];
    return firstVisible(locators);
  }
}
