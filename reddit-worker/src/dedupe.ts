import type { Page } from 'playwright';
import type { ExistingRedditItem, QueueFields, RedditActivityChild } from './types.js';

export function normalizeRedditText(value: string | undefined): string {
  return (value || '').replace(/\r\n/g, '\n').trim();
}

export function findExistingFromActivity(
  fields: QueueFields,
  username: string,
  comments: RedditActivityChild[],
  posts: RedditActivityChild[]
): ExistingRedditItem {
  const actionType = fields['Action Type'];
  const targetId = fields['Target Reddit ID'];
  const threadId = fields['Thread Reddit ID'] || (targetId?.startsWith('t3_') ? targetId : undefined);
  const wantedText = normalizeRedditText(fields['Ready-to-Post Copy']);

  if (actionType === 'Reply' && targetId?.startsWith('t1_')) {
    const hit = comments.find((child) =>
      child.data.author?.toLowerCase() === username.toLowerCase() &&
      child.data.parent_id === targetId
    );
    if (hit?.data.permalink) return { found: true, kind: 'comment', permalink: `https://www.reddit.com${hit.data.permalink}` };
  }

  if (actionType === 'Comment' && threadId?.startsWith('t3_')) {
    const hit = comments.find((child) =>
      child.data.author?.toLowerCase() === username.toLowerCase() &&
      child.data.link_id === threadId
    );
    if (hit?.data.permalink) return { found: true, kind: 'comment', permalink: `https://www.reddit.com${hit.data.permalink}` };
  }

  if (actionType === 'Own Thread') {
    const wantedTitle = normalizeRedditText(fields['Draft Title']);
    const subreddit = fields['Target Subreddit']?.replace(/^r\//i, '').toLowerCase();
    const hit = posts.find((child) =>
      child.data.author?.toLowerCase() === username.toLowerCase() &&
      child.data.subreddit?.toLowerCase() === subreddit &&
      normalizeRedditText(child.data.title) === wantedTitle &&
      normalizeRedditText(child.data.selftext) === wantedText
    );
    if (hit?.data.permalink) return { found: true, kind: 'post', permalink: `https://www.reddit.com${hit.data.permalink}` };
  }

  return { found: false };
}

async function browserJson(page: Page, path: string): Promise<RedditActivityChild[]> {
  return page.evaluate(async (url: string) => {
    try {
      const response = await fetch(url, { credentials: 'include' });
      if (!response.ok) return [];
      const json = await response.json() as { data?: { children?: RedditActivityChild[] } };
      return json?.data?.children || [];
    } catch {
      return [];
    }
  }, path).catch(() => []);
}

export async function findExistingOnReddit(page: Page, fields: QueueFields, username: string): Promise<ExistingRedditItem> {
  const comments = await browserJson(page, `/user/${encodeURIComponent(username)}/comments.json?limit=100&raw_json=1`);
  const posts = fields['Action Type'] === 'Own Thread'
    ? await browserJson(page, `/user/${encodeURIComponent(username)}/submitted.json?limit=100&raw_json=1`)
    : [];
  return findExistingFromActivity(fields, username, comments, posts);
}
