from __future__ import annotations

import os
import logging
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import requests
from dotenv import load_dotenv


BASE_URL = "https://services.leadconnectorhq.com"
SEARCH_LOCATIONS_PATH = "/locations/search"
API_VERSION = "v3"
REQUIRED_SCOPE = "locations.readonly"
PROJECT_ROOT = Path(__file__).resolve().parents[1]
AuditCallback = Callable[[str], None]


class ConnectivityError(RuntimeError):
    """A safe, non-secret-bearing connectivity failure."""


@dataclass(frozen=True)
class LocationSummary:
    name: str
    location_id: str


@dataclass(frozen=True)
class LocationProfile:
    location_id: str
    friendly_business_name: str
    legal_business_name: str
    business_email: str
    business_phone: str
    branded_domain: str
    business_website: str
    business_niche: str
    business_currency: str
    business_logo_url: str


class AgencyConnectivityClient:
    """Read-only client for an agency Private Integration Token."""

    def __init__(
        self,
        agency_token: str,
        company_id: str | None = None,
        session: requests.Session | None = None,
        max_retries: int = 2,
        sleep=time.sleep,
        audit: AuditCallback | None = None,
    ) -> None:
        if not agency_token.strip():
            raise ConnectivityError(
                "GHL_AGENCY_TOKEN is missing. Add the Agency Private Integration Token to .env."
            )
        self._agency_token = agency_token.strip()
        self.company_id = company_id.strip() if company_id and company_id.strip() else None
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.sleep = sleep
        self.audit = audit or (lambda _message: None)

    def _headers(self) -> dict[str, str]:
        return {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._agency_token}",
            "Version": API_VERSION,
        }

    @staticmethod
    def _error_for_status(status_code: int) -> ConnectivityError:
        if status_code == 401:
            return ConnectivityError(
                "Authentication failed (HTTP 401). Verify that GHL_AGENCY_TOKEN is current "
                "and is an Agency-level Private Integration Token."
            )
        if status_code == 403:
            return ConnectivityError(
                f"Permission denied (HTTP 403). Enable the {REQUIRED_SCOPE} scope on the "
                "Agency Private Integration and rotate/copy the token again if HighLevel requires it."
            )
        if status_code == 422:
            return ConnectivityError(
                "HighLevel rejected the location-search request (HTTP 422). Verify that the token "
                "was created at Agency level and that GHL_COMPANY_ID, if set, is the correct agency ID."
            )
        return ConnectivityError(
            f"HighLevel location search failed with HTTP {status_code}. No changes were made."
        )

    def _get_page(self, skip: int, limit: int) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"skip": skip, "limit": limit, "order": "asc"}
        if self.company_id:
            params["companyId"] = self.company_id

        self.audit(f"FETCH_PAGE skip={skip} limit={limit}")
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    f"{BASE_URL}{SEARCH_LOCATIONS_PATH}",
                    headers=self._headers(),
                    params=params,
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self.audit(f"RETRY network_error attempt={attempt + 1}")
                    self.sleep(0.5 * (2**attempt))
                    continue
                raise ConnectivityError(
                    "Unable to reach HighLevel after retrying. Check the network connection."
                ) from exc

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.max_retries:
                    self.audit(
                        f"RETRY http_status={response.status_code} attempt={attempt + 1}"
                    )
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
                    except ValueError:
                        delay = 0.5 * (2**attempt)
                    self.sleep(max(0.0, delay))
                    continue
            if response.status_code >= 400:
                raise self._error_for_status(response.status_code)
            try:
                payload = response.json()
            except ValueError as exc:
                raise ConnectivityError(
                    "HighLevel returned an invalid JSON response. No changes were made."
                ) from exc
            locations = payload.get("locations")
            if not isinstance(locations, list):
                raise ConnectivityError(
                    "HighLevel returned an unexpected location-search response. No changes were made."
                )
            self.audit(f"FETCHED_PAGE skip={skip} count={len(locations)}")
            return locations
        raise ConnectivityError("HighLevel location search exhausted retries. No changes were made.")

    def _get_location(self, location_id: str) -> dict[str, Any]:
        safe_location_id = _safe_field(location_id)
        if not safe_location_id:
            raise ConnectivityError("Cannot fetch a HighLevel location without a Location ID.")

        self.audit(f"FETCH_LOCATION location_id={safe_location_id!r}")
        for attempt in range(self.max_retries + 1):
            try:
                response = self.session.get(
                    f"{BASE_URL}/locations/{safe_location_id}",
                    headers=self._headers(),
                    timeout=30,
                )
            except requests.RequestException as exc:
                if attempt < self.max_retries:
                    self.audit(
                        f"RETRY location_id={safe_location_id!r} network_error "
                        f"attempt={attempt + 1}"
                    )
                    self.sleep(0.5 * (2**attempt))
                    continue
                raise ConnectivityError(
                    f"Unable to read HighLevel location {safe_location_id} after retrying."
                ) from exc

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.max_retries:
                    self.audit(
                        f"RETRY location_id={safe_location_id!r} "
                        f"http_status={response.status_code} attempt={attempt + 1}"
                    )
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 0.5 * (2**attempt)
                    except ValueError:
                        delay = 0.5 * (2**attempt)
                    self.sleep(max(0.0, delay))
                    continue
            if response.status_code >= 400:
                raise self._error_for_status(response.status_code)
            try:
                payload = response.json()
            except ValueError as exc:
                raise ConnectivityError(
                    f"HighLevel returned invalid JSON for location {safe_location_id}."
                ) from exc
            location = payload.get("location")
            if not isinstance(location, dict):
                raise ConnectivityError(
                    f"HighLevel returned an unexpected response for location {safe_location_id}."
                )
            self.audit(f"FETCHED_LOCATION location_id={safe_location_id!r}")
            return location
        raise ConnectivityError(
            f"HighLevel location {safe_location_id} exhausted retries. No changes were made."
        )

    def list_locations(self, page_size: int = 100) -> list[LocationSummary]:
        results: list[LocationSummary] = []
        seen: set[str] = set()
        skip = 0
        while True:
            page = self._get_page(skip=skip, limit=page_size)
            new_count = 0
            for raw in page:
                location_id = str(raw.get("id", "")).strip()
                name = str(raw.get("name", "")).strip()
                if not location_id:
                    continue
                if location_id not in seen:
                    seen.add(location_id)
                    safe_name = _safe_field(name or "[unnamed]")
                    safe_location_id = _safe_field(location_id)
                    results.append(
                        LocationSummary(name=safe_name, location_id=safe_location_id)
                    )
                    self.audit(
                        f"PROCESSED name={safe_name!r} location_id={safe_location_id!r}"
                    )
                    new_count += 1
            if len(page) < page_size:
                break
            if new_count == 0:
                raise ConnectivityError(
                    "HighLevel pagination repeated a page; stopped safely without making changes."
                )
            skip += len(page)
        sorted_results = sorted(
            results, key=lambda item: (item.name.casefold(), item.location_id)
        )
        self.audit(f"COMPLETED total_locations={len(sorted_results)}")
        return sorted_results

    def get_location_profile(self, location: LocationSummary) -> LocationProfile:
        raw = self._get_location(location.location_id)
        business = raw.get("business")
        if not isinstance(business, dict):
            business = {}

        return LocationProfile(
            location_id=_first_text(raw.get("id"), location.location_id),
            friendly_business_name=_first_text(raw.get("name"), location.name),
            legal_business_name=_first_text(
                business.get("legalName"),
                raw.get("legalBusinessName"),
                business.get("name"),
            ),
            business_email=_first_text(business.get("email"), raw.get("email")),
            business_phone=_first_text(business.get("phone"), raw.get("phone")),
            branded_domain=_first_text(
                raw.get("domain"), raw.get("brandedDomain"), business.get("domain")
            ),
            business_website=_first_text(business.get("website"), raw.get("website")),
            business_niche=_first_text(
                business.get("niche"),
                business.get("businessNiche"),
                raw.get("businessNiche"),
                raw.get("niche"),
                business.get("industry"),
                business.get("category"),
            ),
            business_currency=_first_text(
                business.get("currency"),
                business.get("businessCurrency"),
                raw.get("businessCurrency"),
                raw.get("currency"),
            ),
            business_logo_url=_first_text(business.get("logoUrl"), raw.get("logoUrl")),
        )

    def list_location_profiles(self, page_size: int = 100) -> list[LocationProfile]:
        locations = self.list_locations(page_size=page_size)
        profiles = [self.get_location_profile(location) for location in locations]
        self.audit(f"COMPLETED_PROFILE_EXPORT total_locations={len(profiles)}")
        return profiles


def _safe_field(value: str) -> str:
    return " ".join(value.replace("\x00", "").splitlines()).replace("\t", " ").strip()


def _first_text(*values: Any) -> str:
    for value in values:
        if value is None or isinstance(value, (dict, list)):
            continue
        cleaned = _safe_field(str(value))
        if cleaned:
            return cleaned
    return ""


def create_audit_logger(log_dir: str | Path) -> tuple[logging.Logger, Path]:
    root = Path(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = root / f"connectivity-{stamp}.log"
    logger = logging.getLogger(f"ghl_connectivity.{stamp}.{id(log_path)}")
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
    logger, log_path = create_audit_logger(
        os.getenv("GHL_CONNECTIVITY_LOG_DIR", str(PROJECT_ROOT / "logs"))
    )
    logger.info("START endpoint=GET_/locations/search api_version=v3")
    try:
        client = AgencyConnectivityClient(
            agency_token=os.getenv("GHL_AGENCY_TOKEN", ""),
            company_id=os.getenv("GHL_COMPANY_ID"),
            audit=logger.info,
        )
        for location in client.list_locations():
            # Deliberately print only the requested non-secret fields.
            print(f"{location.name}\t{location.location_id}")
        print(f"Processing log: {log_path}", file=sys.stderr)
        return 0
    except ConnectivityError as exc:
        logger.error("FAILED %s", exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        print(f"Processing log: {log_path}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
