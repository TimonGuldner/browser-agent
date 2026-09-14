"""Small, read-only Outscraper probe for LOCENIX lead-source validation.

This intentionally does NOT write to Airtable/Supabase and does NOT perform outreach.
It submits a tiny Google Maps search asynchronously, polls Outscraper's request-results
endpoint, and prints a redacted summary so we can measure whether Outscraper can
replace browser research.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Any

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


def _extract_places(payload: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for d in _walk(payload):
        if any(k in d for k in ("name", "place_id", "google_id", "full_address")):
            candidates.append(d)
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in candidates:
        name = str(_first(d, "name", "title") or "")
        identity = str(_first(d, "place_id", "google_id", "cid", "location_link") or name)
        if name and identity not in seen:
            seen.add(identity)
            out.append(d)
    return out


def _wait_for_result(client: httpx.Client, key: str, request_id: str, max_wait_seconds: int) -> Any:
    deadline = time.monotonic() + max_wait_seconds
    poll_seconds = 10
    attempt = 0

    while time.monotonic() < deadline:
        attempt += 1
        response = client.get(
            REQUEST_URL.format(request_id=request_id),
            params={"flat": "false"},
            headers={"X-API-KEY": key},
            timeout=30.0,
        )
        print(f"poll={attempt} HTTP={response.status_code}")

        if response.status_code == 204:
            print("Outscraper request finished with Failure/no results.")
            raise RuntimeError("Outscraper request failed")
        if response.status_code != 200:
            print(response.text[:1000])
            response.raise_for_status()

        body = response.json()
        status = str(body.get("status", "")).strip().lower()
        print(json.dumps({"request_id": request_id, "status": body.get("status")}, ensure_ascii=False))

        if status == "success":
            return body
        if status == "failure":
            raise RuntimeError(f"Outscraper request failed: {json.dumps(body, ensure_ascii=False)[:1000]}")

        time.sleep(poll_seconds)

    raise TimeoutError(f"Outscraper request {request_id} did not finish within {max_wait_seconds}s")


def main() -> int:
    key = os.getenv("OUTSCRAPER_API_KEY", "").strip()
    if not key:
        print("OUTSCRAPER_API_KEY is not configured.")
        return 2

    query = os.getenv("OUTSCRAPER_TEST_QUERY", "Physiotherapie, Köln, Deutschland")
    limit = max(1, min(int(os.getenv("OUTSCRAPER_TEST_LIMIT", "5")), 10))
    max_wait = max(60, min(int(os.getenv("OUTSCRAPER_MAX_WAIT_SECONDS", "600")), 1200))
    params = [
        ("query", query),
        ("language", "de"),
        ("region", "DE"),
        ("limit", str(limit)),
        ("async", "true"),
        ("enrichment", "contacts_n_leads"),
    ]

    print(json.dumps({"probe": "outscraper_maps_contacts_async", "query": query, "limit": limit}, ensure_ascii=False))

    with httpx.Client() as client:
        response = client.get(API_URL, params=params, headers={"X-API-KEY": key}, timeout=30.0)
        print(f"submit HTTP {response.status_code}")

        if response.status_code not in (200, 202):
            print(response.text[:1000])
            return 1

        submitted = response.json()
        status = str(submitted.get("status", "")).strip().lower()
        request_id = str(submitted.get("id", "")).strip()
        print(json.dumps({"request_id": request_id or None, "status": submitted.get("status")}, ensure_ascii=False))

        if status == "success":
            payload = submitted
        else:
            if not request_id:
                print("Async Outscraper response did not include a request id.")
                print(json.dumps(submitted, ensure_ascii=False)[:1000])
                return 1
            try:
                payload = _wait_for_result(client, key, request_id, max_wait)
            except Exception as exc:
                print(f"ERROR: {type(exc).__name__}: {exc}")
                return 1

    places = _extract_places(payload)[:limit]
    summary = []
    for place in places:
        linkedin = _linkedin_values(place)
        summary.append({
            "name": _first(place, "name", "title"),
            "website": _first(place, "site", "website"),
            "rating": _first(place, "rating"),
            "reviews": _first(place, "reviews", "reviews_count", "reviews_number"),
            "linkedin_count": len(linkedin),
            "linkedin": linkedin[:5],
            "email_count": _email_count(place),
        })

    print(json.dumps({
        "request_id": request_id or None,
        "places_detected": len(places),
        "with_linkedin": sum(1 for item in summary if item["linkedin_count"] > 0),
        "with_email": sum(1 for item in summary if item["email_count"] > 0),
        "results": summary,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
