from __future__ import annotations

import argparse
import csv
import logging
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv


BASE_URL = "https://api.vimeo.com"
ACCEPT_HEADER = "application/vnd.vimeo.*+json;version=3.4"
PROJECT_ROOT = Path(__file__).resolve().parents[1]


class VimeoConnectivityError(RuntimeError):
    pass


@dataclass(frozen=True)
class VimeoAccountSummary:
    name: str
    uri: str


@dataclass(frozen=True)
class VimeoFolderSummary:
    name: str
    folder_id: str
    uri: str


def _safe_field(value: str) -> str:
    return " ".join(value.replace("\x00", "").splitlines()).replace("\t", " ").strip()


class VimeoConnectivityClient:
    """Small GET-only Vimeo client used by the folder-video extractor."""

    def __init__(
        self,
        access_token: str,
        session: requests.Session | None = None,
        max_retries: int = 2,
        sleep=time.sleep,
        audit=None,
    ) -> None:
        if not access_token.strip():
            raise VimeoConnectivityError("VIMEO_ACCESS_TOKEN is missing.")
        self._access_token = access_token.strip()
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.sleep = sleep
        self.audit = audit or (lambda _message: None)

    @staticmethod
    def _safe_url(path_or_url: str) -> str:
        url = urljoin(f"{BASE_URL}/", path_or_url)
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.vimeo.com":
            raise VimeoConnectivityError("Vimeo returned an unexpected external URL.")
        return url

    def _get_json(
        self,
        path_or_url: str,
        operation: str,
        params: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = self._safe_url(path_or_url)
        self.audit(f"GET operation={operation} path={urlparse(url).path}")
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    url,
                    headers={
                        "Accept": ACCEPT_HEADER,
                        "Authorization": f"Bearer {self._access_token}",
                    },
                    params=params,
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self.sleep(0.5 * (2**attempt))
                    continue
                raise VimeoConnectivityError("Unable to reach Vimeo after retrying.") from exc
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.max_retries:
                    self.sleep(0.5 * (2**attempt))
                    continue
            if response.status_code == 401:
                raise VimeoConnectivityError("Vimeo authentication failed (HTTP 401).")
            if response.status_code == 403:
                raise VimeoConnectivityError(
                    f"Vimeo denied {operation} (HTTP 403); check folder permissions."
                )
            if response.status_code == 404:
                raise VimeoConnectivityError(
                    f"Vimeo could not find the resource for {operation} (HTTP 404)."
                )
            if response.status_code >= 400:
                raise VimeoConnectivityError(
                    f"Vimeo {operation} failed with HTTP {response.status_code}."
                )
            try:
                payload = response.json()
            except ValueError as exc:
                raise VimeoConnectivityError(
                    f"Vimeo returned invalid JSON for {operation}."
                ) from exc
            if not isinstance(payload, dict):
                raise VimeoConnectivityError(f"Unexpected Vimeo response for {operation}.")
            return payload
        raise VimeoConnectivityError(f"Vimeo {operation} exhausted retries.")

    def get_authenticated_account(self) -> tuple[VimeoAccountSummary, str]:
        payload = self._get_json(
            "/me",
            "authenticated-account lookup",
            params={"fields": "uri,name,metadata.connections.folders"},
        )
        uri = _safe_field(str(payload.get("uri") or ""))
        if not uri:
            raise VimeoConnectivityError("Vimeo did not return an account URI.")
        connections = ((payload.get("metadata") or {}).get("connections") or {})
        folder_uri = str((connections.get("folders") or {}).get("uri") or "").strip()
        return (
            VimeoAccountSummary(
                name=_safe_field(str(payload.get("name") or "")) or "[unnamed]",
                uri=uri,
            ),
            folder_uri or "/me/projects",
        )

    @staticmethod
    def _user_id_from_uri(value: Any) -> str:
        parts = [part for part in str(value or "").split("/") if part]
        return parts[-1] if len(parts) >= 2 and parts[-2] == "users" else ""

    def list_folders(self, folder_uri: str) -> list[VimeoFolderSummary]:
        results: dict[str, VimeoFolderSummary] = {}
        next_uri: str | None = folder_uri
        params: dict[str, Any] | None = {
            "page": 1,
            "per_page": 100,
            "fields": "name,uri",
        }
        seen_pages: set[str] = set()
        while next_uri:
            safe_url = self._safe_url(next_uri)
            if safe_url in seen_pages:
                raise VimeoConnectivityError("Vimeo repeated a folder page.")
            seen_pages.add(safe_url)
            payload = self._get_json(next_uri, "folder list", params=params)
            params = None
            items = payload.get("data")
            if not isinstance(items, list):
                raise VimeoConnectivityError("Unexpected Vimeo folder-list response.")
            for item in items:
                if not isinstance(item, dict):
                    continue
                uri = _safe_field(str(item.get("uri") or ""))
                folder_id = _resource_id(uri, "project") or _resource_id(uri, "folder")
                if uri and folder_id:
                    results[uri] = VimeoFolderSummary(
                        name=_safe_field(str(item.get("name") or "")) or "[unnamed]",
                        folder_id=folder_id,
                        uri=uri,
                    )
            paging = payload.get("paging") or {}
            candidate = paging.get("next") if isinstance(paging, dict) else None
            next_uri = str(candidate).strip() if candidate else None
        return sorted(
            results.values(), key=lambda folder: (folder.name.casefold(), folder.folder_id)
        )


@dataclass(frozen=True)
class VimeoVideoRecord:
    advisor_name: str
    top_folder_name: str
    top_folder_id: str
    folder_path: str
    folder_id: str
    video_name: str
    video_id: str
    vimeo_link: str
    player_embed_url: str
    autoplay_embed_url: str
    video_uri: str


def _normalized_name(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def _resource_id(uri: Any, resource: str) -> str:
    parts = [part for part in str(uri or "").split("/") if part]
    for index, part in enumerate(parts[:-1]):
        if part in {resource, f"{resource}s"} and parts[index + 1].isdigit():
            return parts[index + 1]
    return parts[-1] if parts and parts[-1].isdigit() else ""


def match_advisor_folders(
    folders: list[VimeoFolderSummary], advisor_names: list[str]
) -> tuple[list[tuple[str, VimeoFolderSummary]], list[str]]:
    by_name: dict[str, list[VimeoFolderSummary]] = {}
    for folder in folders:
        by_name.setdefault(_normalized_name(folder.name), []).append(folder)

    matches: list[tuple[str, VimeoFolderSummary]] = []
    unmatched: list[str] = []
    for advisor_name in advisor_names:
        candidates = by_name.get(_normalized_name(advisor_name), [])
        if not candidates:
            unmatched.append(advisor_name)
            continue
        for folder in candidates:
            matches.append((advisor_name, folder))
    return matches, unmatched


def list_folder_items(
    client: VimeoConnectivityClient,
    owner_user_id: str,
    folder_id: str,
    page_size: int = 100,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    next_uri: str | None = f"/users/{owner_user_id}/projects/{folder_id}/items"
    params: dict[str, Any] | None = {
        "page": 1,
        "per_page": page_size,
        "fields": (
            "type,video.uri,video.name,video.link,video.player_embed_url,"
            "folder.uri,folder.name"
        ),
    }
    seen_pages: set[str] = set()
    page_number = 0
    while next_uri:
        safe_url = client._safe_url(next_uri)
        if safe_url in seen_pages:
            raise VimeoConnectivityError(
                f"Vimeo repeated an item page for folder {folder_id}; stopped safely."
            )
        seen_pages.add(safe_url)
        page_number += 1
        payload = client._get_json(
            next_uri,
            f"folder {folder_id} item page {page_number}",
            params=params,
        )
        params = None
        items = payload.get("data")
        if not isinstance(items, list):
            raise VimeoConnectivityError(
                f"Vimeo returned an unexpected item list for folder {folder_id}."
            )
        results.extend(item for item in items if isinstance(item, dict))
        paging = payload.get("paging") or {}
        candidate = paging.get("next") if isinstance(paging, dict) else None
        next_uri = str(candidate).strip() if candidate else None
    return results


def collect_folder_videos(
    client: VimeoConnectivityClient,
    owner_user_id: str,
    advisor_name: str,
    top_folder: VimeoFolderSummary,
    audit: logging.Logger,
) -> tuple[list[VimeoVideoRecord], list[str]]:
    records: list[VimeoVideoRecord] = []
    errors: list[str] = []
    visited_folders: set[str] = set()
    stack: list[tuple[str, str, int]] = [(top_folder.folder_id, top_folder.name, 0)]

    while stack:
        folder_id, folder_path, depth = stack.pop()
        if folder_id in visited_folders:
            continue
        if depth > 10:
            errors.append(f"{folder_path}: nested-folder depth exceeded Vimeo's limit")
            continue
        visited_folders.add(folder_id)
        audit.info(
            "PROCESS_FOLDER advisor=%r top_folder_id=%r folder_id=%r path=%r",
            advisor_name,
            top_folder.folder_id,
            folder_id,
            folder_path,
        )
        try:
            items = list_folder_items(client, owner_user_id, folder_id)
        except VimeoConnectivityError as exc:
            errors.append(f"{folder_path}: {exc}")
            continue

        for item in items:
            item_type = str(item.get("type") or "")
            if item_type == "folder" and isinstance(item.get("folder"), dict):
                child = item["folder"]
                child_id = _resource_id(child.get("uri"), "project")
                child_name = _safe_field(str(child.get("name") or "")) or "[unnamed]"
                if child_id and child_id not in visited_folders:
                    stack.append((child_id, f"{folder_path} / {child_name}", depth + 1))
                continue
            if item_type != "video" or not isinstance(item.get("video"), dict):
                continue
            video = item["video"]
            video_uri = _safe_field(str(video.get("uri") or ""))
            video_id = _resource_id(video_uri, "video")
            if not video_id:
                continue
            vimeo_link = _safe_field(str(video.get("link") or ""))
            if not vimeo_link:
                vimeo_link = f"https://vimeo.com/{video_id}"
            player_embed_url = _safe_field(str(video.get("player_embed_url") or ""))
            if not player_embed_url:
                player_embed_url = f"https://player.vimeo.com/video/{video_id}"
            query_separator = "&" if "?" in player_embed_url else "?"
            autoplay_embed_url = (
                f"{player_embed_url}{query_separator}"
                "title=0&byline=0&portrait=0&autoplay=1"
            )
            records.append(
                VimeoVideoRecord(
                    advisor_name=advisor_name,
                    top_folder_name=top_folder.name,
                    top_folder_id=top_folder.folder_id,
                    folder_path=folder_path,
                    folder_id=folder_id,
                    video_name=_safe_field(str(video.get("name") or "")) or "[unnamed]",
                    video_id=video_id,
                    vimeo_link=vimeo_link,
                    player_embed_url=player_embed_url,
                    autoplay_embed_url=autoplay_embed_url,
                    video_uri=video_uri,
                )
            )
        audit.info(
            "PROCESSED_FOLDER advisor=%r folder_id=%r item_count=%d",
            advisor_name,
            folder_id,
            len(items),
        )

    records.sort(
        key=lambda row: (
            row.advisor_name.casefold(),
            row.top_folder_id,
            row.folder_path.casefold(),
            row.video_name.casefold(),
            row.video_id,
        )
    )
    return records, errors


def _create_logger(log_dir: Path, stamp: str) -> tuple[logging.Logger, Path]:
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"vimeo-advisor-videos-{stamp}.log"
    path.touch(mode=0o600, exist_ok=False)
    os.chmod(path, 0o600)
    logger = logging.getLogger(f"vimeo_advisor_videos.{stamp}.{id(path)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(path, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger, path


def _write_csv(path: Path, records: list[VimeoVideoRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(VimeoVideoRecord.__annotations__))
        writer.writeheader()
        for record in records:
            writer.writerow(record.__dict__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only extraction of Vimeo video links from advisor folders."
    )
    parser.add_argument(
        "--advisor",
        action="append",
        required=True,
        help="Exact advisor folder name; repeat for multiple advisors.",
    )
    parser.add_argument(
        "--output-dir", default=str(PROJECT_ROOT / "output"), help="Local CSV directory."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load_dotenv(PROJECT_ROOT / ".env")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    logger, log_path = _create_logger(PROJECT_ROOT / "logs", stamp)
    client = VimeoConnectivityClient(
        os.getenv("VIMEO_ACCESS_TOKEN", ""), audit=logger.info
    )
    try:
        account, folder_uri = client.get_authenticated_account()
        owner_user_id = client._user_id_from_uri(account.uri)
        if not owner_user_id:
            raise VimeoConnectivityError("Authenticated Vimeo owner ID is unavailable.")
        folders = client.list_folders(folder_uri)
        matches, unmatched = match_advisor_folders(folders, args.advisor)
        all_records: list[VimeoVideoRecord] = []
        all_errors: list[str] = []
        for advisor_name, folder in matches:
            records, errors = collect_folder_videos(
                client, owner_user_id, advisor_name, folder, logger
            )
            all_records.extend(records)
            all_errors.extend(errors)
            print(
                f"Processed: {advisor_name}\tfolder_id={folder.folder_id}"
                f"\tvideos={len(records)}"
            )
        all_records.sort(
            key=lambda row: (
                row.advisor_name.casefold(),
                row.top_folder_id,
                row.folder_path.casefold(),
                row.video_name.casefold(),
                row.video_id,
            )
        )
        output_path = Path(args.output_dir) / f"vimeo-advisor-videos-{stamp}.csv"
        _write_csv(output_path, all_records)
        for name in unmatched:
            print(f"UNMATCHED ADVISOR: {name}", file=sys.stderr)
        for error in all_errors:
            print(f"API/PERMISSION ERROR: {error}", file=sys.stderr)
        print(f"Matched folders: {len(matches)}")
        print(f"Unmatched advisors: {len(unmatched)}")
        print(f"Video links: {len(all_records)}")
        print(f"CSV: {output_path}")
        print(f"Processing log: {log_path}", file=sys.stderr)
        return 0 if not unmatched and not all_errors else 1
    except VimeoConnectivityError as exc:
        logger.error("FAILED %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Processing log: {log_path}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
