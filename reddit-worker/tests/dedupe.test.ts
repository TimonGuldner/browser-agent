import { describe, expect, it } from 'vitest';
import { findExistingFromActivity } from '../src/dedupe.js';
import type { QueueFields, RedditActivityChild } from '../src/types.js';

const comment = (data: RedditActivityChild['data']): RedditActivityChild => ({ kind: 't1', data });
const post = (data: RedditActivityChild['data']): RedditActivityChild => ({ kind: 't3', data });

describe('dedupe', () => {
  it('detects existing reply under exact parent', () => {
    const fields: QueueFields = { 'Action Type': 'Reply', 'Target Reddit ID': 't1_parent', 'Ready-to-Post Copy': 'x' };
    const result = findExistingFromActivity(fields, 'LOCENIX', [comment({ author: 'LOCENIX', parent_id: 't1_parent', permalink: '/r/x/comments/a/comment/b/' })], []);
    expect(result.found).toBe(true);
    expect(result.kind).toBe('comment');
  });

  it('detects any own comment in thread', () => {
    const fields: QueueFields = { 'Action Type': 'Comment', 'Target Reddit ID': 't3_thread', 'Ready-to-Post Copy': 'x' };
    const result = findExistingFromActivity(fields, 'LOCENIX', [comment({ author: 'locenix', link_id: 't3_thread', permalink: '/r/x/comments/thread/comment/c/' })], []);
    expect(result.found).toBe(true);
  });

  it('detects duplicate own thread by title/body/subreddit', () => {
    const fields: QueueFields = { 'Action Type': 'Own Thread', 'Target Subreddit': 'Unternehmer', 'Draft Title': 'Titel', 'Ready-to-Post Copy': 'Body' };
    const result = findExistingFromActivity(fields, 'LOCENIX', [], [post({ author: 'LOCENIX', subreddit: 'Unternehmer', title: 'Titel', selftext: 'Body', permalink: '/r/Unternehmer/comments/x/titel/' })]);
    expect(result.found).toBe(true);
    expect(result.kind).toBe('post');
  });

  it('does not flag unrelated content', () => {
    const fields: QueueFields = { 'Action Type': 'Reply', 'Target Reddit ID': 't1_target', 'Ready-to-Post Copy': 'x' };
    expect(findExistingFromActivity(fields, 'LOCENIX', [comment({ author: 'LOCENIX', parent_id: 't1_other' })], []).found).toBe(false);
  });
});
