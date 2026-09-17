const SECRET_KEYS = ['AIRTABLE_TOKEN', 'BROWSERBASE_API_KEY', 'authorization', 'cookie', 'session'];

function redact(value: unknown): unknown {
  if (typeof value === 'string') {
    let out = value;
    for (const key of SECRET_KEYS) {
      const secret = process.env[key] || process.env[key.toUpperCase()];
      if (secret && secret.length > 6) out = out.split(secret).join('[REDACTED]');
    }
    return out;
  }
  if (Array.isArray(value)) return value.map(redact);
  if (value && typeof value === 'object') {
    return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, /token|secret|cookie|authorization/i.test(k) ? '[REDACTED]' : redact(v)]));
  }
  return value;
}

export const logger = {
  info(message: string, meta?: unknown) {
    console.log(meta === undefined ? message : `${message} ${JSON.stringify(redact(meta))}`);
  },
  warn(message: string, meta?: unknown) {
    console.warn(meta === undefined ? message : `${message} ${JSON.stringify(redact(meta))}`);
  },
  error(message: string, meta?: unknown) {
    console.error(meta === undefined ? message : `${message} ${JSON.stringify(redact(meta))}`);
  }
};
