"""Small, read-only Outscraper probe for LOCENIX lead-source validation.

This intentionally does NOT write to Airtable/Supabase and does NOT perform outreach.
It follows Outscraper's documented Google Maps async flow with a minimal request,
waits before polling, and prints a redacted summary.
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
    """Return only structural diagnostics; never dump emails or profile URLs."""
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

    candidates = direct
    if not candidates:
        candidates = [
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


def _wait_for_result(client: httpx.Client, key: str, request_id: str, max_wait_seconds: int) -> Any:
    # Outscraper docs say async jobs are usually ready within 1-3 minutes and
    # recommend waiting before checking. Poll slowly to avoid hammering the archive endpoint.
    initial_wait = min(60, max_wait_seconds)
    print(f"initial_wait_seconds={initial_wait}")
    time.sleep(initial_wait)

    deadline = time.monotonic() + max(0, max_wait_seconds - initial_wait)
    poll_seconds = 60
    attempt = 0

    while time.monotonic() <= deadline:
        attempt += 1
        response = client.get(
            REQUEST_URL.format(request_id=request_id),
            headers={"X-API-KEY": key},
            timeout=30.0,
        )
        print(f"poll={attempt} HTTP={response.status_code}")

        if response.status_code == 204:
            raise RuntimeError("Outscraper request archive returned 204 (Failure/no results)")
        if response.status_code != 200:
            print(response.text[:1000])
            response.raise_for_status()

        body = response.json()
        status = str(body.get("status", "")).strip().lower()
        print(json.dumps({"request_id": request_id, "status": body.get("status")}, ensure_ascii=False))

        if status == "success":
            print(json.dumps({"success_payload_shape": _payload_shape(body)}, ensure_ascii=False))
            return body
        if status == "failure":
            raise RuntimeError(f"Outscraper request failed: {json.dumps(body, ensure_ascii=False)[:1000]}")

        if time.monotonic() + poll_seconds > deadline:
            break
        time.sleep(poll_seconds)

    raise TimeoutError(f"Outscraper request {request_id} did not finish within {max_wait_seconds}s")


def main() -> int:
    key = os.getenv("OUTSCRAPER_API_KEY", "").strip()
    if not key:
        print("OUTSCRAPER_API_KEY is not configured.")
        return 2

    query = os.getenv("OUTSCRAPER_TEST_QUERY", "restaurants, Cologne, Germany")
    limit = max(1, min(int(os.getenv("OUTSCRAPER_TEST_LIMIT", "3")), 10))
    max_wait = max(120, min(int(os.getenv("OUTSCRAPER_MAX_WAIT_SECONDS", "600")), 1200))

    # Minimal documented request: query + required limit + async.
    # No language/region/enrichment/flat until the base Maps call is proven stable.
    params = [
        ("query", query),
        ("limit", str(limit)),
        ("async", "true"),
    ]

    print(json.dumps({"probe": "outscraper_maps_minimal_async", "query": query, "limit": limit}, ensure_ascii=False))

    request_id = ""
    with httpx.Client() as client:
        response = client.get(API_URL, params=params, headers={"X-API-KEY": key}, timeout=30.0)
        print(f"submit HTTP {response.status_code}")

        if response.status_code not in (200, 202):
            print(response.text[:1000])
            return 1

        submitted = response.json()
        status = str(submitted.get("status", "")).strip().lower()
        request_id = str(submitted.get("id", "")).strip()
        print(json.dumps({
            "request_id": request_id or None,
            "status": submitted.get("status"),
            "response_keys": sorted(str(k) for k in submitted.keys())[:30],
        }, ensure_ascii=False))

        if status == "success":
            payload = submitted
            print(json.dumps({"success_payload_shape": _payload_shape(payload)}, ensure_ascii=False))
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
            "website_host": _website_host(place),
            "rating": _first(place, "rating"),
            "reviews": _first(place, "reviews", "reviews_count", "reviews_number"),
            "linkedin_count": len(linkedin),
            "linkedin_types": _linkedin_types(linkedin),
            "email_count": _email_count(place),
        })

    result = {
        "request_id": request_id or None,
        "places_detected": len(places),
        "with_website": sum(1 for item in summary if item["website_host"]),
        "with_linkedin": sum(1 for item in summary if item["linkedin_count"] > 0),
        "with_personal_linkedin": sum(1 for item in summary if "personal_profile" in item["linkedin_types"]),
        "with_company_linkedin": sum(1 for item in summary if "company_page" in item["linkedin_types"]),
        "with_email": sum(1 for item in summary if item["email_count"] > 0),
        "results": summary,
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))

    if not places:
        print("ERROR: Outscraper returned Success but the documented data array contained no places.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
