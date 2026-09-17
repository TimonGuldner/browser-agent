import type { Page } from 'playwright';

export async function getLoggedInUsername(page: Page): Promise<string | null> {
  await page.goto('https://www.reddit.com/settings/profile', { waitUntil: 'domcontentloaded', timeout: 45_000 }).catch(() => undefined);
  await page.waitForTimeout(1500);

  if (/\/login\/?(?:\?|$)/i.test(page.url())) return null;

  const apiUsername = await page.evaluate(async () => {
    try {
      const response = await fetch('/api/me.json', { credentials: 'include' });
      if (!response.ok) return null;
      const data = await response.json() as { data?: { name?: string } };
      return data?.data?.name || null;
    } catch {
      return null;
    }
  }).catch(() => null);
  if (apiUsername) return apiUsername;

  const loginLink = page.getByRole('link', { name: /log in|anmelden/i }).first();
  if (await loginLink.isVisible().catch(() => false)) return null;

  const userHref = await page.locator('a[href^="/user/"]').first().getAttribute('href').catch(() => null);
  const match = userHref?.match(/^\/user\/([^/]+)/i);
  return match ? decodeURIComponent(match[1]) : null;
}

export async function detectHumanChallenge(page: Page): Promise<string | null> {
  const body = (await page.locator('body').innerText().catch(() => '')).toLowerCase();
  const signals: Array<[RegExp, string]> = [
    [/captcha|recaptcha/, 'Captcha detected'],
    [/security check|sicherheitsprüfung|sicherheitspruefung/, 'Security check detected'],
    [/verify (your )?account|konto bestätigen|konto bestaetigen/, 'Account verification required'],
    [/unusual activity|ungewöhnliche aktivität|ungewoehnliche aktivitaet/, 'Unusual activity challenge detected']
  ];
  for (const [pattern, message] of signals) if (pattern.test(body)) return message;
  return null;
}

export async function waitForManualLogin(page: Page, seconds: number): Promise<string | null> {
  const deadline = Date.now() + seconds * 1000;
  while (Date.now() < deadline) {
    const username = await getLoggedInUsername(page);
    if (username) return username;
    await page.waitForTimeout(5000);
  }
  return null;
}
