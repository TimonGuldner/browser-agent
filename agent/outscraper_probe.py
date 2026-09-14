"""Read-only Outscraper diagnostics for LOCENIX.

The probe submits a tiny Google Maps job, follows Outscraper's returned
results_location safely, prints structural diagnostics, and summarizes only
business-level fields. It never writes to Airtable/Supabase and never sends
outreach.
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
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


def _is_public_https_url(raw: Any) -> tuple[str | None, str | None]:
    """Return (url, host) for a public HTTPS URL; never expose API keys externally."""
    value = str(raw or "").strip()
    if not value:
        return None, None
    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not host:
        return None, host or None
    if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
        return None, host
    try:
        for info in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return None, host
    except OSError:
        # DNS can be transient on the runner. HTTPS + external hostname is enough
        # for diagnostics; crucially, the API key is only sent to Outscraper hosts.
        pass
    return value, host


def _headers_for(host: str | None, api_key: str) -> dict[str, str]:
    if host and (host == "outscraper.com" or host.endswith(".outscraper.com")):
        return {"X-API-KEY": api_key}
    return {}


def _payload_shape(payload: Any) -> dict[str, Any]:
    shape: dict[str, Any] = {"payload_type": type(payload).__name__}
    if isinstance(payload, dict):
        shape["top_level_keys"] = sorted(str(k) for k in payload.keys())[:40]
        data = payload.get("data")
        shape["data_type"] = type(data).__name__
        if isinstance(data, list):
            shape["data_len"] = len(data)
            if data:
                shape["data_first_type"] = type(data[0]).__name__
                if isinstance(data[0], list):
                    shape["data_first_len"] = len(data[0])
                    if data[0] and isinstance(data[0][0], dict):
                        shape["first_place_keys"] = sorted(str(k) for k in data[0][0].keys())[:60]
                elif isinstance(data[0], dict):
                    shape["first_place_keys"] = sorted(str(k) for k in data[0].keys())[:60]
        elif isinstance(data, dict):
            shape["data_keys"] = sorted(str(k) for k in data.keys())[:40]
        elif isinstance(data, str):
            parsed = urlparse(data)
            shape["data_is_url"] = parsed.scheme in ("http", "https")
            shape["data_url_host"] = parsed.hostname
    elif isinstance(payload, list):
        shape["list_len"] = len(payload)
        if payload:
            shape["first_type"] = type(payload[0]).__name__
            if isinstance(payload[0], dict):
                shape["first_keys"] = sorted(str(k) for k in payload[0].keys())[:60]
    return shape


def _looks_like_place(d: dict[str, Any]) -> bool:
    keys = set(d)
    identity = bool(keys & {"name", "title"}) and bool(
        keys & {"place_id", "google_id", "cid", "location_link", "full_address", "address"}
    )
    business_fields = bool(keys & {"site", "website", "phone", "rating", "reviews", "type", "category"})
    return identity or (bool(keys & {"name", "title"}) and business_fields)


def _extract_places(payload: Any) -> list[dict[str, Any]]:
    candidates = [d for d in _walk(payload) if _looks_like_place(d)]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for d in candidates:
        name = str(_first(d, "name", "title") or "").strip()
        identity = str(_first(d, "place_id", "google_id", "cid", "location_link", "full_address", "address") or name).strip()
        if not name or not identity:
            continue
        marker = f"{name.lower()}|{identity.lower()}"
        if marker in seen:
            continue
        seen.add(marker)
        out.append(d)
    return out


def _collect_strings_for_keys(obj: Any, needles: tuple[str, ...]) -> list[str]:
    values: list[str] = []
    seen: set[str] = set()
    for d in _walk(obj):
        for key, value in d.items():
            lowered_key = str(key).lower()
            if not any(needle in lowered_key for needle in needles):
                continue
            raw_values = value if isinstance(value, list) else [value]
            for item in raw_values:
                if isinstance(item, str):
                    text = item.strip()
                    if text and text not in seen:
                        seen.add(text)
                        values.append(text)
    return values


def _email_values(obj: Any) -> list[str]:
    return [v for v in _collect_strings_for_keys(obj, ("email",)) if "@" in v]


def _linkedin_values(obj: Any) -> list[str]:
    found = _collect_strings_for_keys(obj, ("linkedin",))
    # Some contact-enrichment payloads store socials under generic URL fields.
    for d in _walk(obj):
        for value in d.values():
            vals = value if isinstance(value, list) else [value]
            for item in vals:
                if isinstance(item, str) and "linkedin.com/" in item.lower() and item not in found:
                    found.append(item)
    return found


def _website_host(place: dict[str, Any]) -> str | None:
    raw = str(_first(place, "site", "website", "domain") or "").strip()
    if not raw:
        return None
    candidate = raw if "://" in raw else f"https://{raw}"
    try:
        return urlparse(candidate).netloc.lower() or None
    except Exception:
        return None


def _summary(payload: Any, limit: int) -> dict[str, Any]:
    places = _extract_places(payload)[:limit]
    rows: list[dict[str, Any]] = []
    for place in places:
        linkedin = _linkedin_values(place)
        company = [v for v in linkedin if "/company/" in v.lower()]
        personal = [v for v in linkedin if "/in/" in v.lower()]
        rows.append({
            "name": _first(place, "name", "title"),
            "website_host": _website_host(place),
            "email_count": len(_email_values(place)),
            "linkedin_company_count": len(company),
            "linkedin_personal_count": len(personal),
            "rating": _first(place, "rating"),
            "reviews": _first(place, "reviews", "reviews_count", "reviews_number"),
        })
    return {
        "places_detected": len(places),
        "with_website": sum(bool(x["website_host"]) for x in rows),
        "with_email": sum(x["email_count"] > 0 for x in rows),
        "with_linkedin_company": sum(x["linkedin_company_count"] > 0 for x in rows),
        "with_linkedin_personal": sum(x["linkedin_personal_count"] > 0 for x in rows),
        "results": rows,
    }


def _status(body: Any) -> str:
    if not isinstance(body, dict):
        return ""
    return str(body.get("status", "")).strip().lower()


def _follow_result_location(client: httpx.Client, api_key: str, raw_url: Any, request_id: str, max_wait: int) -> Any:
    url, host = _is_public_https_url(raw_url)
    parsed = urlparse(str(raw_url or ""))
    print(json.dumps({
        "results_location_present": bool(raw_url),
        "results_location_scheme": parsed.scheme or None,
        "results_location_host": parsed.hostname,
        "results_location_path_prefix": "/".join(parsed.path.split("/")[:3])[:120] or None,
        "results_location_accepted": bool(url),
        "api_key_sent_to_result_host": bool(url and host and (host == "outscraper.com" or host.endswith(".outscraper.com"))),
    }, ensure_ascii=False))

    if not url:
        url = REQUEST_URL.format(request_id=request_id)
        host = "api.outscraper.com"
        print("result_location_rejected_using_request_id_fallback=true")

    deadline = time.monotonic() + max_wait
    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1
        try:
            response = client.get(url, headers=_headers_for(host, api_key), timeout=45.0, follow_redirects=True)
        except httpx.ReadTimeout:
            print(f"result_poll={attempt} read_timeout=true")
            time.sleep(10)
            continue

        print(f"result_poll={attempt} HTTP={response.status_code}")
        if response.status_code in (202, 204, 404):
            time.sleep(10)
            continue
        response.raise_for_status()
        body = response.json()
        status = _status(body)
        places = _extract_places(body)
        print(json.dumps({
            "result_poll": attempt,
            "status": body.get("status") if isinstance(body, dict) else None,
            "places_seen": len(places),
            "payload_shape": _payload_shape(body),
        }, ensure_ascii=False))

        if places:
            return body
        if status == "failure":
            # If result_location itself reports failure, one final request endpoint
            # check may contain more detail; do not silently convert it to success.
            if url != REQUEST_URL.format(request_id=request_id):
                fallback = client.get(REQUEST_URL.format(request_id=request_id), headers={"X-API-KEY": api_key}, timeout=30.0)
                print(f"failure_detail_HTTP={fallback.status_code}")
                if fallback.status_code == 200:
                    detail = fallback.json()
                    print(json.dumps({"failure_detail_shape": _payload_shape(detail), "failure_detail_status": detail.get("status") if isinstance(detail, dict) else None}, ensure_ascii=False))
            raise RuntimeError("Outscraper result_location reported Failure")
        if status in ("pending", "running", "processing", "in progress", ""):
            time.sleep(10)
            continue
        # Unknown terminal state: keep polling briefly rather than guessing.
        time.sleep(10)

    raise TimeoutError(f"Outscraper request {request_id} timed out after {max_wait}s")


def main() -> int:
    key = os.getenv("OUTSCRAPER_API_KEY", "").strip()
    if not key:
        print("OUTSCRAPER_API_KEY is not configured.")
        return 2

    query = os.getenv("OUTSCRAPER_TEST_QUERY", "physiotherapist, Cologne, Germany")
    limit = max(1, min(int(os.getenv("OUTSCRAPER_TEST_LIMIT", "5")), 5))
    max_wait = max(120, min(int(os.getenv("OUTSCRAPER_MAX_WAIT_SECONDS", "600")), 900))

    with httpx.Client() as client:
        print(json.dumps({"probe": "outscraper_result_location_v2", "query": query, "limit": limit}, ensure_ascii=False))
        response = client.get(
            API_URL,
            params=[("query", query), ("limit", str(limit)), ("async", "true")],
            headers={"X-API-KEY": key},
            timeout=30.0,
        )
        print(f"async_submit_HTTP={response.status_code}")
        if response.status_code not in (200, 202):
            print(response.text[:500])
            return 3

        submitted = response.json()
        request_id = str(submitted.get("id", "")).strip() if isinstance(submitted, dict) else ""
        raw_location = submitted.get("results_location") if isinstance(submitted, dict) else None
        print(json.dumps({
            "request_id": request_id or None,
            "submit_status": submitted.get("status") if isinstance(submitted, dict) else None,
            "submit_keys": sorted(str(k) for k in submitted.keys()) if isinstance(submitted, dict) else [],
            "submit_shape": _payload_shape(submitted),
        }, ensure_ascii=False))
        if not request_id:
            print("ERROR: Outscraper did not return a request id")
            return 4

        try:
            payload = submitted if _extract_places(submitted) else _follow_result_location(client, key, raw_location, request_id, max_wait)
        except Exception as exc:
            print(f"PROBE_ERROR: {type(exc).__name__}: {exc}")
            return 5

    summary = _summary(payload, limit)
    print(json.dumps({"request_id": request_id, "summary": summary}, ensure_ascii=False, indent=2))
    if summary["places_detected"] == 0:
        print("ERROR: Completed response contained no detectable places.")
        return 6
    return 0


if __name__ == "__main__":
    sys.exit(main())
