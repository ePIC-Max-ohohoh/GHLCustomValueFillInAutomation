from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .connectivity import AgencyConnectivityClient
from .ghl_client import GHLClient
from .oauth import OAuthManager
from .rename_vimeo_custom_values import (
    RenameItem,
    RenameRule,
    canonical_key,
    create_replacements,
    load_rules,
)
from .vimeo_folder_videos import VimeoConnectivityClient


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class AccountPlan:
    name: str
    location_id: str
    creates: list[RenameItem] = field(default_factory=list)
    existing: int = 0


def _exact_index(values: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        index.setdefault(canonical_key(value.get("fieldKey")), []).append(value)
    return index


def plan_account(
    name: str,
    location_id: str,
    values: list[dict[str, Any]],
    rules: list[RenameRule],
) -> AccountPlan:
    index = _exact_index(values)
    plan = AccountPlan(name=name, location_id=location_id)
    for rule in rules:
        destinations = index.get(rule.new_key, [])
        if len(destinations) > 1:
            raise RuntimeError(f"duplicate destination key {rule.new_key}")
        if destinations:
            if str(destinations[0].get("name") or "") != rule.name:
                raise RuntimeError(f"destination name mismatch for {rule.new_key}")
            plan.existing += 1
            continue
        sources = index.get(rule.old_key, [])
        if len(sources) != 1:
            raise RuntimeError(f"source key {rule.old_key} matched {len(sources)} records")
        source = sources[0]
        source_id = str(source.get("id") or "")
        source_name = str(source.get("name") or "")
        if not source_id or not source_name:
            raise RuntimeError(f"source key {rule.old_key} lacks id or name")
        plan.creates.append(
            RenameItem(
                rule=rule,
                custom_value_id=source_id,
                old_name=source_name,
                value=str(source.get("value") or ""),
            )
        )
    return plan


def discover_exact_accounts() -> tuple[list[tuple[str, str]], list[dict[str, str]]]:
    locations = AgencyConnectivityClient(
        agency_token=os.getenv("GHL_AGENCY_TOKEN", ""),
        company_id=os.getenv("GHL_COMPANY_ID"),
    ).list_locations()
    vimeo = VimeoConnectivityClient(os.getenv("VIMEO_ACCESS_TOKEN", ""))
    _, folder_uri = vimeo.get_authenticated_account()
    folders = vimeo.list_folders(folder_uri)
    by_name: dict[str, list[str]] = {}
    for folder in folders:
        by_name.setdefault(folder.name.strip().casefold(), []).append(folder.folder_id)
    matched: list[tuple[str, str]] = []
    skipped: list[dict[str, str]] = []
    for location in locations:
        if location.name.strip().casefold() == "barry goldwater":
            skipped.append({"name": location.name, "reason": "excluded completed account"})
            continue
        folders_for_name = by_name.get(location.name.strip().casefold(), [])
        if len(folders_for_name) == 1:
            matched.append((location.name, location.location_id))
        elif len(folders_for_name) > 1:
            skipped.append({"name": location.name, "reason": "ambiguous Vimeo folders"})
        else:
            skipped.append({"name": location.name, "reason": "no exact Vimeo folder"})
    return matched, skipped


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Create semantic Vimeo Custom Values for exact advisor accounts"
    )
    parser.add_argument(
        "--config", default=str(PROJECT_ROOT / "config/vimeo_custom_value_renames.yml")
    )
    parser.add_argument("--log-dir", default=str(PROJECT_ROOT / "logs"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    rules = load_rules(args.config)
    matched, skipped = discover_exact_accounts()
    client = GHLClient(OAuthManager.from_env())
    plans: list[AccountPlan] = []
    for name, location_id in matched:
        try:
            values = client.get_custom_values(location_id)
            plans.append(plan_account(name, location_id, values, rules))
        except Exception as exc:
            skipped.append({"name": name, "reason": str(exc)})

    results: list[dict[str, Any]] = []
    for plan in plans:
        status = "already_complete" if not plan.creates else "would_create"
        if args.apply and plan.creates:
            try:
                created = create_replacements(client, plan.location_id, plan.creates)
                status = "created_and_verified"
                created_count = len(created)
            except Exception as exc:
                status = "failed"
                created_count = 0
                skipped.append({"name": plan.name, "reason": str(exc)})
        else:
            created_count = 0
        results.append(
            {
                "name": plan.name,
                "locationId": plan.location_id,
                "existing": plan.existing,
                "planned": len(plan.creates),
                "created": created_count,
                "status": status,
            }
        )
        print(
            f"{plan.name}\tlocation={plan.location_id}\texisting={plan.existing}"
            f"\t{'created' if args.apply else 'would_create'}={created_count if args.apply else len(plan.creates)}"
            f"\tstatus={status}"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path(args.log_dir) / f"batch-vimeo-custom-values-{stamp}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "timestamp": stamp,
                "mode": "apply" if args.apply else "dry-run",
                "scope": "exact GHL/Vimeo name intersection; Barry excluded",
                "valuePolicy": "copied from matching legacy key; values omitted from audit",
                "accounts": results,
                "skipped": skipped,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.chmod(log_path, 0o600)
    total = sum(item["created"] if args.apply else item["planned"] for item in results)
    print(f"TOTAL {'CREATED' if args.apply else 'WOULD CREATE'}: {total}")
    print(f"ELIGIBLE ACCOUNTS: {len(plans)}")
    print(f"SKIPPED ACCOUNTS: {len(skipped)}")
    print(f"Audit log: {log_path}")
    return 1 if any(item["status"] == "failed" for item in results) else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
