function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
  return value;
}

function integer(name: string, fallback: number, min: number, max: number): number {
  const raw = process.env[name];
  if (!raw) return fallback;
  const value = Number.parseInt(raw, 10);
  if (!Number.isFinite(value) || value < min || value > max) {
    throw new Error(`${name} must be an integer between ${min} and ${max}`);
  }
  return value;
}

export interface Config {
  browserbaseApiKey: string;
  browserbaseProjectId?: string;
  browserbaseContextId: string;
  airtableToken: string;
  airtableBaseId: string;
  airtableTableName: string;
  maxActionsPerRun: number;
  expectedRedditUsername?: string;
  publishEnabled: boolean;
  loginWaitSeconds: number;
  githubEventName?: string;
}

export function loadConfig(): Config {
  return {
    browserbaseApiKey: required('BROWSERBASE_API_KEY'),
    browserbaseProjectId: process.env.BROWSERBASE_PROJECT_ID?.trim() || undefined,
    browserbaseContextId: required('BROWSERBASE_CONTEXT_ID'),
    airtableToken: required('AIRTABLE_TOKEN'),
    airtableBaseId: process.env.AIRTABLE_BASE_ID?.trim() || 'appEpBPsuKXOFLxQD',
    airtableTableName: process.env.AIRTABLE_TABLE_NAME?.trim() || 'Action Queue',
    maxActionsPerRun: integer('MAX_ACTIONS_PER_RUN', 3, 1, 3),
    expectedRedditUsername: process.env.EXPECTED_REDDIT_USERNAME?.trim() || undefined,
    publishEnabled: (process.env.REDDIT_PUBLISH_ENABLED || '').toLowerCase() === 'true',
    loginWaitSeconds: integer('LOGIN_WAIT_SECONDS', 600, 30, 1800),
    githubEventName: process.env.GITHUB_EVENT_NAME
  };
}
