import asyncio
import logging
from datetime import datetime, timezone

from supabase import Client, create_client

from agent.browser_runner import run_browser_task
from agent.config import settings

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('browser-agent')


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def client() -> Client:
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def claim_next_job(db: Client):
    # Single-worker baseline. For multiple workers, replace this with the atomic
    # claim_browser_job() SQL function included in sql/schema.sql.
    response = (
        db.table('browser_jobs')
        .select('*')
        .eq('status', 'pending')
        .order('created_at')
        .limit(1)
        .execute()
    )
    if not response.data:
        return None

    job = response.data[0]
    claimed = (
        db.table('browser_jobs')
        .update({'status': 'running', 'started_at': now_iso()})
        .eq('id', job['id'])
        .eq('status', 'pending')
        .execute()
    )
    return job if claimed.data else None


async def process_job(db: Client, job: dict):
    job_id = job['id']
    log.info('Running job %s', job_id)
    try:
        result = await run_browser_task(job['task'])
        db.table('browser_jobs').update(
            {'status': 'completed', 'result': result, 'finished_at': now_iso()}
        ).eq('id', job_id).execute()
        log.info('Completed job %s', job_id)
    except Exception as exc:
        log.exception('Job %s failed', job_id)
        db.table('browser_jobs').update(
            {'status': 'failed', 'error': str(exc)[:10000], 'finished_at': now_iso()}
        ).eq('id', job_id).execute()


async def main():
    db = client()
    log.info('Browser Agent worker started')
    while True:
        job = claim_next_job(db)
        if job:
            await process_job(db, job)
        else:
            await asyncio.sleep(settings.poll_seconds)


if __name__ == '__main__':
    asyncio.run(main())
