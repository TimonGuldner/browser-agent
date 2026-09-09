import asyncio
import json
import os
from datetime import datetime, timezone

import httpx

from agent.local_worker import db_client, find_chromium, restore_profile, verify_linkedin_login

AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID", "appN6ox7fjGFyXZhL")
AIRTABLE_BRAND_TABLE_ID = "tblZrS1SpVkoIFSAG"


def airtable_probe() -> bool:
    token = os.getenv("AIRTABLE_PAT", "").strip()
    if not token:
        return False
    response = httpx.get(
        f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{AIRTABLE_BRAND_TABLE_ID}",
        headers={"Authorization": f"Bearer {token}"},
        params={"maxRecords": 1},
        timeout=30,
    )
    return response.status_code == 200


async def main() -> None:
    db = db_client()
    result = {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "airtable_ok": False,
        "profile_restored": False,
        "linkedin_logged_in": False,
        "linkedin_url": None,
        "linkedin_title": None,
        "error": None,
    }
    try:
        result["airtable_ok"] = airtable_probe()
        result["profile_restored"] = restore_profile(db)
        if result["profile_restored"]:
            verification = await verify_linkedin_login(find_chromium())
            result["linkedin_logged_in"] = bool(verification.get("logged_in"))
            result["linkedin_url"] = verification.get("url")
            result["linkedin_title"] = verification.get("title")
    except Exception as exc:
        result["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"

    db.table("agent_config").upsert(
        {
            "key": "connection_probe_last",
            "value": result,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="key",
    ).execute()
    print(json.dumps(result, ensure_ascii=True))

    if not (result["airtable_ok"] and result["profile_restored"] and result["linkedin_logged_in"]):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
