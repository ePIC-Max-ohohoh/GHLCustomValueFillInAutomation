from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from .ghl_client import GHLClient
from .oauth import OAuthManager


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class RenameError(RuntimeError):
    pass


@dataclass(frozen=True)
class RenameRule:
    old_key: str
    new_key: str
    name: str


@dataclass(frozen=True)
class RenameItem:
    rule: RenameRule
    custom_value_id: str
    old_name: str
    value: str


def canonical_key(value: object) -> str:
    text = str(value or "").strip()
    if text.startswith("{{") and text.endswith("}}"):
        text = text[2:-2]
    text = "".join(text.split())
    return text.removeprefix("custom_values.")


def load_rules(path: str | Path) -> list[RenameRule]:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    rows = raw.get("renames")
    if not isinstance(rows, list) or not rows:
        raise RenameError("Rename configuration requires a non-empty renames list")
    rules: list[RenameRule] = []
    for row in rows:
        if not isinstance(row, dict):
            raise RenameError("Every rename must be a mapping")
        old_key = str(row.get("old_key") or "").strip()
        new_key = str(row.get("new_key") or "").strip()
        name = str(row.get("name") or "").strip()
        if not re.fullmatch(r"[a-z0-9_]+", old_key) or not re.fullmatch(
            r"[a-z0-9_]+", new_key
        ):
            raise RenameError("Rename keys must use lowercase letters, digits, and underscores")
        if not name:
            raise RenameError("Every rename requires a display name")
        rules.append(RenameRule(old_key, new_key, name))
    if len({rule.old_key for rule in rules}) != len(rules):
        raise RenameError("Duplicate source key in rename configuration")
    if len({rule.new_key for rule in rules}) != len(rules):
        raise RenameError("Duplicate destination key in rename configuration")
    return rules


def build_plan(values: list[dict[str, Any]], rules: list[RenameRule]) -> list[RenameItem]:
    by_key: dict[str, list[dict[str, Any]]] = {}
    for value in values:
        by_key.setdefault(canonical_key(value.get("fieldKey")), []).append(value)
    collisions = [rule.new_key for rule in rules if by_key.get(rule.new_key)]
    if collisions:
        raise RenameError(f"Destination keys already exist: {', '.join(collisions)}")
    plan: list[RenameItem] = []
    for rule in rules:
        matches = by_key.get(rule.old_key, [])
        if len(matches) != 1:
            raise RenameError(f"Source key {rule.old_key} matched {len(matches)} records")
        live = matches[0]
        custom_value_id = str(live.get("id") or "")
        old_name = str(live.get("name") or "")
        if not custom_value_id or not old_name:
            raise RenameError(f"Source key {rule.old_key} lacks id or name")
        plan.append(
            RenameItem(rule, custom_value_id, old_name, str(live.get("value") or ""))
        )
    return plan


def _get_by_id(client: GHLClient, location_id: str, custom_value_id: str) -> dict[str, Any]:
    for attempt in range(5):
        matches = [
            item
            for item in client.get_custom_values(location_id)
            if str(item.get("id") or "") == custom_value_id
        ]
        if len(matches) == 1:
            return matches[0]
        if attempt < 4:
            time.sleep(0.5 * (2**attempt))
    raise RenameError(f"Custom Value ID disappeared during verification: {custom_value_id}")


def _wait_for_state(
    client: GHLClient,
    location_id: str,
    custom_value_id: str,
    predicate: Any,
    description: str,
) -> dict[str, Any]:
    last: dict[str, Any] | None = None
    for attempt in range(10):
        matches = [
            item
            for item in client.get_custom_values(location_id)
            if str(item.get("id") or "") == custom_value_id
        ]
        if len(matches) == 1:
            last = matches[0]
            if predicate(last):
                return last
        if attempt < 9:
            time.sleep(0.75)
    raise RenameError(f"Timed out waiting for {description}")


def _wait_until_deleted(client: GHLClient, location_id: str, custom_value_id: str) -> None:
    for attempt in range(10):
        remaining = [
            item
            for item in client.get_custom_values(location_id)
            if str(item.get("id") or "") == custom_value_id
        ]
        if not remaining:
            return
        if attempt < 9:
            time.sleep(0.75)
    raise RenameError("newly created record still exists")


def _verify(item: RenameItem, live: dict[str, Any], restored: bool = False) -> None:
    expected_name = item.old_name if restored else item.rule.name
    expected_key = item.rule.old_key if restored else item.rule.new_key
    if str(live.get("name") or "") != expected_name:
        raise RenameError(f"Name verification failed for {item.rule.old_key}")
    actual_key = canonical_key(live.get("fieldKey"))
    if actual_key != expected_key:
        raise RenameError(
            f"Key verification failed for {item.rule.old_key}: "
            f"expected {expected_key}, received {actual_key}"
        )
    if str(live.get("value") or "") != item.value:
        raise RenameError(f"Value preservation failed for {item.rule.old_key}")


def apply_plan(client: GHLClient, location_id: str, plan: list[RenameItem]) -> list[dict[str, str]]:
    changed: list[RenameItem] = []
    results: list[dict[str, str]] = []
    try:
        for item in plan:
            client.update_custom_value(
                location_id, item.custom_value_id, item.rule.name, item.value
            )
            changed.append(item)
            live = _get_by_id(client, location_id, item.custom_value_id)
            _verify(item, live)
            results.append(
                {
                    "old_key": item.rule.old_key,
                    "new_key": item.rule.new_key,
                    "name": item.rule.name,
                    "status": "updated_and_verified",
                }
            )
        return results
    except Exception as exc:
        rollback_errors: list[str] = []
        for item in reversed(changed):
            try:
                client.update_custom_value(
                    location_id, item.custom_value_id, item.old_name, item.value
                )
                _verify(item, _get_by_id(client, location_id, item.custom_value_id), restored=True)
            except Exception as rollback_exc:
                rollback_errors.append(f"{item.rule.old_key}: {rollback_exc}")
        rollback_status = "rollback verified" if not rollback_errors else (
            "ROLLBACK ERRORS: " + "; ".join(rollback_errors)
        )
        raise RenameError(f"Migration stopped: {exc}; {rollback_status}") from exc


def create_replacements(
    client: GHLClient, location_id: str, plan: list[RenameItem]
) -> list[dict[str, str]]:
    created: list[tuple[RenameItem, str]] = []
    results: list[dict[str, str]] = []
    try:
        for item in plan:
            seed_name = item.rule.new_key.replace("_", " ")
            response = client.create_custom_value(location_id, seed_name, item.value)
            payload = response.get("customValue", {})
            custom_value_id = str(payload.get("id") or "")
            if not custom_value_id:
                raise RenameError(f"Create response lacked id for {item.rule.new_key}")
            created.append((item, custom_value_id))
            _wait_for_state(
                client,
                location_id,
                custom_value_id,
                lambda live: canonical_key(live.get("fieldKey")) == item.rule.new_key
                and str(live.get("value") or "") == item.value,
                f"seeded key/value {item.rule.new_key}",
            )
            client.update_custom_value(
                location_id, custom_value_id, item.rule.name, item.value
            )
            _wait_for_state(
                client,
                location_id,
                custom_value_id,
                lambda live: str(live.get("name") or "") == item.rule.name
                and canonical_key(live.get("fieldKey")) == item.rule.new_key
                and str(live.get("value") or "") == item.value,
                f"final name/key/value {item.rule.new_key}",
            )
            results.append(
                {
                    "source_key": item.rule.old_key,
                    "new_key": item.rule.new_key,
                    "name": item.rule.name,
                    "status": "created_and_verified",
                }
            )
        return results
    except Exception as exc:
        rollback_errors: list[str] = []
        for item, custom_value_id in reversed(created):
            try:
                client.delete_custom_value(location_id, custom_value_id)
                _wait_until_deleted(client, location_id, custom_value_id)
            except Exception as rollback_exc:
                rollback_errors.append(f"{item.rule.new_key}: {rollback_exc}")
        rollback_status = "new replacements rolled back" if not rollback_errors else (
            "ROLLBACK ERRORS: " + "; ".join(rollback_errors)
        )
        raise RenameError(f"Replacement creation stopped: {exc}; {rollback_status}") from exc


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rename Barry's existing Vimeo Custom Values")
    parser.add_argument("--location", required=True)
    parser.add_argument(
        "--config", default=str(PROJECT_ROOT / "config/vimeo_custom_value_renames.yml")
    )
    parser.add_argument("--log-dir", default=str(PROJECT_ROOT / "logs"))
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--create-replacements", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    client = GHLClient(OAuthManager.from_env())
    rules = load_rules(args.config)
    plan = build_plan(client.get_custom_values(args.location), rules)
    if not args.apply and not args.create_replacements:
        for item in plan:
            print(f"{item.rule.old_key} -> {item.rule.new_key} | {item.rule.name}")
        print(f"DRY RUN — {len(plan)} records validated; no changes made")
        return 0
    results = (
        create_replacements(client, args.location, plan)
        if args.create_replacements
        else apply_plan(client, args.location, plan)
    )
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path(args.log_dir) / f"vimeo-custom-value-renames-{stamp}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "timestamp": stamp,
                "locationId": args.location,
                "valuePolicy": "preserved exactly; values omitted from audit",
                "results": results,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    os.chmod(log_path, 0o600)
    action = "CREATED" if args.create_replacements else "UPDATED"
    print(f"{action} AND VERIFIED: {len(results)} Custom Values")
    print(f"Audit log: {log_path}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RenameError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
