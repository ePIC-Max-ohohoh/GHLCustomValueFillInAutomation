from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from .connectivity import AgencyConnectivityClient
from .ghl_client import GHLClient
from .oauth import OAuthManager
from .rename_vimeo_custom_values import _wait_for_state, canonical_key
from .vimeo_folder_videos import (
    VimeoConnectivityClient,
    VimeoFolderSummary,
    VimeoVideoRecord,
    collect_folder_videos,
)
from .vimeo_sync import (
    VimeoSyncConfig,
    classify_video,
    generated_player_url,
    load_vimeo_sync_config,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class FillItem:
    advisor: str
    location_id: str
    custom_value_id: str
    key: str
    name: str
    video_title: str
    video_id: str
    url: str
    status: str
    reason: str = ""


def _index(values: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for value in values:
        result[canonical_key(value.get("fieldKey"))].append(value)
    return result


def plan_account(
    advisor: str,
    location_id: str,
    values: list[dict[str, Any]],
    videos: list[VimeoVideoRecord],
    config: VimeoSyncConfig,
) -> list[FillItem]:
    live_by_key = _index(values)
    target_keys = {rule.key for rule in config.sequenced}
    video_by_key: dict[str, list[VimeoVideoRecord]] = defaultdict(list)
    for video in videos:
        category, key = classify_video(video, advisor, config)
        if category == "sequenced" and key in target_keys:
            video_by_key[str(key)].append(video)

    items: list[FillItem] = []
    for rule in config.sequenced:
        live_matches = live_by_key.get(rule.key, [])
        if not live_matches:
            continue
        if len(live_matches) != 1:
            items.append(
                FillItem(advisor, location_id, "", rule.key, "", "", "", "", "skipped", "duplicate live key")
            )
            continue
        live = live_matches[0]
        live_value = str(live.get("value") or "")
        matches = video_by_key.get(rule.key, [])
        common = {
            "advisor": advisor,
            "location_id": location_id,
            "custom_value_id": str(live.get("id") or ""),
            "key": rule.key,
            "name": str(live.get("name") or ""),
        }
        if live_value.strip():
            items.append(FillItem(**common, video_title="", video_id="", url="", status="nonempty_skipped", reason="existing value is non-empty"))
        elif len(matches) == 0:
            items.append(FillItem(**common, video_title="", video_id="", url="", status="skipped", reason="matching Vimeo video not found"))
        elif len(matches) > 1:
            items.append(FillItem(**common, video_title="", video_id="", url="", status="skipped", reason="multiple Vimeo videos matched"))
        else:
            video = matches[0]
            items.append(
                FillItem(
                    **common,
                    video_title=video.video_name,
                    video_id=video.video_id,
                    url=generated_player_url(video.video_id),
                    status="would_fill",
                )
            )
    return items


def _folder_index(folders: list[VimeoFolderSummary]) -> dict[str, list[VimeoFolderSummary]]:
    result: dict[str, list[VimeoFolderSummary]] = defaultdict(list)
    for folder in folders:
        result[folder.name.strip().casefold()].append(folder)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Fill only blank semantic Vimeo Custom Values across exact advisor accounts"
    )
    parser.add_argument(
        "--config", default=str(PROJECT_ROOT / "config/vimeo_mappings.yml")
    )
    parser.add_argument("--log-dir", default=str(PROJECT_ROOT / "logs"))
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")

    config = load_vimeo_sync_config(args.config)
    target_keys = {rule.key for rule in config.sequenced}
    locations = AgencyConnectivityClient(
        agency_token=os.getenv("GHL_AGENCY_TOKEN", ""),
        company_id=os.getenv("GHL_COMPANY_ID"),
    ).list_locations()
    vimeo = VimeoConnectivityClient(os.getenv("VIMEO_ACCESS_TOKEN", ""))
    account, folder_uri = vimeo.get_authenticated_account()
    owner_id = vimeo._user_id_from_uri(account.uri)
    if not owner_id:
        raise RuntimeError("Vimeo owner user ID is unavailable")
    folders = _folder_index(vimeo.list_folders(folder_uri))
    client = GHLClient(OAuthManager.from_env())
    audit_logger = logging.getLogger("batch_fill_vimeo.read_only")
    all_items: list[FillItem] = []
    skipped_accounts: list[dict[str, str]] = []

    for location in locations:
        try:
            values = client.get_custom_values(location.location_id)
        except Exception as exc:
            skipped_accounts.append({"advisor": location.name, "reason": str(exc)})
            continue
        live_keys = set(_index(values))
        if not (live_keys & target_keys):
            continue
        folder_matches = folders.get(location.name.strip().casefold(), [])
        if len(folder_matches) != 1:
            reason = "no exact Vimeo folder" if not folder_matches else "ambiguous Vimeo folders"
            skipped_accounts.append({"advisor": location.name, "reason": reason})
            continue
        videos, errors = collect_folder_videos(
            vimeo, owner_id, location.name, folder_matches[0], audit_logger
        )
        if errors:
            skipped_accounts.append({"advisor": location.name, "reason": "; ".join(errors)})
            continue
        all_items.extend(
            plan_account(location.name, location.location_id, values, videos, config)
        )

    if args.apply:
        for item in all_items:
            if item.status != "would_fill":
                continue
            try:
                current_values = client.get_custom_values(item.location_id)
                current_matches = [
                    value
                    for value in current_values
                    if str(value.get("id") or "") == item.custom_value_id
                    and canonical_key(value.get("fieldKey")) == item.key
                ]
                if len(current_matches) != 1:
                    item.status = "skipped"
                    item.reason = "live record changed before write"
                    continue
                current = current_matches[0]
                if str(current.get("value") or "").strip():
                    item.status = "nonempty_skipped"
                    item.reason = "value became non-empty before write"
                    continue
                client.update_custom_value(
                    item.location_id,
                    item.custom_value_id,
                    str(current.get("name") or item.name),
                    item.url,
                )
                _wait_for_state(
                    client,
                    item.location_id,
                    item.custom_value_id,
                    lambda live: canonical_key(live.get("fieldKey")) == item.key
                    and str(live.get("value") or "") == item.url,
                    f"Vimeo URL for {item.advisor}/{item.key}",
                )
                item.status = "filled_and_verified"
            except Exception as exc:
                item.status = "failed"
                item.reason = str(exc)

    for item in all_items:
        print(
            f"{item.advisor}\t{item.key}\tstatus={item.status}"
            + (f"\treason={item.reason}" if item.reason else "")
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = Path(args.log_dir) / f"batch-fill-vimeo-values-{stamp}.json"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        json.dumps(
            {
                "timestamp": stamp,
                "mode": "apply" if args.apply else "dry-run",
                "policy": "fill blank only; non-empty values skipped",
                "items": [
                    {
                        "advisor": item.advisor,
                        "locationId": item.location_id,
                        "key": item.key,
                        "videoTitle": item.video_title,
                        "videoId": item.video_id,
                        "status": item.status,
                        "reason": item.reason,
                    }
                    for item in all_items
                ],
                "skippedAccounts": skipped_accounts,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    os.chmod(log_path, 0o600)
    counts: dict[str, int] = defaultdict(int)
    for item in all_items:
        counts[item.status] += 1
    print("SUMMARY " + " ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"Audit log: {log_path}")
    return 1 if counts.get("failed") else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
