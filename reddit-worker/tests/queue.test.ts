import { describe, expect, it } from 'vitest';
import { validateAction } from '../src/publisher.js';
import type { QueueFields } from '../src/types.js';

const base: QueueFields = {
  Status: 'Approved',
  'Bot May Publish': true,
  'Action Type': 'Comment',
  'Ready-to-Post Copy': 'Hallo',
  'Target Reddit ID': 't3_abc'
};

describe('queue validation', () => {
  it('skips status not Approved', () => expect(validateAction({ ...base, Status: 'Ready' })).toBe('Status is not Approved'));
  it('skips Bot May Publish false', () => expect(validateAction({ ...base, 'Bot May Publish': false })).toBe('Bot May Publish is false'));
  it('blocks missing Ready-to-Post Copy', () => expect(validateAction({ ...base, 'Ready-to-Post Copy': '' })).toBe('Ready-to-Post Copy missing'));
  it('blocks Reply without t1 id', () => expect(validateAction({ ...base, 'Action Type': 'Reply', 'Target Permalink': 'https://reddit.com/x', 'Target Reddit ID': 't3_abc' })).toContain('t1_'));
  it('accepts Own Thread with title/subreddit', () => expect(validateAction({ ...base, 'Action Type': 'Own Thread', 'Target Reddit ID': undefined, 'Target Subreddit': 'Unternehmer', 'Draft Title': 'Titel' })).toBeNull());
});
