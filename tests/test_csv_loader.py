from __future__ import annotations

import pytest

from src.csv_loader import CSVValidationError, load_advisors, select_advisors


def write_csv(tmp_path, text):
    path = tmp_path / "advisors.csv"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_rows_and_preserves_blank_values(tmp_path):
    path = write_csv(
        tmp_path,
        "record_name,ghl_location_id,advisor_email,video_bridge\n"
        "John Smith,loc-1,john@example.com,\n",
    )
    records = load_advisors(path)
    assert records[0].record_name == "John Smith"
    assert records[0].values["video_bridge"] == ""
    assert not records[0].validation_errors


def test_duplicate_csv_columns_are_rejected(tmp_path):
    path = write_csv(
        tmp_path,
        "record_name,ghl_location_id,advisor_email,advisor_email\nJohn,loc,a,b\n",
    )
    with pytest.raises(CSVValidationError, match="Duplicate CSV headers"):
        load_advisors(path)


def test_missing_location_is_a_row_validation_error(tmp_path):
    path = write_csv(tmp_path, "record_name,ghl_location_id,advisor_email\nJohn,,a@b.com\n")
    record = load_advisors(path)[0]
    assert "ghl_location_id is missing" in record.validation_errors


def test_malformed_row_is_retained_for_safe_reporting(tmp_path):
    path = write_csv(tmp_path, "record_name,ghl_location_id,advisor_email\nJohn,loc,a,b\n")
    record = load_advisors(path)[0]
    assert any("cells" in error for error in record.validation_errors)


def test_single_advisor_selection_requires_unique_name(tmp_path):
    path = write_csv(
        tmp_path,
        "record_name,ghl_location_id\nJohn,loc-1\nJohn,loc-2\n",
    )
    records = load_advisors(path)
    with pytest.raises(CSVValidationError, match="Multiple advisors"):
        select_advisors(records, advisor="John")
    assert select_advisors(records, advisor="John", location="loc-2")[0].location_id == "loc-2"


def test_selection_is_exact(tmp_path):
    path = write_csv(tmp_path, "record_name,ghl_location_id\nJohn Smith,loc-1\n")
    with pytest.raises(CSVValidationError, match="No exact advisor match"):
        select_advisors(load_advisors(path), advisor="John")

