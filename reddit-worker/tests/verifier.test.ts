import { describe, expect, it } from 'vitest';
import { normalizeRedditText } from '../src/dedupe.js';

describe('verification helpers', () => {
  it('normalizes only line endings/outer whitespace', () => {
    expect(normalizeRedditText('  a\r\n\r\nb  ')).toBe('a\n\nb');
  });
  it('preserves internal markdown and URLs', () => {
    const text = '**LOCENIX**\nhttps://www.locenix.com/tools/local-seo-check';
    expect(normalizeRedditText(text)).toBe(text);
  });
});
