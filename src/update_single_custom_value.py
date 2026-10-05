from __future__ import annotations

import argparse
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from .oauth import OAuthError, OAuthManager


BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _canonical_key(value: str) -> str:
    text = value.strip()
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2]
    return "".join(text.split())


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "Version": API_VERSION,
    }


def _get_values(
    session: requests.Session, token: str, location_id: str
) -> list[dict[str, Any]]:
    response = session.get(
        f"{BASE_URL}/locations/{location_id}/customValues",
        headers=_headers(token),
        timeout=30,
    )
    if response.status_code >= 400:
        raise OAuthError(f"GET customValues failed with HTTP {response.status_code}")
    values = response.json().get("customValues", [])
    if not isinstance(values, list):
        raise OAuthError("GET customValues returned an unexpected response shape")
    return values


def _find_exact(values: list[dict[str, Any]], target: str) -> dict[str, Any]:
    canonical = _canonical_key(target)
    matches = [
        item
        for item in values
        if _canonical_key(str(item.get("fieldKey") or "")) == canonical
    ]
    if not matches:
        raise OAuthError(f"Custom Value key was not found: {target}")
    if len(matches) != 1:
        raise OAuthError(f"Custom Value key matched {len(matches)} items; update aborted")
    return matches[0]


def main() -> int:
    parser = argparse.ArgumentParser(description="Update exactly one existing Custom Value.")
    parser.add_argument("--location", required=True)
    parser.add_argument("--key", required=True)
    parser.add_argument("--value", required=True)
    parser.add_argument("--log-dir", default=str(PROJECT_ROOT / "logs"))
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    oauth = OAuthManager.from_env()
    token = oauth.get_location_token(args.location)
    session = requests.Session()
    item = _find_exact(_get_values(session, token, args.location), args.key)
    custom_value_id = str(item.get("id") or "")
    name = str(item.get("name") or "")
    before = str(item.get("value") or "")
    if not custom_value_id or not name:
        raise OAuthError("Matched Custom Value did not include both id and name")

    changed = before != args.value
    if changed:
        response = session.put(
            f"{BASE_URL}/locations/{args.location}/customValues/{custom_value_id}",
            headers=_headers(token),
            json={"name": name, "value": args.value},
            timeout=30,
        )
        if response.status_code >= 400:
            raise OAuthError(f"PUT customValue failed with HTTP {response.status_code}")

    verified = item
    after = before
    for attempt in range(5):
        verified = _find_exact(_get_values(session, token, args.location), args.key)
        after = str(verified.get("value") or "")
        if after == args.value:
            break
        if attempt < 4:
            time.sleep(0.5 * (2**attempt))
    if after != args.value:
        raise OAuthError("Read-back verification failed; value does not match requested value")

    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = log_dir / f"single-custom-value-update-{stamp}.json"
    log_path.write_text(
        json.dumps(
            {
                "timestamp": stamp,
                "locationId": args.location,
                "customValueId": custom_value_id,
                "name": name,
                "fieldKey": verified.get("fieldKey"),
                "before": before,
                "after": after,
                "changed": changed,
                "verified": True,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.chmod(log_path, 0o600)

    print(f"Key: {verified.get('fieldKey')}")
    print(f"Before: {before or '[blank]'}")
    print(f"After: {after}")
    print(f"Changed: {'yes' if changed else 'no (already matched)'}")
    print("Verified by GET read-back: yes")
    print(f"Audit log: {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
