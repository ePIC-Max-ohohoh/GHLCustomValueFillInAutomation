from __future__ import annotations

import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests
from dotenv import load_dotenv


BASE_URL = "https://api.vimeo.com"
ACCEPT_HEADER = "application/vnd.vimeo.*+json;version=3.4"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
AuditCallback = Callable[[str], None]


class VimeoConnectivityError(RuntimeError):
    """A safe Vimeo connectivity failure that never contains credentials."""


@dataclass(frozen=True)
class VimeoAccountSummary:
    name: str
    uri: str
    account_type: str
    teams_uri: str = ""
    advertised_team_count: int | None = None


@dataclass(frozen=True)
class VimeoFolderSummary:
    name: str
    folder_id: str
    uri: str


@dataclass(frozen=True)
class VimeoTeamSummary:
    owner_user_id: str
    team_id: str
    name: str
    role: str = "[unknown]"
    source: str = "teams connection"


class VimeoConnectivityClient:
    """GET-only Vimeo client for authentication and accessible folders/projects."""

    def __init__(
        self,
        access_token: str,
        session: requests.Session | None = None,
        max_retries: int = 2,
        sleep=time.sleep,
        audit: AuditCallback | None = None,
    ) -> None:
        if not access_token.strip():
            raise VimeoConnectivityError(
                "VIMEO_ACCESS_TOKEN is missing. Add the Vimeo Personal Access Token to .env."
            )
        self._access_token = access_token.strip()
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.sleep = sleep
        self.audit = audit or (lambda _message: None)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": ACCEPT_HEADER,
            "Authorization": f"Bearer {self._access_token}",
        }

    @staticmethod
    def _safe_url(path_or_url: str) -> str:
        url = urljoin(f"{BASE_URL}/", path_or_url)
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.netloc != "api.vimeo.com":
            raise VimeoConnectivityError(
                "Vimeo pagination returned an unexpected external URL; stopped safely."
            )
        return url

    @staticmethod
    def _error_for_status(status_code: int, operation: str) -> VimeoConnectivityError:
        if status_code == 401:
            return VimeoConnectivityError(
                "Vimeo authentication failed (HTTP 401). Verify that VIMEO_ACCESS_TOKEN is "
                "current and belongs to the intended Vimeo account."
            )
        if status_code == 403:
            return VimeoConnectivityError(
                f"Vimeo denied {operation} (HTTP 403). Verify the token has public and private "
                "scopes and that the account has access to the requested library."
            )
        if status_code == 404:
            return VimeoConnectivityError(
                f"Vimeo could not find the resource for {operation} (HTTP 404). The account may "
                "not have access to that personal or team library."
            )
        return VimeoConnectivityError(
            f"Vimeo {operation} failed with HTTP {status_code}. No Vimeo data was modified."
        )

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
                    headers=self._headers(),
                    params=params,
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self.audit(f"RETRY operation={operation} network_error attempt={attempt + 1}")
                    self.sleep(0.5 * (2**attempt))
                    continue
                raise VimeoConnectivityError(
                    "Unable to reach Vimeo after retrying. Check the network connection."
                ) from exc

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.max_retries:
                    self.audit(
                        f"RETRY operation={operation} http_status={response.status_code} "
                        f"attempt={attempt + 1}"
                    )
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
                    except ValueError:
                        delay = 0.5 * (2**attempt)
                    self.sleep(max(0.0, delay))
                    continue
            if response.status_code >= 400:
                raise self._error_for_status(response.status_code, operation)
            try:
                payload = response.json()
            except ValueError as exc:
                raise VimeoConnectivityError(
                    f"Vimeo returned invalid JSON while attempting {operation}."
                ) from exc
            if not isinstance(payload, dict):
                raise VimeoConnectivityError(
                    f"Vimeo returned an unexpected response while attempting {operation}."
                )
            return payload
        raise VimeoConnectivityError(f"Vimeo {operation} exhausted retries.")

    def get_authenticated_account(self) -> tuple[VimeoAccountSummary, str]:
        payload = self._get_json(
            "/me",
            "authenticated-account lookup",
            params={
                "fields": "uri,name,account,metadata.connections.folders,"
                "metadata.connections.folders_root,metadata.connections.teams",
            },
        )
        uri = _safe_field(str(payload.get("uri") or ""))
        name = _safe_field(str(payload.get("name") or ""))
        account_type = _safe_field(str(payload.get("account") or ""))
        if not uri:
            raise VimeoConnectivityError(
                "Vimeo authenticated the request but did not return an account URI."
            )

        connections = ((payload.get("metadata") or {}).get("connections") or {})
        folder_connection = connections.get("folders") or {}
        folder_uri = str(folder_connection.get("uri") or "").strip()
        if not folder_uri:
            # Vimeo's folder API historically uses the original "project" nomenclature.
            folder_uri = "/me/projects"

        team_connection = connections.get("teams") or {}
        teams_uri = str(team_connection.get("uri") or "").strip()
        advertised_team_count = team_connection.get("total")
        if not isinstance(advertised_team_count, int):
            advertised_team_count = None

        summary = VimeoAccountSummary(
            name=name or "[unnamed]",
            uri=uri,
            account_type=account_type or "[unknown]",
            teams_uri=teams_uri,
            advertised_team_count=advertised_team_count,
        )
        self.audit(
            f"AUTHENTICATED name={summary.name!r} uri={summary.uri!r} "
            f"account_type={summary.account_type!r}"
        )
        self.audit(f"FOLDER_CONNECTION uri={_safe_field(folder_uri)!r}")
        self.audit(
            f"TEAM_CONNECTION uri={_safe_field(teams_uri)!r} "
            f"advertised_total={advertised_team_count!r}"
        )
        return summary, folder_uri

    @staticmethod
    def _user_id_from_uri(value: Any) -> str:
        uri = str(value or "").strip()
        parts = [part for part in uri.split("/") if part]
        if len(parts) >= 2 and parts[-2] == "users" and parts[-1].isdigit():
            return parts[-1]
        return ""

    @classmethod
    def _team_from_payload(
        cls, raw: dict[str, Any], source: str = "teams connection"
    ) -> VimeoTeamSummary | None:
        owner = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        metadata = raw.get("metadata") if isinstance(raw.get("metadata"), dict) else {}
        connections = (
            metadata.get("connections")
            if isinstance(metadata.get("connections"), dict)
            else {}
        )
        owner_connection = (
            connections.get("owner")
            if isinstance(connections.get("owner"), dict)
            else {}
        )
        owner_id = str(raw.get("owner_user_id") or raw.get("owner_id") or "").strip()
        if not owner_id:
            owner_id = cls._user_id_from_uri(owner.get("uri"))
        if not owner_id:
            owner_id = cls._user_id_from_uri(owner_connection.get("uri"))
        if not owner_id.isdigit():
            return None

        team_id = _safe_field(str(raw.get("id") or "")) or "[unknown]"
        name = _safe_field(
            str(
                raw.get("team_name")
                or raw.get("name")
                or owner.get("name")
                or owner_connection.get("display_name")
                or ""
            )
        )
        return VimeoTeamSummary(
            owner_user_id=owner_id,
            team_id=team_id,
            name=name or "[unnamed]",
            source=source,
        )

    def list_team_memberships(
        self, teams_uri: str, page_size: int = 100
    ) -> list[VimeoTeamSummary]:
        """Follow Vimeo's advertised team connection and return team owners."""
        if not teams_uri:
            return []
        results: dict[str, VimeoTeamSummary] = {}
        seen_pages: set[str] = set()
        next_uri: str | None = teams_uri
        params: dict[str, Any] | None = {"page": 1, "per_page": page_size}
        page_number = 0

        while next_uri:
            safe_page_url = self._safe_url(next_uri)
            if safe_page_url in seen_pages:
                raise VimeoConnectivityError(
                    "Vimeo team pagination repeated a page; stopped safely."
                )
            seen_pages.add(safe_page_url)
            page_number += 1
            payload = self._get_json(
                next_uri, f"team membership page {page_number}", params=params
            )
            params = None
            items = payload.get("data")
            if not isinstance(items, list):
                items = payload.get("teams")
            if not isinstance(items, list):
                raise VimeoConnectivityError(
                    "Vimeo returned an unexpected team-membership response."
                )
            self.audit(f"FETCHED_TEAM_PAGE page={page_number} count={len(items)}")
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                team = self._team_from_payload(raw)
                if team is None:
                    self.audit("SKIPPED_TEAM reason=missing_owner_user_id")
                    continue
                results[team.owner_user_id] = team
                self.audit(
                    f"PROCESSED_TEAM name={team.name!r} team_id={team.team_id!r} "
                    f"owner_user_id={team.owner_user_id!r}"
                )
            paging = payload.get("paging") or {}
            candidate = paging.get("next") if isinstance(paging, dict) else None
            next_uri = str(candidate).strip() if candidate else None

        return sorted(
            results.values(), key=lambda team: (team.name.casefold(), team.owner_user_id)
        )

    def get_current_team(self) -> VimeoTeamSummary:
        """Return Vimeo's current-team descriptor when team enumeration is gated."""
        payload = self._get_json("/me/team", "current-team lookup")
        team = self._team_from_payload(payload, source="current-team fallback")
        if team is None:
            raise VimeoConnectivityError(
                "Vimeo returned a current-team response without an owner user ID."
            )
        self.audit(
            f"CURRENT_TEAM owner_user_id={team.owner_user_id!r} name={team.name!r}"
        )
        return team

    def get_team_role(self, owner_user_id: str) -> str:
        if not owner_user_id.isdigit():
            raise VimeoConnectivityError("The Vimeo team owner user ID must be numeric.")
        payload = self._get_json(
            f"/users/{owner_user_id}/team/role",
            f"team role lookup for owner {owner_user_id}",
            params={"fields": "role,permission_level,status,active"},
        )
        role = _safe_field(
            str(payload.get("role") or payload.get("permission_level") or "")
        )
        self.audit(f"TEAM_ROLE owner_user_id={owner_user_id!r} role={role!r}")
        return role or "[unknown]"

    def list_folders(
        self, folder_uri: str, page_size: int = 100
    ) -> list[VimeoFolderSummary]:
        results: list[VimeoFolderSummary] = []
        seen_folders: set[str] = set()
        seen_pages: set[str] = set()
        next_uri: str | None = folder_uri
        params: dict[str, Any] | None = {
            "page": 1,
            "per_page": page_size,
            "fields": "name,uri",
        }
        page_number = 0

        while next_uri:
            safe_page_url = self._safe_url(next_uri)
            if safe_page_url in seen_pages:
                raise VimeoConnectivityError(
                    "Vimeo pagination repeated a page; stopped safely without making changes."
                )
            seen_pages.add(safe_page_url)
            page_number += 1
            payload = self._get_json(
                next_uri,
                f"folder page {page_number}",
                params=params,
            )
            params = None
            items = payload.get("data")
            if not isinstance(items, list):
                raise VimeoConnectivityError(
                    "Vimeo returned an unexpected folder-list response."
                )
            self.audit(f"FETCHED_FOLDER_PAGE page={page_number} count={len(items)}")

            for raw in items:
                if not isinstance(raw, dict):
                    continue
                uri = _safe_field(str(raw.get("uri") or ""))
                name = _safe_field(str(raw.get("name") or ""))
                if not uri or uri in seen_folders:
                    continue
                folder_id = _safe_field(uri.rstrip("/").split("/")[-1])
                seen_folders.add(uri)
                folder = VimeoFolderSummary(
                    name=name or "[unnamed]",
                    folder_id=folder_id,
                    uri=uri,
                )
                results.append(folder)
                self.audit(
                    f"PROCESSED_FOLDER name={folder.name!r} id={folder.folder_id!r} "
                    f"uri={folder.uri!r}"
                )

            paging = payload.get("paging") or {}
            candidate = paging.get("next") if isinstance(paging, dict) else None
            next_uri = str(candidate).strip() if candidate else None

        sorted_results = sorted(
            results, key=lambda item: (item.name.casefold(), item.folder_id, item.uri)
        )
        self.audit(f"COMPLETED total_folders={len(sorted_results)}")
        return sorted_results


def _safe_field(value: str) -> str:
    return " ".join(value.replace("\x00", "").splitlines()).replace("\t", " ").strip()


def create_vimeo_audit_logger(log_dir: str | Path) -> tuple[logging.Logger, Path]:
    root = Path(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = root / f"vimeo-connectivity-{stamp}.log"
    log_path.touch(mode=0o600, exist_ok=False)
    os.chmod(log_path, 0o600)
    logger = logging.getLogger(f"vimeo_connectivity.{stamp}.{id(log_path)}")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = logging.FileHandler(log_path, encoding="utf-8")
    formatter = logging.Formatter("%(asctime)sZ %(message)s", "%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger, log_path


def main() -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    logger, log_path = create_vimeo_audit_logger(
        os.getenv("VIMEO_CONNECTIVITY_LOG_DIR", str(PROJECT_ROOT / "logs"))
    )
    logger.info(
        "START methods=GET endpoints=/me,advertised_folders,advertised_teams,"
        "/me/team,/users/{owner_id}/team/role,/users/{owner_id}/folders"
    )
    try:
        client = VimeoConnectivityClient(
            access_token=os.getenv("VIMEO_ACCESS_TOKEN", ""),
            audit=logger.info,
        )
        account, folder_uri = client.get_authenticated_account()
        folders = client.list_folders(folder_uri)

        print(f"Authenticated Vimeo account: {account.name}\t{account.uri}")
        print(f"Account type: {account.account_type}")
        print(
            "Advertised team memberships: "
            f"{account.advertised_team_count if account.advertised_team_count is not None else '[unknown]'}"
        )
        for folder in folders:
            print(f"Personal folder: {folder.name}\t{folder.folder_id}\t{folder.uri}")
        print(f"Accessible personal folders/projects: {len(folders)}")

        errors: list[str] = []
        teams_by_owner: dict[str, VimeoTeamSummary] = {}
        if account.teams_uri:
            try:
                for team in client.list_team_memberships(account.teams_uri):
                    teams_by_owner[team.owner_user_id] = team
            except VimeoConnectivityError as exc:
                errors.append(f"Team membership enumeration: {exc}")

        # Vimeo can advertise /users/{id}/teams while returning 404 when that
        # same connection is followed. This fallback identifies only the current
        # owner context; for a free member it can be the member's personal team.
        try:
            current_team = client.get_current_team()
            teams_by_owner.setdefault(current_team.owner_user_id, current_team)
        except VimeoConnectivityError as exc:
            errors.append(f"Current-team fallback: {exc}")

        explicit_owner_ids = {
            value.strip()
            for value in os.getenv("VIMEO_TEAM_OWNER_USER_IDS", "").split(",")
            if value.strip()
        }
        invalid_owner_ids = sorted(
            value for value in explicit_owner_ids if not value.isdigit()
        )
        if invalid_owner_ids:
            errors.append(
                "Configured team owner IDs must be numeric; ignored: "
                + ", ".join(invalid_owner_ids)
            )
        for owner_id in sorted(value for value in explicit_owner_ids if value.isdigit()):
            teams_by_owner.setdefault(
                owner_id,
                VimeoTeamSummary(
                    owner_user_id=owner_id,
                    team_id="[unknown]",
                    name="[configured team owner]",
                    source="VIMEO_TEAM_OWNER_USER_IDS",
                ),
            )

        authenticated_user_id = client._user_id_from_uri(account.uri)
        external_team_count = 0
        team_folder_count = 0
        for owner_id, team in sorted(teams_by_owner.items()):
            role = team.role
            try:
                role = client.get_team_role(owner_id)
            except VimeoConnectivityError as exc:
                errors.append(f"Team role for owner {owner_id}: {exc}")

            context = (
                "personal/current"
                if owner_id == authenticated_user_id
                else "Team Library"
            )
            print(
                f"Team: {team.name}\towner_user_id={owner_id}\tteam_id={team.team_id}"
                f"\trole={role}\tcontext={context}\tsource={team.source}"
            )
            if owner_id == authenticated_user_id:
                continue
            external_team_count += 1
            try:
                team_folders = client.list_folders(f"/users/{owner_id}/folders")
            except VimeoConnectivityError as exc:
                errors.append(f"Team Library folders for owner {owner_id}: {exc}")
                continue
            for folder in team_folders:
                print(
                    f"Team Library folder: {folder.name}\t{folder.folder_id}\t{folder.uri}"
                )
            team_folder_count += len(team_folders)

        if account.advertised_team_count and external_team_count == 0:
            errors.append(
                "Vimeo advertises team memberships but did not expose an external owner user ID. "
                "Set VIMEO_TEAM_OWNER_USER_IDS to a known team owner's numeric Vimeo user ID "
                "to perform the documented owner-scoped folder lookup with this same token."
            )
        print(f"External team owners discovered/queried: {external_team_count}")
        print(f"Accessible Team Library folders: {team_folder_count}")
        for error in errors:
            logger.warning("NONFATAL %s", error)
            print(f"API/PERMISSION ERROR: {error}", file=sys.stderr)
        print(f"Processing log: {log_path}", file=sys.stderr)
        return 0
    except VimeoConnectivityError as exc:
        logger.error("FAILED %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Processing log: {log_path}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
