from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path


class CSVValidationError(ValueError):
    """Raised for file-level CSV problems that make safe parsing impossible."""


@dataclass(frozen=True)
class AdvisorRecord:
    record_name: str
    location_id: str
    values: dict[str, str]
    row_number: int
    validation_errors: tuple[str, ...] = field(default_factory=tuple)


IDENTITY_COLUMNS = {"record_name", "ghl_location_id"}


def load_advisors(path: str | Path) -> list[AdvisorRecord]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise CSVValidationError(f"CSV file not found: {csv_path}")

    with csv_path.open(newline="", encoding="utf-8-sig") as handle:
        rows = csv.reader(handle)
        try:
            headers = next(rows)
        except StopIteration as exc:
            raise CSVValidationError("CSV is empty") from exc

        normalized_headers = [header.strip() for header in headers]
        if any(not header for header in normalized_headers):
            raise CSVValidationError("CSV contains a blank header")
        duplicates = sorted({h for h in normalized_headers if normalized_headers.count(h) > 1})
        if duplicates:
            raise CSVValidationError(f"Duplicate CSV headers: {', '.join(duplicates)}")
        required = IDENTITY_COLUMNS - set(normalized_headers)
        if required:
            raise CSVValidationError(f"Missing required CSV headers: {', '.join(sorted(required))}")

        records: list[AdvisorRecord] = []
        for row_number, row in enumerate(rows, start=2):
            if not row or all(not cell.strip() for cell in row):
                continue
            errors: list[str] = []
            if len(row) != len(normalized_headers):
                errors.append(
                    f"row has {len(row)} cells but header has {len(normalized_headers)}"
                )
                row = (row + [""] * len(normalized_headers))[: len(normalized_headers)]
            raw = dict(zip(normalized_headers, row))
            record_name = raw.get("record_name", "").strip()
            location_id = raw.get("ghl_location_id", "").strip()
            if not record_name:
                errors.append("record_name is missing")
                record_name = f"Row {row_number}"
            if not location_id:
                errors.append("ghl_location_id is missing")
            values = {key: value for key, value in raw.items() if key not in IDENTITY_COLUMNS}
            records.append(
                AdvisorRecord(record_name, location_id, values, row_number, tuple(errors))
            )
    return records


def select_advisors(
    records: list[AdvisorRecord], advisor: str | None = None, location: str | None = None
) -> list[AdvisorRecord]:
    selected = records
    if advisor is not None:
        selected = [record for record in selected if record.record_name == advisor]
        if not selected:
            raise CSVValidationError(f"No exact advisor match for: {advisor}")
        if len(selected) > 1 and location is None:
            raise CSVValidationError(
                f"Multiple advisors are named {advisor!r}; select one with --location"
            )
    if location is not None:
        selected = [record for record in selected if record.location_id == location]
        if not selected:
            raise CSVValidationError(f"No advisor has Location ID: {location}")
        if len(selected) > 1:
            raise CSVValidationError(f"Duplicate Location ID in CSV: {location}")
    return selected

