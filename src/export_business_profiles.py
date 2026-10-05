from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .connectivity import (
    AgencyConnectivityClient,
    ConnectivityError,
    LocationProfile,
    PROJECT_ROOT,
    create_audit_logger,
)


HEADERS = {
    "location_id": "Location ID",
    "friendly_business_name": "Friendly Business Name",
    "legal_business_name": "Legal Business Name",
    "business_email": "Business Email",
    "business_phone": "Business Phone",
    "branded_domain": "Branded Domain",
    "business_website": "Business Website",
    "business_niche": "Business Niche",
    "business_currency": "Business Currency",
    "business_logo_url": "Business Logo URL",
}


def _excel_safe(value: str) -> str:
    """Keep identifiers and phone numbers as text when the CSV is opened in Excel."""
    if value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_excel_csv(profiles: list[LocationProfile], path: str | Path) -> Path:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(HEADERS.values()))
        writer.writeheader()
        for profile in profiles:
            raw = asdict(profile)
            writer.writerow(
                {label: _excel_safe(raw[key]) for key, label in HEADERS.items()}
            )
    return output_path


def _default_output_path() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return PROJECT_ROOT / "output" / f"ghl-business-profiles-{stamp}.csv"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read every HighLevel sub-account Business Profile and save an "
            "Excel-compatible CSV. This command performs GET requests only."
        )
    )
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument("--page-size", type=int, default=100)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.page_size < 1 or args.page_size > 100:
        print("ERROR: --page-size must be between 1 and 100.", file=sys.stderr)
        return 2

    load_dotenv(PROJECT_ROOT / ".env")
    logger, log_path = create_audit_logger(
        os.getenv("GHL_CONNECTIVITY_LOG_DIR", str(PROJECT_ROOT / "logs"))
    )
    logger.info("START business_profile_export method=GET api_version=v3")
    output_path = args.output or _default_output_path()
    try:
        client = AgencyConnectivityClient(
            agency_token=os.getenv("GHL_AGENCY_TOKEN", ""),
            company_id=os.getenv("GHL_COMPANY_ID"),
            audit=logger.info,
        )
        profiles = client.list_location_profiles(page_size=args.page_size)
        saved = write_excel_csv(profiles, output_path)
        logger.info("SAVED_PROFILE_EXPORT total_locations=%s path=%s", len(profiles), saved)
        print(f"Saved {len(profiles)} advisor business profiles to {saved}")
        print(f"Processing log: {log_path}", file=sys.stderr)
        return 0
    except (ConnectivityError, OSError) as exc:
        logger.error("FAILED %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Processing log: {log_path}", file=sys.stderr)
        return 2
    finally:
        for handler in list(logger.handlers):
            if isinstance(handler, logging.FileHandler):
                handler.flush()


if __name__ == "__main__":
    raise SystemExit(main())
