from __future__ import annotations

import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.config import AppConfig, ValueRule  # noqa: E402


@pytest.fixture
def app_config() -> AppConfig:
    return AppConfig(
        custom_values={
            "advisor_email": ValueRule("advisor_email", True, "Advisor Email"),
            "advisor_phone": ValueRule("advisor_phone", True, "Advisor Phone"),
            "video_bridge": ValueRule("video_bridge", True, "Video: Bridge"),
            "disabled_slot": ValueRule("disabled_slot", False, "Disabled Slot"),
        },
        legacy_values={
            "advisor_website": ValueRule(
                "advisor_website", False, status="legacy", preferred_key="advisor_website_url"
            )
        },
        custom_fields={},
    )


class FakeClient:
    def __init__(self, live=None, get_error=None, update_error_keys=None):
        self.live = live or []
        self.get_error = get_error
        self.update_error_keys = set(update_error_keys or [])
        self.writes = []

    def get_custom_values(self, location_id):
        if self.get_error:
            raise self.get_error
        return list(self.live)

    def update_custom_value(self, location_id, custom_value_id, name, value):
        if custom_value_id in self.update_error_keys:
            raise RuntimeError("temporary test failure")
        self.writes.append((location_id, custom_value_id, name, value))
        return {"customValue": {"id": custom_value_id}}


@pytest.fixture
def fake_client_factory():
    return FakeClient

