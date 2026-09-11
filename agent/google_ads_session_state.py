import json
from typing import Any

from playwright.async_api import BrowserContext, Page

from agent import local_worker

SESSION_OBJECT = 'sessions/locenix-google-ads-storage-state.enc'


def _store(db):
    return db.storage.from_(local_worker.PROFILE_BUCKET)


async def save_session_state(db, context: BrowserContext) -> int:
    state = await context.storage_state()
    raw = json.dumps(state, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
    encrypted = local_worker.derive_fernet().encrypt(raw)
    store = _store(db)
    try:
        store.update(SESSION_OBJECT, encrypted, {'content-type': 'application/octet-stream', 'upsert': 'true'})
    except Exception:
        try:
            store.remove([SESSION_OBJECT])
        except Exception:
            pass
        store.upload(SESSION_OBJECT, encrypted, {'content-type': 'application/octet-stream', 'upsert': 'true'})
    return len(encrypted)


def load_session_state(db) -> dict[str, Any] | None:
    try:
        encrypted = _store(db).download(SESSION_OBJECT)
        raw = local_worker.derive_fernet().decrypt(encrypted)
        return json.loads(raw.decode('utf-8'))
    except Exception:
        return None


async def apply_session_state(db, context: BrowserContext, page: Page) -> bool:
    state = load_session_state(db)
    if not state:
        return False
    cookies = state.get('cookies') or []
    if cookies:
        await context.add_cookies(cookies)
    for origin in state.get('origins') or []:
        origin_url = origin.get('origin')
        local_storage = origin.get('localStorage') or []
        if not origin_url or not local_storage:
            continue
        try:
            await page.goto(origin_url, wait_until='domcontentloaded', timeout=30000)
            await page.evaluate(
                """items => { for (const item of items) localStorage.setItem(item.name, item.value); }""",
                local_storage,
            )
        except Exception:
            continue
    return True
