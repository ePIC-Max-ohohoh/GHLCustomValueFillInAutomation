from src.csv_loader import AdvisorRecord
from src.updater import AdvisorUpdater, process_batch


LIVE = [
    {"id": "email-id", "name": "Advisor Email", "fieldKey": "{{custom_values.advisor_email}}", "value": "old@example.com"},
    {"id": "phone-id", "name": "Advisor Phone", "fieldKey": "{{custom_values.advisor_phone}}", "value": "555"},
]


def advisor(values, name="John", location="loc-1", errors=()):
    return AdvisorRecord(name, location, values, 2, tuple(errors))


def test_blank_values_are_skipped(app_config, fake_client_factory):
    client = fake_client_factory(LIVE)
    result = AdvisorUpdater(client, app_config).process(advisor({"advisor_email": "   "}))
    assert result.changes[0].status == "skipped"
    assert client.writes == []


def test_identical_values_are_unchanged(app_config, fake_client_factory):
    client = fake_client_factory(LIVE)
    result = AdvisorUpdater(client, app_config).process(advisor({"advisor_phone": "555"}))
    assert result.changes[0].status == "unchanged"


def test_missing_key_is_not_created(app_config, fake_client_factory):
    client = fake_client_factory(LIVE)
    result = AdvisorUpdater(client, app_config).process(advisor({"video_bridge": "<iframe>"}))
    assert result.changes[0].status == "missing"
    assert client.writes == []


def test_legacy_and_disabled_keys_are_blocked(app_config, fake_client_factory):
    client = fake_client_factory(LIVE)
    result = AdvisorUpdater(client, app_config, apply=True).process(
        advisor({"advisor_website": "https://example.com", "disabled_slot": "value"})
    )
    assert [change.status for change in result.changes] == ["legacy", "disabled"]
    assert client.writes == []


def test_unapproved_column_is_blocked(app_config, fake_client_factory):
    result = AdvisorUpdater(fake_client_factory(LIVE), app_config).process(advisor({"random": "x"}))
    assert result.changes[0].status == "unapproved"


def test_apply_uses_live_id_and_live_name_without_renaming(app_config, fake_client_factory):
    client = fake_client_factory(LIVE)
    result = AdvisorUpdater(client, app_config, apply=True).process(
        advisor({"advisor_email": "new@example.com"})
    )
    assert result.changes[0].status == "updated"
    assert client.writes == [("loc-1", "email-id", "Advisor Email", "new@example.com")]


def test_update_failure_does_not_stop_next_value(app_config, fake_client_factory):
    client = fake_client_factory(LIVE, update_error_keys={"email-id"})
    result = AdvisorUpdater(client, app_config, apply=True).process(
        advisor({"advisor_email": "new@example.com", "advisor_phone": "777"})
    )
    assert [change.status for change in result.changes] == ["api_error", "updated"]


def test_one_advisor_api_failure_does_not_stop_batch(app_config, fake_client_factory):
    class PerLocationClient(fake_client_factory):
        def get_custom_values(self, location_id):
            if location_id == "bad":
                raise RuntimeError("forbidden")
            return LIVE

    results = process_batch(
        [advisor({"advisor_email": "x"}, "Bad", "bad"), advisor({"advisor_email": "x"}, "Good", "good")],
        PerLocationClient(),
        app_config,
        False,
    )
    assert results[0].changes[0].status == "api_error"
    assert results[1].changes[0].status == "would_update"


def test_missing_location_prevents_api_call(app_config, fake_client_factory):
    client = fake_client_factory(LIVE)
    result = AdvisorUpdater(client, app_config, apply=True).process(
        advisor({"advisor_email": "x"}, location="", errors=("ghl_location_id is missing",))
    )
    assert result.changes[0].status == "validation_error"
    assert client.writes == []

