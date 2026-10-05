from __future__ import annotations

import random
import time
from typing import Any, Protocol

import requests

from .oauth import API_VERSION


BASE_URL = "https://services.leadconnectorhq.com"


class TokenProvider(Protocol):
    def get_location_token(self, location_id: str, force_refresh: bool = False) -> str: ...

    def invalidate_location(self, location_id: str) -> None: ...


class GHLAPIError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class GHLClient:
    def __init__(
        self,
        token_provider: TokenProvider,
        session: requests.Session | None = None,
        max_retries: int = 3,
        backoff_base: float = 0.5,
        sleep=time.sleep,
    ) -> None:
        self.token_provider = token_provider
        self.session = session or requests.Session()
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.sleep = sleep

    def _request(
        self, method: str, path: str, location_id: str, json_body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        refreshed = False
        for attempt in range(self.max_retries + 1):
            token = self.token_provider.get_location_token(location_id, force_refresh=refreshed)
            response = self.session.request(
                method,
                f"{BASE_URL}{path}",
                json=json_body,
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {token}",
                    "Version": API_VERSION,
                },
                timeout=30,
            )
            if response.status_code == 401 and not refreshed:
                self.token_provider.invalidate_location(location_id)
                refreshed = True
                continue
            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self.max_retries:
                    retry_after = response.headers.get("Retry-After")
                    try:
                        delay = float(retry_after) if retry_after else 0.0
                    except ValueError:
                        delay = 0.0
                    if delay <= 0:
                        delay = self.backoff_base * (2**attempt) + random.uniform(0, 0.1)
                    self.sleep(delay)
                    continue
            if response.status_code >= 400:
                try:
                    detail = response.json().get("message", "API request failed")
                except (ValueError, AttributeError):
                    detail = "API request failed"
                raise GHLAPIError(
                    f"{method} {path} failed with HTTP {response.status_code}: {detail}",
                    response.status_code,
                )
            if not response.content:
                return {}
            return response.json()
        raise GHLAPIError(f"{method} {path} exhausted retries")

    def get_custom_values(self, location_id: str) -> list[dict[str, Any]]:
        payload = self._request(
            "GET", f"/locations/{location_id}/customValues", location_id
        )
        values = payload.get("customValues", [])
        if not isinstance(values, list):
            raise GHLAPIError("Custom Values response had an unexpected shape")
        return values

    def update_custom_value(
        self, location_id: str, custom_value_id: str, name: str, value: str
    ) -> dict[str, Any]:
        return self._request(
            "PUT",
            f"/locations/{location_id}/customValues/{custom_value_id}",
            location_id,
            {"name": name, "value": value},
        )

    def create_custom_value(
        self, location_id: str, name: str, value: str
    ) -> dict[str, Any]:
        return self._request(
            "POST",
            f"/locations/{location_id}/customValues",
            location_id,
            {"name": name, "value": value},
        )

    def delete_custom_value(self, location_id: str, custom_value_id: str) -> dict[str, Any]:
        return self._request(
            "DELETE",
            f"/locations/{location_id}/customValues/{custom_value_id}",
            location_id,
        )
