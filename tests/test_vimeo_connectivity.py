from __future__ import annotations

import json

import pytest

from src.vimeo_connectivity import (
    VimeoConnectivityClient,
    VimeoConnectivityError,
    VimeoTeamSummary,
    create_vimeo_audit_logger,
)


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
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


def account_payload(
    folder_uri="/users/123/folders", teams_uri="/users/123/teams", team_total=2
):
    return {
        "uri": "/users/123",
        "name": "Vimeo Owner",
        "account": "advanced",
        "metadata": {
            "connections": {
                "folders": {"uri": folder_uri},
                "teams": {"uri": teams_uri, "total": team_total},
            }
        },
    }


def test_requires_token():
    with pytest.raises(VimeoConnectivityError, match="VIMEO_ACCESS_TOKEN is missing"):
        VimeoConnectivityClient("")


def test_authenticates_with_get_me_and_never_writes():
    session = Session([Response(payload=account_payload())])
    account, folder_uri = VimeoConnectivityClient("private-token", session=session).get_authenticated_account()
    assert account.name == "Vimeo Owner"
    assert account.uri == "/users/123"
    assert account.account_type == "advanced"
    assert account.teams_uri == "/users/123/teams"
    assert account.advertised_team_count == 2
    assert folder_uri == "/users/123/folders"
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://api.vimeo.com/me"
    assert kwargs["headers"]["Authorization"] == "Bearer private-token"


def test_uses_legacy_projects_fallback_when_connection_is_absent():
    payload = account_payload()
    payload["metadata"]["connections"] = {}
    account, folder_uri = VimeoConnectivityClient(
        "token", session=Session([Response(payload=payload)])
    ).get_authenticated_account()
    assert account.uri == "/users/123"
    assert folder_uri == "/me/projects"


def test_team_memberships_paginate_and_extract_owner_ids_from_supported_shapes():
    session = Session(
        [
            Response(
                payload={
                    "data": [
                        {
                            "id": 88,
                            "name": "Agency Team",
                            "owner_user_id": 9001,
                        },
                        {
                            "id": 99,
                            "user": {"name": "Owner Two", "uri": "/users/9002"},
                        },
                    ],
                    "paging": {"next": "/users/123/teams?page=2"},
                }
            ),
            Response(
                payload={
                    "teams": [
                        {
                            "team_name": "Owner Three Team",
                            "owner_id": 9003,
                            "uri": "/users/9003/team",
                        }
                    ],
                    "paging": {"next": None},
                }
            ),
        ]
    )
    teams = VimeoConnectivityClient("token", session=session).list_team_memberships(
        "/users/123/teams", page_size=2
    )
    assert [(team.name, team.owner_user_id, team.team_id) for team in teams] == [
        ("Agency Team", "9001", "88"),
        ("Owner Three Team", "9003", "[unknown]"),
        ("Owner Two", "9002", "99"),
    ]
    assert session.calls[0][2]["params"] == {"page": 1, "per_page": 2}
    assert session.calls[1][2]["params"] is None
    assert all(call[0] == "GET" for call in session.calls)


def test_current_team_fallback_extracts_owner_connection():
    session = Session(
        [
            Response(
                payload={
                    "uri": "/users/9001/team",
                    "metadata": {
                        "connections": {
                            "owner": {
                                "uri": "/users/9001",
                                "display_name": "Team Owner",
                            }
                        }
                    },
                }
            )
        ]
    )
    team = VimeoConnectivityClient("token", session=session).get_current_team()
    assert team == VimeoTeamSummary(
        owner_user_id="9001",
        team_id="[unknown]",
        name="Team Owner",
        source="current-team fallback",
    )
    assert session.calls[0][1] == "https://api.vimeo.com/me/team"


def test_team_role_uses_owner_id_and_remains_get_only():
    session = Session([Response(payload={"role": "Contributor", "active": True})])
    role = VimeoConnectivityClient("token", session=session).get_team_role("9001")
    assert role == "Contributor"
    method, url, kwargs = session.calls[0]
    assert method == "GET"
    assert url == "https://api.vimeo.com/users/9001/team/role"
    assert kwargs["params"]["fields"] == "role,permission_level,status,active"


def test_team_membership_404_is_not_reported_as_bad_authentication():
    session = Session([Response(status=404)])
    with pytest.raises(VimeoConnectivityError, match="may not have access") as error:
        VimeoConnectivityClient("token", session=session, max_retries=0).list_team_memberships(
            "/users/123/teams"
        )
    assert "authentication failed" not in str(error.value)


def test_paginates_deduplicates_and_printable_fields_are_sanitized():
    session = Session(
        [
            Response(
                payload={
                    "data": [
                        {"name": "Zulu\nAdvisor", "uri": "/users/123/folders/2"},
                        {"name": "Alpha", "uri": "/users/123/folders/1"},
                    ],
                    "paging": {"next": "/users/123/folders?page=2"},
                }
            ),
            Response(
                payload={
                    "data": [
                        {"name": "Alpha duplicate", "uri": "/users/123/folders/1"},
                        {"name": "Beta", "uri": "/users/123/folders/3"},
                    ],
                    "paging": {"next": None},
                }
            ),
        ]
    )
    folders = VimeoConnectivityClient("token", session=session).list_folders(
        "/users/123/folders", page_size=2
    )
    assert [(item.name, item.folder_id) for item in folders] == [
        ("Alpha", "1"),
        ("Beta", "3"),
        ("Zulu Advisor", "2"),
    ]
    assert len(session.calls) == 2
    assert session.calls[0][2]["params"]["per_page"] == 2
    assert session.calls[1][2]["params"] is None


@pytest.mark.parametrize(
    ("status", "message"),
    [(401, "authentication failed"), (403, "public and private"), (404, "personal or team")],
)
def test_permission_errors_are_clear_and_do_not_echo_token(status, message):
    token = "must-never-appear"
    session = Session([Response(status=status, payload={"error": token})])
    with pytest.raises(VimeoConnectivityError, match=message) as error:
        VimeoConnectivityClient(token, session=session, max_retries=0).list_folders(
            "/me/projects"
        )
    assert token not in str(error.value)


def test_rejects_external_pagination_url():
    session = Session(
        [
            Response(
                payload={
                    "data": [],
                    "paging": {"next": "https://malicious.example/folders?page=2"},
                }
            )
        ]
    )
    with pytest.raises(VimeoConnectivityError, match="unexpected external URL"):
        VimeoConnectivityClient("token", session=session).list_folders("/me/projects")


def test_429_retries_without_writing():
    sleeps = []
    session = Session(
        [
            Response(status=429, headers={"Retry-After": "0.01"}),
            Response(payload={"data": [], "paging": {"next": None}}),
        ]
    )
    result = VimeoConnectivityClient(
        "token", session=session, sleep=sleeps.append
    ).list_folders("/me/projects")
    assert result == []
    assert sleeps == [0.01]
    assert all(call[0] == "GET" for call in session.calls)


def test_audit_log_is_private_and_never_contains_token(tmp_path):
    token = "secret-vimeo-token"
    logger, log_path = create_vimeo_audit_logger(tmp_path)
    session = Session(
        [
            Response(
                payload={
                    "data": [{"name": "Advisor One", "uri": "/users/1/folders/9"}],
                    "paging": {"next": None},
                }
            )
        ]
    )
    VimeoConnectivityClient(token, session=session, audit=logger.info).list_folders(
        "/users/1/folders"
    )
    for handler in logger.handlers:
        handler.flush()
    text = log_path.read_text(encoding="utf-8")
    assert "PROCESSED_FOLDER name='Advisor One' id='9'" in text
    assert token not in text
    assert log_path.stat().st_mode & 0o777 == 0o600
