"""Small, read-only Outscraper probe for LOCENIX lead-source validation.

This intentionally does NOT write to Airtable/Supabase and does NOT perform outreach.
It fetches a tiny Google Maps sample with contacts/social enrichment and prints a
redacted summary so we can measure whether Outscraper can replace browser research.
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any

import httpx

API_URL = "https://api.outscraper.com/maps/search"


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
    # Keep likely top-level place objects, deduping by stable-ish identity.
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in candidates:
        name = str(_first(d, "name", "title") or "")
        identity = str(_first(d, "place_id", "google_id", "cid", "location_link") or name)
        if name and identity not in seen:
            seen.add(identity)
            out.append(d)
    return out


def main() -> int:
    key = os.getenv("OUTSCRAPER_API_KEY", "").strip()
    if not key:
        print("OUTSCRAPER_API_KEY is not configured.")
        return 2

    query = os.getenv("OUTSCRAPER_TEST_QUERY", "Physiotherapie, Köln, Deutschland")
    limit = max(1, min(int(os.getenv("OUTSCRAPER_TEST_LIMIT", "5")), 10))
    params = [
        ("query", query),
        ("language", "de"),
        ("region", "DE"),
        ("limit", str(limit)),
        ("async", "false"),
        ("enrichment", "contacts_n_leads"),
    ]
    print(json.dumps({"probe": "outscraper_maps_contacts", "query": query, "limit": limit}, ensure_ascii=False))
    with httpx.Client(timeout=180.0) as client:
        response = client.get(API_URL, params=params, headers={"X-API-KEY": key})
    print(f"HTTP {response.status_code}")
    if response.status_code != 200:
        print(response.text[:1000])
        return 1

    payload = response.json()
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
    print(json.dumps({"places_detected": len(places), "results": summary}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
