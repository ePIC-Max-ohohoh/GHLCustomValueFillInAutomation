from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Protocol

from .config import AppConfig
from .csv_loader import AdvisorRecord
from .matcher import CustomValueIndex


class CustomValuesClient(Protocol):
    def get_custom_values(self, location_id: str) -> list[dict[str, Any]]: ...

    def update_custom_value(
        self, location_id: str, custom_value_id: str, name: str, value: str
    ) -> dict[str, Any]: ...


@dataclass
class ChangeResult:
    key: str
    status: str
    reason: str = ""
    custom_value_id: str | None = None
    current_value: str | None = None
    new_value: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AdvisorResult:
    record_name: str
    location_id: str
    apply: bool
    changes: list[ChangeResult] = field(default_factory=list)
    validation_errors: list[str] = field(default_factory=list)

    def add(self, key: str, status: str, **kwargs: Any) -> None:
        self.changes.append(ChangeResult(key=key, status=status, **kwargs))

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for change in self.changes:
            counts[change.status] = counts.get(change.status, 0) + 1
        return counts

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_name": self.record_name,
            "location_id": self.location_id,
            "mode": "apply" if self.apply else "dry-run",
            "validation_errors": self.validation_errors,
            "counts": self.counts,
            "changes": [change.to_dict() for change in self.changes],
        }


class AdvisorUpdater:
    def __init__(self, client: CustomValuesClient, config: AppConfig, apply: bool = False) -> None:
        self.client = client
        self.config = config
        self.apply = apply

    def process(self, advisor: AdvisorRecord) -> AdvisorResult:
        result = AdvisorResult(advisor.record_name, advisor.location_id, self.apply)
        if advisor.validation_errors:
            result.validation_errors.extend(advisor.validation_errors)
            result.add("__advisor__", "validation_error", reason="; ".join(advisor.validation_errors))
            return result
        try:
            live_values = self.client.get_custom_values(advisor.location_id)
        except Exception as exc:
            result.add("__advisor__", "api_error", reason=str(exc))
            return result

        index = CustomValueIndex(live_values)
        for key, raw_source in advisor.values.items():
            source = raw_source.strip()
            if key in self.config.legacy_values:
                legacy = self.config.legacy_values[key]
                result.add(
                    key,
                    "legacy",
                    reason=f"{legacy.status or 'legacy'} key — not updated",
                    new_value=raw_source,
                )
                continue
            rule = self.config.custom_values.get(key)
            if rule is None:
                result.add(key, "unapproved", reason="CSV column is not in the allowlist")
                continue
            if not rule.enabled:
                result.add(key, "disabled", reason="key is disabled in configuration")
                continue
            if not source:
                result.add(key, "skipped", reason="source value is blank")
                continue

            match = index.match(key, rule)
            if match.status == "missing":
                result.add(key, "missing", reason=match.reason, new_value=raw_source)
                continue
            if match.status == "ambiguous" or match.item is None:
                result.add(key, "ambiguous", reason=match.reason, new_value=raw_source)
                continue

            live = match.item
            if live.value == raw_source:
                result.add(
                    key,
                    "unchanged",
                    custom_value_id=live.id,
                    current_value=live.value,
                    new_value=raw_source,
                )
                continue
            if not self.apply:
                result.add(
                    key,
                    "would_update",
                    custom_value_id=live.id,
                    current_value=live.value,
                    new_value=raw_source,
                )
                continue
            try:
                # Passing the live name back is required by HighLevel and guarantees no rename.
                self.client.update_custom_value(
                    advisor.location_id, live.id, live.name, raw_source
                )
                result.add(
                    key,
                    "updated",
                    custom_value_id=live.id,
                    current_value=live.value,
                    new_value=raw_source,
                )
            except Exception as exc:
                result.add(
                    key,
                    "api_error",
                    reason=str(exc),
                    custom_value_id=live.id,
                    current_value=live.value,
                    new_value=raw_source,
                )
        return result


def process_batch(
    advisors: list[AdvisorRecord], client: CustomValuesClient, config: AppConfig, apply: bool
) -> list[AdvisorResult]:
    updater = AdvisorUpdater(client, config, apply)
    return [updater.process(advisor) for advisor in advisors]

