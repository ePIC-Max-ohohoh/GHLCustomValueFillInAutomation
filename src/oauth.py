from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests


TOKEN_URL = "https://services.leadconnectorhq.com/oauth/token"
LOCATION_TOKEN_URL = "https://services.leadconnectorhq.com/oauth/location-token"
INSTALLED_LOCATIONS_URL = "https://services.leadconnectorhq.com/oauth/installed-locations"
API_VERSION = "v3"


class OAuthError(RuntimeError):
    pass


class SecretRedactor:
    """Redacts known secrets and common token-like JSON/form fields."""

    FIELD_PATTERN = re.compile(
        r"(?i)(client_secret|access_token|refresh_token|authorization|code)"
        r"(\s*[=:]\s*|\"\s*:\s*\")([^\s,}\"]+)"
    )

    def __init__(self, secrets: list[str] | None = None) -> None:
        self.secrets = sorted({item for item in (secrets or []) if item}, key=len, reverse=True)

    def redact(self, value: object) -> str:
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        return self.FIELD_PATTERN.sub(lambda m: f"{m.group(1)}{m.group(2)}[REDACTED]", text)


@dataclass
class AccessToken:
    value: str
    expires_at: float
    refresh_token: str | None = None
    company_id: str | None = None
    user_type: str | None = None

    def usable(self, now: float | None = None) -> bool:
        return bool(self.value) and self.expires_at > (now or time.time()) + 60


class TokenStore:
    """Small local store for rotated agency refresh tokens; file mode is 0600."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OAuthError(f"Unable to read OAuth token store: {self.path}") from exc

    def save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".ghl-token-", dir=self.path.parent)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(data, handle)
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise


class OAuthManager:
    """HighLevel v3 agency OAuth and per-location token exchange."""

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        refresh_token: str = "",
        company_id: str | None = None,
        redirect_uri: str | None = None,
        app_id: str | None = None,
        token_store: TokenStore | None = None,
        session: requests.Session | None = None,
        now=time.time,
    ) -> None:
        if not client_id or not client_secret:
            raise OAuthError("GHL_CLIENT_ID and GHL_CLIENT_SECRET are required")
        self.client_id = client_id
        self.client_secret = client_secret
        self.redirect_uri = redirect_uri
        self.app_id = app_id
        self.company_id = company_id
        self.session = session or requests.Session()
        self.token_store = token_store
        self.now = now
        stored = token_store.load() if token_store else {}
        self.refresh_token = str(stored.get("refresh_token") or refresh_token or "")
        self.company_id = str(stored.get("company_id") or company_id or "") or None
        self.agency_token: AccessToken | None = None
        self.location_tokens: dict[str, AccessToken] = {}
        self.redactor = SecretRedactor([client_secret, refresh_token, self.refresh_token])

    @classmethod
    def from_env(cls, session: requests.Session | None = None) -> "OAuthManager":
        store_path = os.getenv("GHL_TOKEN_STORE", ".ghl_tokens.json")
        return cls(
            client_id=os.getenv("GHL_CLIENT_ID", ""),
            client_secret=os.getenv("GHL_CLIENT_SECRET", ""),
            refresh_token=os.getenv("GHL_REFRESH_TOKEN", ""),
            company_id=os.getenv("GHL_COMPANY_ID"),
            redirect_uri=os.getenv("GHL_REDIRECT_URI"),
            app_id=os.getenv("GHL_APP_ID"),
            token_store=TokenStore(store_path),
            session=session,
        )

    def _error(self, prefix: str, response: requests.Response) -> OAuthError:
        body = self.redactor.redact(getattr(response, "text", ""))[:500]
        return OAuthError(f"{prefix} (HTTP {response.status_code}): {body}")

    def _token_request(self, data: dict[str, str]) -> dict[str, Any]:
        response = self.session.post(
            TOKEN_URL,
            data=data,
            headers={"Accept": "application/json", "Version": API_VERSION},
            timeout=30,
        )
        if response.status_code >= 400:
            raise self._error("OAuth token request failed", response)
        return response.json()

    def exchange_authorization_code(self, code: str, user_type: str = "Company") -> AccessToken:
        data = {
            "client_id": self.client_id,
            "client_secret": self.client_secret,
            "grant_type": "authorization_code",
            "code": code,
            "user_type": user_type,
        }
        if self.redirect_uri:
            data["redirect_uri"] = self.redirect_uri
        payload = self._token_request(data)
        return self._accept_agency_payload(payload)

    def _accept_agency_payload(self, payload: dict[str, Any]) -> AccessToken:
        access_value = str(payload.get("access_token", ""))
        new_refresh = str(payload.get("refresh_token", ""))
        if not access_value or not new_refresh:
            raise OAuthError("OAuth response did not include access_token and refresh_token")
        self.refresh_token = new_refresh
        self.company_id = str(payload.get("companyId") or self.company_id or "") or None
        token = AccessToken(
            value=access_value,
            expires_at=self.now() + int(payload.get("expires_in", 0)),
            refresh_token=new_refresh,
            company_id=self.company_id,
            user_type=str(payload.get("userType", "Company")),
        )
        self.agency_token = token
        self.redactor = SecretRedactor([self.client_secret, self.refresh_token, access_value])
        if self.token_store:
            self.token_store.save(
                {"refresh_token": new_refresh, "company_id": self.company_id or ""}
            )
        return token

    def get_agency_token(self, force_refresh: bool = False) -> str:
        if not force_refresh and self.agency_token and self.agency_token.usable(self.now()):
            return self.agency_token.value
        if not self.refresh_token:
            raise OAuthError(
                "No agency refresh token is available; run with --initialize-oauth first"
            )
        payload = self._token_request(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "user_type": "Company",
            }
        )
        token = self._accept_agency_payload(payload)
        if token.user_type and token.user_type.lower() != "company":
            raise OAuthError("Configured refresh token is not an agency/company token")
        return token.value

    def invalidate_location(self, location_id: str) -> None:
        self.location_tokens.pop(location_id, None)

    def get_location_token(self, location_id: str, force_refresh: bool = False) -> str:
        cached = self.location_tokens.get(location_id)
        if not force_refresh and cached and cached.usable(self.now()):
            return cached.value
        agency_token = self.get_agency_token()
        if not self.company_id:
            raise OAuthError("GHL_COMPANY_ID is required and was not returned by OAuth")
        response = self.session.post(
            LOCATION_TOKEN_URL,
            data={"companyId": self.company_id, "locationId": location_id},
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {agency_token}",
                "Version": API_VERSION,
            },
            timeout=30,
        )
        if response.status_code == 401 and not force_refresh:
            self.get_agency_token(force_refresh=True)
            return self.get_location_token(location_id, force_refresh=True)
        if response.status_code >= 400:
            raise self._error(f"Unable to obtain token for Location {location_id}", response)
        payload = response.json()
        value = str(payload.get("access_token", ""))
        if not value:
            raise OAuthError("Location token response did not include access_token")
        self.location_tokens[location_id] = AccessToken(
            value=value,
            expires_at=self.now() + int(payload.get("expires_in", 0)),
            refresh_token=payload.get("refresh_token"),
            company_id=self.company_id,
            user_type="Location",
        )
        return value

    def get_installed_locations(self) -> list[dict[str, Any]]:
        if not self.app_id or not self.company_id:
            raise OAuthError("GHL_APP_ID and GHL_COMPANY_ID are required for location discovery")
        token = self.get_agency_token()
        items: list[dict[str, Any]] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {
                "companyId": self.company_id,
                "appId": self.app_id,
                "isInstalled": "true",
                "pageSize": 100,
            }
            if page_token:
                params["pageToken"] = page_token
            response = self.session.get(
                INSTALLED_LOCATIONS_URL,
                params=params,
                headers={
                    "Accept": "application/json",
                    "Authorization": f"Bearer {token}",
                    "Version": API_VERSION,
                },
                timeout=30,
            )
            if response.status_code >= 400:
                raise self._error("Unable to discover installed locations", response)
            payload = response.json()
            items.extend(payload.get("items", []))
            pagination = payload.get("pagination") or {}
            page_token = pagination.get("nextPageToken") or pagination.get("next_page_token")
            if not page_token:
                return items
