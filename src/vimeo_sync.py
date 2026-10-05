from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from .connectivity import AgencyConnectivityClient, ConnectivityError
from .ghl_client import GHLAPIError, GHLClient
from .matcher import CustomValueIndex, LiveCustomValue
from .oauth import OAuthManager
from .vimeo_folder_videos import (
    VimeoConnectivityClient,
    VimeoConnectivityError,
    VimeoVideoRecord,
    collect_folder_videos,
    match_advisor_folders,
)


SEQUENCE_RE = re.compile(r"^\s*(\d+)\.\s+.+")


class VimeoSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class NamedVideoRule:
    key: str
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class VimeoSyncConfig:
    named: tuple[NamedVideoRule, ...]
    sequenced: tuple[NamedVideoRule, ...] = ()


@dataclass
class VimeoSyncItem:
    category: str
    title: str
    video_id: str
    target_key: str | None
    generated_url: str
    status: str
    current_value: str | None = None
    custom_value_id: str | None = None
    reason: str = ""


@dataclass
class VimeoSyncReport:
    advisor: str
    location_id: str
    apply: bool
    items: list[VimeoSyncItem] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def counts(self) -> dict[str, int]:
        return {
            "named_matched": sum(item.category == "named" for item in self.items),
            "sequenced_matched": sum(item.category == "sequenced" for item in self.items),
            "would_update": sum(item.status == "would_update" for item in self.items),
            "updated": sum(item.status == "updated" for item in self.items),
            "unchanged": sum(item.status == "unchanged" for item in self.items),
            "missing_ghl_keys": sum(item.status == "no_target_key" for item in self.items),
            "unmapped_vimeo_videos": sum(item.category == "unmapped" for item in self.items),
        }


def load_vimeo_sync_config(path: str | Path) -> VimeoSyncConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise VimeoSyncError(f"Vimeo mapping configuration not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise VimeoSyncError(f"Unable to read Vimeo mapping configuration: {config_path}") from exc
    named = raw.get("named")
    if not isinstance(named, dict) or not named:
        raise VimeoSyncError("Vimeo mapping configuration must contain named mappings")
    rules: list[NamedVideoRule] = []
    sequence_rules: list[NamedVideoRule] = []
    aliases_seen: dict[str, str] = {}
    for key, value in named.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key):
            raise VimeoSyncError("Vimeo target keys must contain lowercase letters, digits, or underscores")
        aliases = value.get("aliases") if isinstance(value, dict) else None
        if not isinstance(aliases, list) or not aliases or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise VimeoSyncError(f"Named Vimeo mapping {key!r} requires non-empty aliases")
        cleaned = tuple(alias.strip() for alias in aliases)
        for alias in cleaned:
            normalized = alias.casefold()
            if normalized in aliases_seen:
                raise VimeoSyncError(
                    f"Duplicate Vimeo alias {alias!r} for {aliases_seen[normalized]!r} and {key!r}"
                )
            aliases_seen[normalized] = key
        rules.append(NamedVideoRule(key=key, aliases=cleaned))
    sequenced = raw.get("sequenced", {})
    if not isinstance(sequenced, dict):
        raise VimeoSyncError("Vimeo sequenced mappings must be a mapping")
    for key, value in sequenced.items():
        if not isinstance(key, str) or not re.fullmatch(r"[a-z0-9_]+", key):
            raise VimeoSyncError("Vimeo target keys must contain lowercase letters, digits, or underscores")
        aliases = value.get("aliases") if isinstance(value, dict) else None
        if not isinstance(aliases, list) or not aliases or not all(
            isinstance(alias, str) and alias.strip() for alias in aliases
        ):
            raise VimeoSyncError(f"Sequenced Vimeo mapping {key!r} requires non-empty aliases")
        cleaned = tuple(alias.strip() for alias in aliases)
        for alias in cleaned:
            normalized = alias.casefold()
            if normalized in aliases_seen:
                raise VimeoSyncError(
                    f"Duplicate Vimeo alias {alias!r} for {aliases_seen[normalized]!r} and {key!r}"
                )
            aliases_seen[normalized] = key
        sequence_rules.append(NamedVideoRule(key=key, aliases=cleaned))
    return VimeoSyncConfig(named=tuple(rules), sequenced=tuple(sequence_rules))


def _named_target(title: str, advisor: str, config: VimeoSyncConfig) -> str | None:
    candidate = title.strip().casefold()
    for rule in config.named:
        for alias in rule.aliases:
            if candidate == alias.format(advisor=advisor).strip().casefold():
                return rule.key
    return None


def _sequenced_target(title: str, config: VimeoSyncConfig) -> str | None:
    candidate = title.strip().casefold()
    for rule in config.sequenced:
        if any(candidate == alias.strip().casefold() for alias in rule.aliases):
            return rule.key
    return None


def classify_video(
    video: VimeoVideoRecord, advisor: str, config: VimeoSyncConfig
) -> tuple[str, str | None]:
    named_key = _named_target(video.video_name, advisor, config)
    if named_key:
        return "named", named_key
    sequenced_key = _sequenced_target(video.video_name, config)
    if sequenced_key:
        return "sequenced", sequenced_key
    match = SEQUENCE_RE.match(video.video_name)
    if match:
        number = int(match.group(1))
        if number > 0:
            return "sequenced", f"advisorvimeovideo{number}"
    return "unmapped", None


def generated_player_url(video_id: str) -> str:
    if not video_id.isdigit():
        raise VimeoSyncError(f"Invalid Vimeo video ID: {video_id!r}")
    return (
        f"https://player.vimeo.com/video/{video_id}"
        "?title=0&byline=0&portrait=0&autoplay=1"
    )


def _exact_live_value(index: CustomValueIndex, key: str) -> LiveCustomValue | None:
    matches = index.by_key.get(key, [])
    return matches[0] if len(matches) == 1 else None


def build_sync_report(
    advisor: str,
    location_id: str,
    videos: list[VimeoVideoRecord],
    live_values: list[dict[str, Any]],
    config: VimeoSyncConfig,
    apply: bool = False,
) -> VimeoSyncReport:
    report = VimeoSyncReport(advisor=advisor, location_id=location_id, apply=apply)
    index = CustomValueIndex(live_values)
    target_counts: dict[str, int] = {}
    classifications: list[tuple[VimeoVideoRecord, str, str | None]] = []
    for video in videos:
        category, target_key = classify_video(video, advisor, config)
        classifications.append((video, category, target_key))
        if target_key:
            target_counts[target_key] = target_counts.get(target_key, 0) + 1

    for video, category, target_key in classifications:
        url = generated_player_url(video.video_id)
        if category == "unmapped" or target_key is None:
            report.items.append(
                VimeoSyncItem(
                    category="unmapped",
                    title=video.video_name,
                    video_id=video.video_id,
                    target_key=None,
                    generated_url=url,
                    status="unmapped",
                    reason="no named alias or numeric prefix matched",
                )
            )
            continue
        if target_counts[target_key] > 1:
            report.items.append(
                VimeoSyncItem(
                    category=category,
                    title=video.video_name,
                    video_id=video.video_id,
                    target_key=target_key,
                    generated_url=url,
                    status="duplicate_target",
                    reason="multiple Vimeo videos map to the same target key",
                )
            )
            continue
        matches = index.by_key.get(target_key, [])
        live = _exact_live_value(index, target_key)
        if live is None:
            reason = "exact Custom Value does not exist"
            if len(matches) > 1:
                reason = "duplicate live Custom Value fieldKey"
            report.items.append(
                VimeoSyncItem(
                    category=category,
                    title=video.video_name,
                    video_id=video.video_id,
                    target_key=target_key,
                    generated_url=url,
                    status="no_target_key",
                    reason=reason,
                )
            )
            continue
        status = "unchanged" if live.value == url else ("updated" if apply else "would_update")
        report.items.append(
            VimeoSyncItem(
                category=category,
                title=video.video_name,
                video_id=video.video_id,
                target_key=target_key,
                generated_url=url,
                status=status,
                current_value=live.value,
                custom_value_id=live.id,
            )
        )
    return report


def render_sync_report(report: VimeoSyncReport) -> str:
    lines = [f"Advisor: {report.advisor}", "", "NAMED VIDEOS", ""]
    status_labels = {
        "unchanged": "UNCHANGED",
        "would_update": "WOULD UPDATE",
        "updated": "UPDATED",
        "no_target_key": "NO TARGET KEY — SKIPPED",
        "duplicate_target": "DUPLICATE TARGET — SKIPPED",
    }
    for category, heading in (("named", None), ("sequenced", "SEQUENCED VIDEOS")):
        if heading:
            lines.extend([heading, ""])
        items = [item for item in report.items if item.category == category]
        if not items:
            lines.extend(["[none]", ""])
            continue
        for item in items:
            lines.append(item.title)
            lines.append(f"→ {item.target_key}")
            lines.append(f"→ Vimeo ID: {item.video_id}")
            if item.current_value is not None:
                lines.append(f"→ Current GHL value: {item.current_value or '[blank]'}")
            lines.append(f"→ Proposed GHL value: {item.generated_url}")
            lines.append(f"→ {status_labels.get(item.status, item.status.upper())}")
            lines.append("")

    lines.extend(["UNMAPPED VIDEOS", ""])
    unmapped = [item for item in report.items if item.category == "unmapped"]
    if not unmapped:
        lines.extend(["[none]", ""])
    else:
        for item in unmapped:
            lines.extend([item.title, "→ UNMAPPED — SKIPPED", ""])

    counts = report.counts
    lines.extend(
        [
            "SUMMARY",
            "",
            f"Named matched: {counts['named_matched']}",
            f"Sequenced matched: {counts['sequenced_matched']}",
            f"Would update: {counts['would_update']}",
            f"Updated: {counts['updated']}",
            f"Unchanged: {counts['unchanged']}",
            f"Missing GHL keys: {counts['missing_ghl_keys']}",
            f"Unmapped Vimeo videos: {counts['unmapped_vimeo_videos']}",
        ]
    )
    if report.errors:
        lines.extend(["", "ERRORS", ""] + [f"- {error}" for error in report.errors])
    lines.extend(["", "APPLY MODE — CHANGES WRITTEN" if report.apply else "DRY RUN — NO CHANGES MADE"])
    return "\n".join(lines)


def resolve_location_id(advisor: str, explicit_location: str | None = None) -> str:
    if explicit_location:
        return explicit_location
    token = os.getenv("GHL_AGENCY_TOKEN", "")
    try:
        locations = AgencyConnectivityClient(
            agency_token=token,
            company_id=os.getenv("GHL_COMPANY_ID"),
        ).list_locations()
    except ConnectivityError as exc:
        raise VimeoSyncError(f"Unable to resolve advisor Location ID: {exc}") from exc
    matches = [item for item in locations if item.name.strip().casefold() == advisor.strip().casefold()]
    if not matches:
        raise VimeoSyncError(f"No exact GHL sub-account match for advisor: {advisor}")
    if len(matches) > 1:
        raise VimeoSyncError(f"Multiple GHL sub-accounts match advisor {advisor!r}; use --location")
    return matches[0].location_id


def fetch_advisor_videos(advisor: str) -> list[VimeoVideoRecord]:
    client = VimeoConnectivityClient(os.getenv("VIMEO_ACCESS_TOKEN", ""))
    account, folder_uri = client.get_authenticated_account()
    owner_user_id = client._user_id_from_uri(account.uri)
    if not owner_user_id:
        raise VimeoSyncError("Authenticated Vimeo owner ID is unavailable")
    folders = client.list_folders(folder_uri)
    matches, unmatched = match_advisor_folders(folders, [advisor])
    if unmatched or not matches:
        raise VimeoSyncError(f"No exact Vimeo folder match for advisor: {advisor}")
    videos: dict[tuple[str, str], VimeoVideoRecord] = {}
    audit = logging.getLogger("vimeo_sync.read_only")
    errors: list[str] = []
    for _, folder in matches:
        folder_videos, folder_errors = collect_folder_videos(
            client, owner_user_id, advisor, folder, audit
        )
        errors.extend(folder_errors)
        for video in folder_videos:
            videos[(video.video_id, video.folder_id)] = video
    if errors:
        raise VimeoSyncError("; ".join(errors))
    return sorted(videos.values(), key=lambda video: (video.video_name.casefold(), video.video_id))


def run_vimeo_sync(
    advisor: str,
    config_path: str | Path,
    log_dir: str | Path,
    location_id: str | None = None,
    apply: bool = False,
) -> tuple[VimeoSyncReport, Path]:
    resolved_location = resolve_location_id(advisor, location_id)
    videos = fetch_advisor_videos(advisor)
    config = load_vimeo_sync_config(config_path)
    oauth = OAuthManager.from_env()
    client = GHLClient(oauth)
    try:
        live_values = client.get_custom_values(resolved_location)
    except (GHLAPIError, Exception) as exc:
        raise VimeoSyncError(f"Unable to retrieve live GHL Custom Values: {exc}") from exc
    report = build_sync_report(
        advisor, resolved_location, videos, live_values, config, apply=apply
    )

    if apply:
        for item in report.items:
            if item.status != "updated" or not item.custom_value_id or not item.target_key:
                continue
            live = _exact_live_value(CustomValueIndex(live_values), item.target_key)
            if live is None:
                raise VimeoSyncError(f"Target key disappeared before update: {item.target_key}")
            client.update_custom_value(
                resolved_location, item.custom_value_id, live.name, item.generated_url
            )
        verified = CustomValueIndex(client.get_custom_values(resolved_location))
        for item in report.items:
            if item.status != "updated" or not item.target_key:
                continue
            live = _exact_live_value(verified, item.target_key)
            if live is None or live.value != item.generated_url:
                raise VimeoSyncError(f"Read-back verification failed for {item.target_key}")

    text = render_sync_report(report)
    root = Path(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report_path = root / f"vimeo-sync-{advisor.casefold().replace(' ', '-')}-{stamp}.txt"
    report_path.write_text(text + "\n", encoding="utf-8")
    os.chmod(report_path, 0o600)
    return report, report_path
