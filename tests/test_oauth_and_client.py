from __future__ import annotations

import json

from src.ghl_client import GHLClient
from src.oauth import OAuthManager


class Response:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self.payload = payload or {}
        self.text = json.dumps(self.payload)
        self.content = self.text.encode()
        self.headers = {}

    def json(self):
        return self.payload


class OAuthSession:
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if url.endswith("/oauth/token"):
            return Response(
                payload={
                    "access_token": "agency-access",
                    "refresh_token": "rotated-refresh",
                    "expires_in": 86400,
                    "userType": "Company",
                    "companyId": "company-1",
                }
            )
        return Response(
            payload={
                "access_token": "location-access",
                "refresh_token": "location-refresh",
                "expires_in": 86400,
                "locationId": "loc-1",
            }
        )


def test_oauth_refreshes_agency_then_exchanges_location_token(tmp_path):
    session = OAuthSession()
    from src.oauth import TokenStore

    store = TokenStore(tmp_path / "tokens.json")
    oauth = OAuthManager(
        "client", "secret", "initial-refresh", company_id="company-1",
        token_store=store, session=session, now=lambda: 1000,
    )
    assert oauth.get_location_token("loc-1") == "location-access"
    assert session.posts[0][1]["data"]["grant_type"] == "refresh_token"
    assert session.posts[1][1]["data"] == {"companyId": "company-1", "locationId": "loc-1"}
    assert json.loads((tmp_path / "tokens.json").read_text())["refresh_token"] == "rotated-refresh"


class StaticTokenProvider:
    def get_location_token(self, location_id, force_refresh=False):
        return "location-token"

    def invalidate_location(self, location_id):
        pass


class CaptureSession:
    def __init__(self):
        self.call = None

    def request(self, method, url, **kwargs):
        self.call = (method, url, kwargs)
        return Response(payload={"customValue": {"id": "value-1"}})


def test_update_contract_uses_v3_live_name_and_value():
    session = CaptureSession()
    client = GHLClient(StaticTokenProvider(), session=session)
    client.update_custom_value("loc-1", "value-1", "Advisor Email", "new@example.com")
    method, url, kwargs = session.call
    assert method == "PUT"
    assert url.endswith("/locations/loc-1/customValues/value-1")
    assert kwargs["headers"]["Version"] == "v3"
    assert kwargs["json"] == {"name": "Advisor Email", "value": "new@example.com"}


def test_create_and_delete_custom_value_contracts():
    session = CaptureSession()
    client = GHLClient(StaticTokenProvider(), session=session)
    client.create_custom_value("loc-1", "Vimeo 01 Title", "video-url")
    method, url, kwargs = session.call
    assert method == "POST"
    assert url.endswith("/locations/loc-1/customValues")
    assert kwargs["json"] == {"name": "Vimeo 01 Title", "value": "video-url"}
    client.delete_custom_value("loc-1", "value-1")
    method, url, kwargs = session.call
    assert method == "DELETE"
    assert url.endswith("/locations/loc-1/customValues/value-1")
    assert kwargs["json"] is None
