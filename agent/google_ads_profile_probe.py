import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

from agent import local_worker

RESULT = Path('google_ads_profile_probe_result.json')


async def main() -> None:
    db = local_worker.db_client()
    restored = local_worker.restore_profile(db)
    if not restored:
        payload = {'restored': False, 'authenticated': False, 'reason': 'PROFILE_NOT_FOUND'}
        RESULT.write_text(json.dumps(payload, indent=2), encoding='utf-8')
        print('PROFILE_PROBE=' + json.dumps(payload), flush=True)
        return

    chromium = local_worker.find_chromium()
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            str(local_worker.PROFILE_DIR),
            executable_path=chromium,
            headless=True,
            args=['--no-sandbox', '--disable-dev-shm-usage', '--password-store=basic'],
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://ads.google.com/aw/overview', wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(8000)
            url = page.url
            title = await page.title()
            authenticated = (
                'accounts.google.com' not in url
                and 'ServiceLogin' not in url
                and 'signin' not in url.lower()
                and 'ads.google.com' in url
            )
            body = ''
            try:
                body = (await page.locator('body').inner_text(timeout=5000))[:2000]
            except Exception:
                pass
            account_visible = '475-469-4125' in body or 'LOCENIX' in body.upper()
            payload = {
                'restored': True,
                'authenticated': authenticated,
                'account_hint_visible': account_visible,
                'title': title,
                'host': url.split('/')[2] if '://' in url else '',
            }
            RESULT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
            print('PROFILE_PROBE=' + json.dumps(payload, ensure_ascii=False), flush=True)
        finally:
            await context.close()


if __name__ == '__main__':
    asyncio.run(main())
