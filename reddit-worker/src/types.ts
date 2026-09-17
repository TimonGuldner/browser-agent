export type ActionType = 'Reply' | 'Comment' | 'Own Thread';
export type ActionStatus = 'Draft' | 'Ready' | 'Approved' | 'Published' | 'Blocked' | 'Rejected';
export type DedupeStatus = 'Not Checked' | 'Clear' | 'Already Commented' | 'Already Posted' | 'Ambiguous' | 'Error';

export interface AirtableRecord<T> {
  id: string;
  createdTime?: string;
  fields: T;
}

export interface QueueFields {
  Action?: string;
  'Action Type'?: ActionType;
  'Target URL'?: string;
  'Target Permalink'?: string;
  'Target Reddit ID'?: string;
  'Thread Reddit ID'?: string;
  'Ready-to-Post Copy'?: string;
  'Draft Title'?: string;
  Status?: ActionStatus;
  'Bot May Publish'?: boolean;
  'Dedupe Status'?: DedupeStatus;
  'Dedupe Checked At'?: string;
  'Existing Reddit URL'?: string;
  'Published At'?: string;
  'Published URL'?: string;
  'Last Error'?: string;
  'Target Subreddit'?: string;
}

export interface ExistingRedditItem {
  found: boolean;
  permalink?: string;
  kind?: 'comment' | 'post';
}

export interface RedditActivityChild {
  kind: string;
  data: {
    id?: string;
    name?: string;
    author?: string;
    body?: string;
    selftext?: string;
    title?: string;
    permalink?: string;
    parent_id?: string;
    link_id?: string;
    subreddit?: string;
  };
}

export interface PublishResult {
  submitted: boolean;
  permalink?: string;
}

export interface ActionExecutionResult {
  outcome: 'published' | 'deduped' | 'blocked' | 'skipped';
  permalink?: string;
  reason?: string;
}
