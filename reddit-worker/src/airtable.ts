import type { AirtableRecord, QueueFields } from './types.js';

type GenericRecord = AirtableRecord<Record<string, unknown>>;

export class AirtableClient {
  constructor(
    private readonly token: string,
    private readonly baseId: string,
    private readonly tableName: string
  ) {}

  private tableUrlFor(tableName: string): string {
    return `https://api.airtable.com/v0/${this.baseId}/${encodeURIComponent(tableName)}`;
  }

  private get tableUrl(): string {
    return this.tableUrlFor(this.tableName);
  }

  private async request<T>(url: string, init: RequestInit = {}): Promise<T> {
    const response = await fetch(url, {
      ...init,
      headers: {
        Authorization: `Bearer ${this.token}`,
        'Content-Type': 'application/json',
        ...(init.headers || {})
      }
    });
    if (!response.ok) {
      const text = await response.text();
      throw new Error(`Airtable ${response.status}: ${text.slice(0, 500)}`);
    }
    return response.json() as Promise<T>;
  }

  async listCandidates(limit = 3): Promise<AirtableRecord<QueueFields>[]> {
    const formula = `AND({Status}='Approved',{Bot May Publish}=TRUE(),{Ready-to-Post Copy}!='',{Published URL}='')`;
    const params = new URLSearchParams({
      filterByFormula: formula,
      pageSize: String(Math.min(limit, 100)),
      'sort[0][field]': 'Planned For',
      'sort[0][direction]': 'asc'
    });
    const data = await this.request<{ records: AirtableRecord<QueueFields>[] }>(`${this.tableUrl}?${params}`);
    return data.records.slice(0, limit);
  }

  async getRecord(id: string): Promise<AirtableRecord<QueueFields>> {
    return this.request<AirtableRecord<QueueFields>>(`${this.tableUrl}/${id}`);
  }

  async update(id: string, fields: Partial<QueueFields>): Promise<void> {
    await this.request(`${this.tableUrl}/${id}`, {
      method: 'PATCH',
      body: JSON.stringify({ fields })
    });
  }

  async block(id: string, error: string): Promise<void> {
    await this.update(id, {
      Status: 'Blocked',
      'Bot May Publish': false,
      'Last Error': error
    });
  }

  async markDeduped(id: string, kind: 'comment' | 'post', permalink: string): Promise<void> {
    const now = new Date().toISOString();
    await this.update(id, {
      Status: 'Published',
      'Bot May Publish': false,
      'Dedupe Status': kind === 'comment' ? 'Already Commented' : 'Already Posted',
      'Dedupe Checked At': now,
      'Existing Reddit URL': permalink,
      'Published URL': permalink,
      'Last Error': ''
    });
  }

  async markDedupeClear(id: string): Promise<void> {
    await this.update(id, {
      'Dedupe Status': 'Clear',
      'Dedupe Checked At': new Date().toISOString(),
      'Last Error': ''
    });
  }

  async markPublished(id: string, permalink: string): Promise<void> {
    const now = new Date().toISOString();
    await this.update(id, {
      Status: 'Published',
      'Bot May Publish': false,
      'Published At': now,
      'Published URL': permalink,
      'Existing Reddit URL': permalink,
      'Dedupe Status': 'Clear',
      'Last Error': ''
    });
  }

  async getSystemConfig(key: string): Promise<string | null> {
    const formula = `{Config Key}='${key.replace(/'/g, "\\'")}'`;
    const params = new URLSearchParams({ filterByFormula: formula, maxRecords: '1', pageSize: '1' });
    const data = await this.request<{ records: GenericRecord[] }>(`${this.tableUrlFor('System Config')}?${params}`);
    const value = data.records[0]?.fields?.Value;
    return typeof value === 'string' ? value : value == null ? null : String(value);
  }

  async setSystemConfig(key: string, value: string, notes?: string): Promise<void> {
    const formula = `{Config Key}='${key.replace(/'/g, "\\'")}'`;
    const params = new URLSearchParams({ filterByFormula: formula, maxRecords: '1', pageSize: '1' });
    const url = this.tableUrlFor('System Config');
    const data = await this.request<{ records: GenericRecord[] }>(`${url}?${params}`);
    const fields: Record<string, unknown> = { 'Config Key': key, Value: value };
    if (notes) fields.Notes = notes;
    const existing = data.records[0];
    if (existing) {
      await this.request(`${url}/${existing.id}`, { method: 'PATCH', body: JSON.stringify({ fields }) });
      return;
    }
    await this.request(url, { method: 'POST', body: JSON.stringify({ records: [{ fields }], typecast: true }) });
  }

  async setWorkerHeartbeat(status: string, loginStatus?: string): Promise<void> {
    const now = new Date().toISOString();
    await Promise.all([
      this.setSystemConfig('reddit_worker_last_run_at', now, 'Automatisch vom GitHub Reddit Worker aktualisiert.'),
      this.setSystemConfig('reddit_worker_status', status, 'Letzter technischer Zustand des Reddit Workers.'),
      loginStatus ? this.setSystemConfig('reddit_login_status', loginStatus, 'Status der gespeicherten Reddit-Session.') : Promise.resolve()
    ]);
  }

  async isPublishingPaused(): Promise<boolean> {
    const value = await this.getSystemConfig('reddit_publishing_paused');
    return (value || 'false').toLowerCase() === 'true';
  }
}
