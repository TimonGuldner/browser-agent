import type { AirtableRecord, QueueFields } from './types.js';

export class AirtableClient {
  constructor(
    private readonly token: string,
    private readonly baseId: string,
    private readonly tableName: string
  ) {}

  private get tableUrl(): string {
    return `https://api.airtable.com/v0/${this.baseId}/${encodeURIComponent(this.tableName)}`;
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
}
