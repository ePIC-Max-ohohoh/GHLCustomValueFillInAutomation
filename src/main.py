from __future__ import annotations

import argparse
import getpass
import sys
from pathlib import Path

from dotenv import load_dotenv

from .config import ConfigurationError, load_config
from .csv_loader import CSVValidationError, load_advisors, select_advisors
from .ghl_client import GHLClient
from .oauth import OAuthError, OAuthManager
from .reporter import render_console, write_reports
from .updater import process_batch
from .vimeo_sync import VimeoSyncError, render_sync_report, run_vimeo_sync


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely update existing HighLevel Custom Values from an advisor CSV."
    )
    parser.add_argument("--csv", default=str(PROJECT_ROOT / "data" / "advisors.csv"))
    parser.add_argument("--config-dir", default=str(PROJECT_ROOT / "config"))
    parser.add_argument("--log-dir", default=str(PROJECT_ROOT / "logs"))
    parser.add_argument("--advisor", help="Exact record_name to process")
    parser.add_argument("--location", help="Exact GHL Location ID to process")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply an explicitly selected supported operation; otherwise dry-run.",
    )
    parser.add_argument(
        "--sync-vimeo",
        action="store_true",
        help="Sync mapped Vimeo videos to existing live GHL Custom Values.",
    )
    parser.add_argument(
        "--initialize-oauth",
        action="store_true",
        help="Securely prompt for a one-time agency authorization code and store rotated tokens.",
    )
    parser.add_argument(
        "--list-installed-locations",
        action="store_true",
        help="List sub-accounts where the OAuth app is installed, then exit.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    try:
        if args.sync_vimeo:
            if not args.advisor:
                raise VimeoSyncError("--sync-vimeo requires --advisor")
            report, report_path = run_vimeo_sync(
                advisor=args.advisor,
                location_id=args.location,
                apply=args.apply,
                config_path=Path(args.config_dir) / "vimeo_mappings.yml",
                log_dir=args.log_dir,
            )
            print(render_sync_report(report))
            print(f"\nReport: {report_path}")
            return 1 if report.errors else 0
        if args.apply:
            raise OAuthError(
                "Custom Value writes are disabled in this build. Run the read-only "
                "connectivity test with: python -m src.connectivity"
            )
        if args.initialize_oauth:
            oauth = OAuthManager.from_env()
            code = getpass.getpass("Paste the HighLevel agency authorization code: ").strip()
            if not code:
                raise OAuthError("Authorization code cannot be blank")
            oauth.exchange_authorization_code(code, user_type="Company")
            print("Agency OAuth initialized. Tokens were stored securely and were not displayed.")
            return 0
        if args.list_installed_locations:
            oauth = OAuthManager.from_env()
            locations = oauth.get_installed_locations()
            for item in locations:
                location_id = item.get("locationId") or item.get("id") or "[unknown id]"
                name = item.get("name") or item.get("locationName") or "[unnamed]"
                print(f"{location_id}\t{name}")
            print(f"Installed locations: {len(locations)}")
            return 0
        config = load_config(args.config_dir)
        advisors = select_advisors(load_advisors(args.csv), args.advisor, args.location)
        oauth = OAuthManager.from_env()
        client = GHLClient(oauth)
        results = process_batch(advisors, client, config, args.apply)
        report = render_console(results)
        print(report)
        json_path, csv_path = write_reports(results, args.log_dir, oauth.redactor)
        print(f"\nReports: {json_path} and {csv_path}")
        return 1 if any(result.counts.get("api_error", 0) for result in results) else 0
    except (ConfigurationError, CSVValidationError, OAuthError, VimeoSyncError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
