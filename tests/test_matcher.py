from src.config import ValueRule
from src.matcher import CustomValueIndex, extract_key


def test_extracts_live_key_with_whitespace():
    assert extract_key("{{ custom_values.advisor_email }}") == "advisor_email"


def test_exact_key_match_has_priority():
    index = CustomValueIndex(
        [
            {"id": "1", "name": "Wrong Display", "fieldKey": "{{custom_values.advisor_email}}", "value": "a"},
            {"id": "2", "name": "Advisor Email", "fieldKey": "{{custom_values.other}}", "value": "b"},
        ]
    )
    result = index.match("advisor_email", ValueRule("advisor_email", True, "Advisor Email"))
    assert result.status == "matched"
    assert result.item.id == "1"


def test_exact_display_name_fallback_when_field_key_unavailable():
    index = CustomValueIndex([{"id": "1", "name": "Advisor Email", "value": "a"}])
    result = index.match("advisor_email", ValueRule("advisor_email", True, "Advisor Email"))
    assert result.status == "matched"


def test_missing_key_is_reported():
    index = CustomValueIndex([])
    result = index.match("advisor_email", ValueRule("advisor_email", True, "Advisor Email"))
    assert result.status == "missing"


def test_duplicate_exact_keys_are_ambiguous():
    index = CustomValueIndex(
        [
            {"id": "1", "name": "A", "fieldKey": "{{custom_values.advisor_email}}", "value": "a"},
            {"id": "2", "name": "B", "fieldKey": "{{custom_values.advisor_email}}", "value": "b"},
        ]
    )
    result = index.match("advisor_email", ValueRule("advisor_email", True, "Advisor Email"))
    assert result.status == "ambiguous"


def test_matching_is_not_fuzzy():
    index = CustomValueIndex(
        [{"id": "1", "name": "Advisor email address", "fieldKey": "{{custom_values.advisor_emailaddress}}", "value": "a"}]
    )
    result = index.match("advisor_email", ValueRule("advisor_email", True, "Advisor Email"))
    assert result.status == "missing"

