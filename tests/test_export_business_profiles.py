from __future__ import annotations

import csv
import json

from src.connectivity import AgencyConnectivityClient, LocationProfile
from src.export_business_profiles import write_excel_csv


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


def test_reads_and_maps_all_business_profile_fields():
    session = Session(
        [
            Response(payload={"locations": [{"id": "loc-1", "name": "Friendly"}]}),
            Response(
                payload={
                    "location": {
                        "id": "loc-1",
                        "name": "Friendly",
                        "domain": "advisor.example.com",
                        "email": "advisor@example.com",
                        "phone": "+13105550100",
                        "website": "https://fallback.example.com",
                        "logoUrl": "https://cdn.example.com/logo.png",
                        "business": {
                            "name": "Legal Advisor, Inc.",
                            "website": "https://advisor.example.com",
                            "niche": "Financial Services",
                            "currency": "USD",
                        },
                    }
                }
            ),
        ]
    )
    profiles = AgencyConnectivityClient("token", session=session).list_location_profiles()
    assert profiles == [
        LocationProfile(
            location_id="loc-1",
            friendly_business_name="Friendly",
            legal_business_name="Legal Advisor, Inc.",
            business_email="advisor@example.com",
            business_phone="+13105550100",
            branded_domain="advisor.example.com",
            business_website="https://advisor.example.com",
            business_niche="Financial Services",
            business_currency="USD",
            business_logo_url="https://cdn.example.com/logo.png",
        )
    ]
    assert session.calls[1][0].endswith("/locations/loc-1")
    assert session.calls[1][1]["headers"]["Version"] == "v3"


def test_writes_excel_compatible_csv_and_preserves_phone_as_text(tmp_path):
    output = write_excel_csv(
        [
            LocationProfile(
                location_id="loc-1",
                friendly_business_name="Friendly",
                legal_business_name="Legal Advisor, Inc.",
                business_email="advisor@example.com",
                business_phone="+13105550100",
                branded_domain="advisor.example.com",
                business_website="https://advisor.example.com",
                business_niche="Financial Services",
                business_currency="USD",
                business_logo_url="",
            )
        ],
        tmp_path / "profiles.csv",
    )
    with output.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]["Legal Business Name"] == "Legal Advisor, Inc."
    assert rows[0]["Business Phone"] == "'+13105550100"
