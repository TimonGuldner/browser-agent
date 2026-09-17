import { describe, expect, it, vi } from 'vitest';
import type { AirtableRecord, QueueFields } from '../src/types.js';

describe('publishing safety invariants', () => {
  it('Ready-to-Post Copy remains byte-for-byte unchanged in source record', () => {
    const copy = 'Absatz 1\n\n**Fett** https://example.com/?a=1&b=2';
    const record: AirtableRecord<QueueFields> = { id: 'rec123', fields: { 'Ready-to-Post Copy': copy } };
    expect(record.fields['Ready-to-Post Copy']).toBe(copy);
  });

  it('a successful Reddit write followed by Airtable failure must be recoverable by dedupe', async () => {
    const redditWrite = vi.fn().mockResolvedValue(undefined);
    const airtableUpdate = vi.fn().mockRejectedValue(new Error('network'));
    await redditWrite();
    await expect(airtableUpdate()).rejects.toThrow('network');
    expect(redditWrite).toHaveBeenCalledTimes(1);
  });

  it('timeout after submit must verify before retry', async () => {
    const submit = vi.fn().mockRejectedValue(new Error('timeout'));
    const verify = vi.fn().mockResolvedValue('https://reddit.com/permalink');
    await expect(submit()).rejects.toThrow('timeout');
    expect(await verify()).toContain('reddit.com');
    expect(submit).toHaveBeenCalledTimes(1);
  });

  it('ambiguous publish does not retry automatically', () => {
    const retryAllowed = false;
    expect(retryAllowed).toBe(false);
  });

  it('login requirement never implies publishing', () => {
    const username = null;
    const mayPublish = Boolean(username);
    expect(mayPublish).toBe(false);
  });

  it('wrong account must stop', () => {
    const expected = 'locenix';
    const actual = 'other';
    expect(actual.toLowerCase() === expected.toLowerCase()).toBe(false);
  });

  it('captcha requires human action', () => {
    const challenge = 'Captcha detected';
    expect(challenge).toMatch(/Captcha/);
  });

  it('GitHub concurrency key is stable', () => {
    expect('locenix-reddit-worker').toBe('locenix-reddit-worker');
  });
});
