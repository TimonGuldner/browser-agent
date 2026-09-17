import { mkdir, writeFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { createInterface } from 'node:readline/promises';
import { stdin as input, stdout as output } from 'node:process';
import { chromium } from 'playwright';
import { getLoggedInUsername } from './reddit-auth.js';

async function main(): Promise<void> {
  const outDir = resolve('.secrets');
  const jsonPath = resolve(outDir, 'reddit-storage-state.json');
  const b64Path = resolve(outDir, 'REDDIT_STORAGE_STATE_B64.txt');

  await mkdir(outDir, { recursive: true });

  const browser = await chromium.launch({ headless: false });
  const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: 'de-DE',
    timezoneId: 'Europe/Berlin'
  });
  const page = await context.newPage();
  const rl = createInterface({ input, output });

  try {
    await page.goto('https://www.reddit.com/login/', { waitUntil: 'domcontentloaded', timeout: 45_000 });
    output.write('\nLogge dich im geöffneten Chromium-Fenster manuell bei Reddit ein.\n');
    await rl.question('Wenn du vollständig eingeloggt bist, hier ENTER drücken: ');

    const username = await getLoggedInUsername(page);
    if (!username) {
      throw new Error('Reddit-Login konnte nicht bestätigt werden. Bitte erneut ausführen und erst nach vollständigem Login ENTER drücken.');
    }

    const state = await context.storageState();
    const json = JSON.stringify(state);
    const b64 = Buffer.from(json, 'utf8').toString('base64');

    if (b64.length > 45_000) {
      throw new Error(`Storage state ist mit ${b64.length} Zeichen ungewöhnlich groß für ein GitHub Secret. Bitte nicht automatisch speichern.`);
    }

    await writeFile(jsonPath, `${json}\n`, { encoding: 'utf8', mode: 0o600 });
    await writeFile(b64Path, `${b64}\n`, { encoding: 'utf8', mode: 0o600 });

    output.write(`\nLOGIN_COMPLETE user=${username}\n`);
    output.write(`Storage-State gespeichert: ${jsonPath}\n`);
    output.write(`GitHub-Secret-Wert gespeichert: ${b64Path}\n`);
    output.write('Den Inhalt der TXT-Datei als GitHub Secret REDDIT_STORAGE_STATE_B64 speichern. Diese Dateien niemals committen oder teilen.\n');
  } finally {
    rl.close();
    await context.close().catch(() => undefined);
    await browser.close().catch(() => undefined);
  }
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
});
