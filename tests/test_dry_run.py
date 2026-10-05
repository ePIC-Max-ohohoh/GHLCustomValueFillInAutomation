from __future__ import annotations

import json

from src.csv_loader import AdvisorRecord
from src.ghl_client import GHLClient
from src.oauth import SecretRedactor
from src.reporter import write_reports
from src.updater import AdvisorUpdater


LIVE = [
    {"id": "email-id", "name": "Advisor Email", "fieldKey": "{{custom_values.advisor_email}}", "value": "old@example.com"}
]


def test_dry_run_never_writes(app_config, fake_client_factory):
    client = fake_client_factory(LIVE)
    record = AdvisorRecord("John", "loc", {"advisor_email": "new@example.com"}, 2)
    result = AdvisorUpdater(client, app_config, apply=False).process(record)
    assert result.changes[0].status == "would_update"
    assert client.writes == []


def test_apply_flag_enables_write(app_config, fake_client_factory):
    client = fake_client_factory(LIVE)
    record = AdvisorRecord("John", "loc", {"advisor_email": "new@example.com"}, 2)
    AdvisorUpdater(client, app_config, apply=True).process(record)
    assert len(client.writes) == 1


def test_report_redacts_known_secrets(tmp_path, app_config, fake_client_factory):
    secret = "super-secret-refresh-token"
    client = fake_client_factory(get_error=RuntimeError(f"failed with {secret}"))
    record = AdvisorRecord("John", "loc", {"advisor_email": "new@example.com"}, 2)
    result = AdvisorUpdater(client, app_config).process(record)
    json_path, csv_path = write_reports([result], tmp_path, SecretRedactor([secret]))
    assert secret not in json_path.read_text(encoding="utf-8")
    assert secret not in csv_path.read_text(encoding="utf-8")
    assert "REDACTED" in json_path.read_text(encoding="utf-8")


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class TokenProvider:
    def get_location_token(self, location_id, force_refresh=False):
        return "token"

    def invalidate_location(self, location_id):
        pass


class SequenceSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = 0

    def request(self, *args, **kwargs):
        self.calls += 1
        return self.responses.pop(0)


def test_retry_behavior_for_429():
    session = SequenceSession(
        [FakeResponse(429, headers={"Retry-After": "0.01"}), FakeResponse(200, {"customValues": []})]
    )
    sleeps = []
    client = GHLClient(TokenProvider(), session=session, sleep=sleeps.append)
    assert client.get_custom_values("loc") == []
    assert session.calls == 2
    assert sleeps == [0.01]

