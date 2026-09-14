"""Read-only Outscraper diagnostics for LOCENIX.

No Airtable/Supabase writes and no outreach. This probe tests two official Maps paths:
1) async request followed through the returned results_location URL, and
2) a tiny synchronous request for comparison.
All output is redacted to structural metadata and business-level summary fields.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any
from urllib.parse import urlparse

import httpx

API_URL = "https://api.outscraper.com/maps/search"
REQUEST_URL = "https://api.outscraper.com/requests/{request_id}"


def _walk(obj: Any):
    if isinstance(obj, dict):
        yield obj
        for value in obj.values():
            yield from _walk(value)
    elif isinstance(obj, list):
        for value in obj:
            yield from _walk(value)


def _first(d: dict[str, Any], *names: str):
    for name in names:
        value = d.get(name)
        if value not in (None, "", [], {}):
            return value
    return None


def _linkedin_values(obj: Any) -> list[str]:
    found: list[str] = []
    for d in _walk(obj):
        for key, value in d.items():
            if "linkedin" in str(key).lower():
                vals = value if isinstance(value, list) else [value]
                for val in vals:
                    if val and str(val) not in found:
                        found.append(str(val))
    return found


def _linkedin_types(values: list[str]) -> list[str]:
    types: list[str] = []
    for value in values:
        lowered = value.lower()
        if "/in/" in lowered:
            kind = "personal_profile"
        elif "/company/" in lowered:
            kind = "company_page"
        elif "linkedin.com" in lowered:
            kind = "other_linkedin"
        else:
            continue
        if kind not in types:
            types.append(kind)
    return types


def _email_count(obj: Any) -> int:
    values: set[str] = set()
    for d in _walk(obj):
        for key, value in d.items():
            if "email" in str(key).lower():
                vals = value if isinstance(value, list) else [value]
                for val in vals:
                    if isinstance(val, str) and "@" in val:
                        values.add(val.lower())
    return len(values)


def _website_host(place: dict[str, Any]) -> str | None:
    raw = str(_first(place, "site", "website") or "").strip()
    if not raw:
        return None
    try:
        return urlparse(raw).netloc.lower() or None
    except Exception:
        return None


def _payload_shape(payload: Any) -> dict[str, Any]:
    shape: dict[str, Any] = {"payload_type": type(payload).__name__}
    if isinstance(payload, dict):
        shape["top_level_keys"] = sorted(str(k) for k in payload.keys())[:30]
        data = payload.get("data")
        shape["data_type"] = type(data).__name__
        if isinstance(data, list):
            shape["data_len"] = len(data)
            if data:
                shape["data_first_type"] = type(data[0]).__name__
                if isinstance(data[0], list):
                    shape["data_first_len"] = len(data[0])
                    if data[0] and isinstance(data[0][0], dict):
                        shape["first_place_keys"] = sorted(str(k) for k in data[0][0].keys())[:40]
                elif isinstance(data[0], dict):
                    shape["first_place_keys"] = sorted(str(k) for k in data[0].keys())[:40]
        elif isinstance(data, dict):
            shape["data_keys"] = sorted(str(k) for k in data.keys())[:30]
        elif isinstance(data, str):
            shape["data_is_url"] = data.startswith(("http://", "https://"))
    elif isinstance(payload, list):
        shape["list_len"] = len(payload)
        if payload:
            shape["first_type"] = type(payload[0]).__name__
    return shape


def _extract_places(payload: Any) -> list[dict[str, Any]]:
    raw: Any = payload.get("data") if isinstance(payload, dict) else payload
    direct: list[dict[str, Any]] = []
    if isinstance(raw, list):
        if all(isinstance(item, dict) for item in raw):
            direct = [item for item in raw if isinstance(item, dict)]
        else:
            for group in raw:
                if isinstance(group, list):
                    direct.extend(item for item in group if isinstance(item, dict))
                elif isinstance(group, dict):
                    direct.append(group)
    elif isinstance(raw, dict):
        for key in ("results", "places", "items", "data"):
            value = raw.get(key)
            if isinstance(value, list):
                direct.extend(item for item in value if isinstance(item, dict))

    candidates = direct or [
        d for d in _walk(payload)
        if any(k in d for k in ("name", "title", "place_id", "google_id", "full_address"))
    ]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in candidates:
        name = str(_first(d, "name", "title") or "").strip()
        identity = str(_first(d, "place_id", "google_id", "cid", "location_link") or name).strip()
        if not name or not identity or identity in seen:
            continue
        seen.add(identity)
        out.append(d)
    return out


def _safe_results_location(raw: Any) -> str | None:
    value = str(raw or "").strip()
    if not value:
        return None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not (host == "outscraper.com" or host.endswith(".outscraper.com")):
        return None
    return value


def _fetch_async_result(client: httpx.Client, key: str, request_id: str, results_location: str | None, max_wait: int) -> Any:
    target = results_location or REQUEST_URL.format(request_id=request_id)
    source = "results_location" if results_location else "request_id_fallback"
    print(json.dumps({"async_poll_source": source, "results_location_host": urlparse(target).hostname}, ensure_ascii=False))

    initial_wait = min(60, max_wait)
    print(f"initial_wait_seconds={initial_wait}")
    time.sleep(initial_wait)
    deadline = time.monotonic() + max(0, max_wait - initial_wait)
    attempt = 0

    while time.monotonic() <= deadline:
        attempt += 1
        response = client.get(target, headers={"X-API-KEY": key}, timeout=30.0)
        print(f"async_poll={attempt} HTTP={response.status_code}")
        if response.status_code == 204:
            raise RuntimeError("Outscraper async result returned 204")
        if response.status_code != 200:
            print(response.text[:500])
            response.raise_for_status()
        body = response.json()
        status = str(body.get("status", "")).strip().lower() if isinstance(body, dict) else "success"
        print(json.dumps({"request_id": request_id, "status": body.get("status") if isinstance(body, dict) else None}, ensure_ascii=False))
        if status in ("success", ""):
            print(json.dumps({"async_payload_shape": _payload_shape(body)}, ensure_ascii=False))
            return body
        if status == "failure":
            raise RuntimeError(f"Outscraper async request failed: {json.dumps(body, ensure_ascii=False)[:500]}")
        if time.monotonic() + 60 > deadline:
            break
        time.sleep(60)
    raise TimeoutError(f"Outscraper async request {request_id} timed out after {max_wait}s")


def _summary(payload: Any, limit: int) -> dict[str, Any]:
    places = _extract_places(payload)[:limit]
    rows = []
    for place in places:
        linkedin = _linkedin_values(place)
        rows.append({
            "name": _first(place, "name", "title"),
            "website_host": _website_host(place),
            "rating": _first(place, "rating"),
            "reviews": _first(place, "reviews", "reviews_count", "reviews_number"),
            "linkedin_count": len(linkedin),
            "linkedin_types": _linkedin_types(linkedin),
            "email_count": _email_count(place),
        })
    return {
        "places_detected": len(places),
        "with_website": sum(1 for x in rows if x["website_host"]),
        "with_linkedin": sum(1 for x in rows if x["linkedin_count"] > 0),
        "with_email": sum(1 for x in rows if x["email_count"] > 0),
        "results": rows,
    }


def main() -> int:
    key = os.getenv("OUTSCRAPER_API_KEY", "").strip()
    if not key:
        print("OUTSCRAPER_API_KEY is not configured.")
        return 2

    query = os.getenv("OUTSCRAPER_TEST_QUERY", "restaurants, Cologne, Germany")
    limit = max(1, min(int(os.getenv("OUTSCRAPER_TEST_LIMIT", "3")), 3))
    max_wait = max(120, min(int(os.getenv("OUTSCRAPER_MAX_WAIT_SECONDS", "600")), 900))
    headers = {"X-API-KEY": key}

    async_payload: Any = None
    sync_payload: Any = None
    request_id = ""

    with httpx.Client() as client:
        print(json.dumps({"probe": "outscraper_results_location_plus_sync", "query": query, "limit": limit}, ensure_ascii=False))

        async_response = client.get(
            API_URL,
            params=[("query", query), ("limit", str(limit)), ("async", "true")],
            headers=headers,
            timeout=30.0,
        )
        print(f"async_submit_HTTP={async_response.status_code}")
        if async_response.status_code in (200, 202):
            submitted = async_response.json()
            request_id = str(submitted.get("id", "")).strip() if isinstance(submitted, dict) else ""
            results_location = _safe_results_location(submitted.get("results_location") if isinstance(submitted, dict) else None)
            print(json.dumps({
                "request_id": request_id or None,
                "status": submitted.get("status") if isinstance(submitted, dict) else None,
                "response_keys": sorted(str(k) for k in submitted.keys()) if isinstance(submitted, dict) else [],
                "has_valid_results_location": bool(results_location),
            }, ensure_ascii=False))
            try:
                status = str(submitted.get("status", "")).strip().lower() if isinstance(submitted, dict) else ""
                async_payload = submitted if status == "success" else _fetch_async_result(client, key, request_id, results_location, max_wait)
            except Exception as exc:
                print(f"ASYNC_ERROR: {type(exc).__name__}: {exc}")
        else:
            print(async_response.text[:500])

        # Independent tiny synchronous control test. This is diagnostic only.
        print("starting_sync_control_test=true")
        try:
            sync_response = client.get(
                API_URL,
                params=[("query", query), ("limit", str(limit)), ("async", "false")],
                headers=headers,
                timeout=240.0,
            )
            print(f"sync_HTTP={sync_response.status_code}")
            if sync_response.status_code == 200:
                sync_payload = sync_response.json()
                print(json.dumps({"sync_payload_shape": _payload_shape(sync_payload)}, ensure_ascii=False))
            else:
                print(sync_response.text[:500])
        except Exception as exc:
            print(f"SYNC_ERROR: {type(exc).__name__}: {exc}")

    async_summary = _summary(async_payload, limit) if async_payload is not None else None
    sync_summary = _summary(sync_payload, limit) if sync_payload is not None else None
    print(json.dumps({
        "request_id": request_id or None,
        "async_summary": async_summary,
        "sync_summary": sync_summary,
    }, ensure_ascii=False, indent=2))

    async_places = (async_summary or {}).get("places_detected", 0)
    sync_places = (sync_summary or {}).get("places_detected", 0)
    if async_places or sync_places:
        return 0
    print("ERROR: Neither official async results_location nor synchronous Maps path returned any places.")
    return 3


if __name__ == "__main__":
    sys.exit(main())
