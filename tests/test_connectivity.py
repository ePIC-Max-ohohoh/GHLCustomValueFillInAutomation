from __future__ import annotations

import json

import pytest

from src.connectivity import AgencyConnectivityClient, ConnectivityError, create_audit_logger


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}
        self.content = json.dumps(self._payload).encode()

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def test_requires_token():
    with pytest.raises(ConnectivityError, match="GHL_AGENCY_TOKEN is missing"):
        AgencyConnectivityClient("")


def test_uses_bearer_v3_and_get_only():
    session = Session([Response(payload={"locations": [{"id": "loc-1", "name": "Advisor One"}]})])
    client = AgencyConnectivityClient("private-token", session=session)
    assert client.list_locations()[0].location_id == "loc-1"
    url, kwargs = session.calls[0]
    assert url.endswith("/locations/search")
    assert kwargs["headers"]["Authorization"] == "Bearer private-token"
    assert kwargs["headers"]["Version"] == "v3"
    assert kwargs["params"] == {"skip": 0, "limit": 100, "order": "asc"}


def test_optional_company_id_is_sent():
    session = Session([Response(payload={"locations": []})])
    AgencyConnectivityClient("token", company_id="company-1", session=session).list_locations()
    assert session.calls[0][1]["params"]["companyId"] == "company-1"


def test_paginates_and_deduplicates():
    session = Session(
        [
            Response(payload={"locations": [{"id": "2", "name": "Zulu"}, {"id": "1", "name": "Alpha"}]}),
            Response(payload={"locations": [{"id": "3", "name": "Beta"}]}),
        ]
    )
    locations = AgencyConnectivityClient("token", session=session).list_locations(page_size=2)
    assert [(item.name, item.location_id) for item in locations] == [
        ("Alpha", "1"), ("Beta", "3"), ("Zulu", "2")
    ]
    assert session.calls[1][1]["params"]["skip"] == 2


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "Authentication failed"), (403, "locations.readonly"), (422, "Agency level")],
)
def test_permission_errors_are_clear_and_do_not_echo_token(status, message):
    token = "must-never-appear"
    session = Session([Response(status=status, payload={"message": token})])
    with pytest.raises(ConnectivityError, match=message) as error:
        AgencyConnectivityClient(token, session=session, max_retries=0).list_locations()
    assert token not in str(error.value)


def test_429_retries_without_writing():
    sleeps = []
    session = Session(
        [Response(429, headers={"Retry-After": "0.01"}), Response(payload={"locations": []})]
    )
    result = AgencyConnectivityClient("token", session=session, sleep=sleeps.append).list_locations()
    assert result == []
    assert sleeps == [0.01]
    assert len(session.calls) == 2


def test_audit_log_records_processed_locations_without_token(tmp_path):
    token = "secret-agency-token"
    logger, log_path = create_audit_logger(tmp_path)
    session = Session(
        [Response(payload={"locations": [{"id": "loc-1", "name": "Advisor One"}]})]
    )
    client = AgencyConnectivityClient(token, session=session, audit=logger.info)
    client.list_locations()
    for handler in logger.handlers:
        handler.flush()
    text = log_path.read_text(encoding="utf-8")
    assert "FETCH_PAGE skip=0 limit=100" in text
    assert "PROCESSED name='Advisor One' location_id='loc-1'" in text
    assert "COMPLETED total_locations=1" in text
    assert token not in text


def test_control_characters_are_removed_from_processed_fields():
    session = Session(
        [Response(payload={"locations": [{"id": "loc\t1", "name": "Advisor\nOne"}]})]
    )
    result = AgencyConnectivityClient("token", session=session).list_locations()
    assert result[0].name == "Advisor One"
    assert result[0].location_id == "loc 1"
