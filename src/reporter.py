from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .oauth import SecretRedactor
from .updater import AdvisorResult


LABELS = {
    "updated": "UPDATED",
    "would_update": "WOULD UPDATE",
    "unchanged": "UNCHANGED",
    "skipped": "SKIP",
    "missing": "MISSING",
    "ambiguous": "AMBIGUOUS",
    "legacy": "LEGACY KEY — NOT UPDATED",
    "disabled": "DISABLED — NOT UPDATED",
    "unapproved": "UNAPPROVED — NOT UPDATED",
    "validation_error": "VALIDATION ERROR",
    "api_error": "API ERROR",
}


def render_console(results: Iterable[AdvisorResult]) -> str:
    result_list = list(results)
    lines: list[str] = []
    for result in result_list:
        lines.extend([f"Advisor: {result.record_name}", f"Location: {result.location_id or '[missing]'}", ""])
        for change in result.changes:
            lines.append(f"{LABELS.get(change.status, change.status.upper()):<26} {change.key}")
            if change.reason:
                lines.append(f"  Reason: {change.reason}")
            if change.status in {"would_update", "updated"}:
                lines.append(f"  Current: {change.current_value}")
                lines.append(f"  New:     {change.new_value}")
        counts = result.counts
        lines.append("Summary: " + ", ".join(f"{value} {key}" for key, value in sorted(counts.items())))
        lines.append("APPLY MODE" if result.apply else "DRY RUN — no GHL changes were made")
        lines.append("")

    totals: dict[str, int] = {}
    for result in result_list:
        for key, value in result.counts.items():
            totals[key] = totals.get(key, 0) + value
    lines.append("BATCH SUMMARY")
    lines.append(f"Advisors processed: {len(result_list)}")
    for key, value in sorted(totals.items()):
        lines.append(f"{key.replace('_', ' ').title()}: {value}")
    return "\n".join(lines)


def write_reports(
    results: list[AdvisorResult], log_dir: str | Path, redactor: SecretRedactor | None = None
) -> tuple[Path, Path]:
    root = Path(log_dir)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = root / f"ghl-custom-values-{stamp}.json"
    csv_path = root / f"ghl-custom-values-{stamp}.csv"
    redact = (redactor or SecretRedactor()).redact

    safe_payload = json.loads(redact(json.dumps([result.to_dict() for result in results])))
    json_path.write_text(json.dumps(safe_payload, indent=2, ensure_ascii=False), encoding="utf-8")
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "record_name", "location_id", "mode", "key", "status", "reason",
            "custom_value_id", "current_value", "new_value",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for result in results:
            for change in result.changes:
                writer.writerow(
                    {
                        "record_name": result.record_name,
                        "location_id": result.location_id,
                        "mode": "apply" if result.apply else "dry-run",
                        "key": change.key,
                        "status": change.status,
                        "reason": redact(change.reason),
                        "custom_value_id": change.custom_value_id,
                        "current_value": change.current_value,
                        "new_value": change.new_value,
                    }
                )
    return json_path, csv_path

