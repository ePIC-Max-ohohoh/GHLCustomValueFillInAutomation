from __future__ import annotations

import argparse
import csv
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv

from .oauth import OAuthError, OAuthManager


BASE_URL = "https://services.leadconnectorhq.com"
API_VERSION = "2021-07-28"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _get_collection(
    session: requests.Session,
    token: str,
    location_id: str,
    resource: str,
) -> list[dict[str, Any]]:
    response = session.get(
        f"{BASE_URL}/locations/{location_id}/{resource}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Version": API_VERSION,
        },
        timeout=30,
    )
    if response.status_code >= 400:
        raise OAuthError(f"GET {resource} failed with HTTP {response.status_code}")
    payload = response.json()
    items = payload.get(resource, [])
    if not isinstance(items, list):
        raise OAuthError(f"GET {resource} returned an unexpected response shape")
    return items


def _write_csv(path: Path, items: list[dict[str, Any]]) -> None:
    columns = sorted({key for item in items for key in item})
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for item in items:
            writer.writerow(
                {
                    key: json.dumps(value, ensure_ascii=False)
                    if isinstance(value, (dict, list))
                    else value
                    for key, value in item.items()
                }
            )
    os.chmod(path, 0o600)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read a location's Custom Fields and Custom Values without modifying data."
    )
    parser.add_argument("--location", required=True)
    parser.add_argument("--name", default="location")
    parser.add_argument("--output-dir", default=str(PROJECT_ROOT / "logs"))
    args = parser.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    oauth = OAuthManager.from_env()
    token = oauth.get_location_token(args.location)
    session = requests.Session()
    custom_fields = _get_collection(session, token, args.location, "customFields")
    custom_values = _get_collection(session, token, args.location, "customValues")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    safe_name = "".join(char.lower() if char.isalnum() else "-" for char in args.name)
    safe_name = "-".join(part for part in safe_name.split("-") if part) or "location"
    base = output_dir / f"{safe_name}-custom-data-{stamp}"

    json_path = base.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {
                "locationName": args.name,
                "locationId": args.location,
                "customFields": custom_fields,
                "customValues": custom_values,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.chmod(json_path, 0o600)

    fields_path = Path(f"{base}-fields.csv")
    values_path = Path(f"{base}-values.csv")
    _write_csv(fields_path, custom_fields)
    _write_csv(values_path, custom_values)

    print(f"Custom Fields: {len(custom_fields)}")
    print(f"Custom Values: {len(custom_values)}")
    print(f"JSON report: {json_path}")
    print(f"Custom Fields CSV: {fields_path}")
    print(f"Custom Values CSV: {values_path}")
    print("READ ONLY — no HighLevel data was modified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
