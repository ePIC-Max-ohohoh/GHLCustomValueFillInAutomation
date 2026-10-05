from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .config import ValueRule


FIELD_KEY_RE = re.compile(r"^\{\{\s*custom_values\.([^}\s]+)\s*\}\}$")


@dataclass(frozen=True)
class LiveCustomValue:
    id: str
    name: str
    value: str
    field_key: str | None
    key: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> "LiveCustomValue":
        field_key = raw.get("fieldKey")
        return cls(
            id=str(raw.get("id", "")),
            name=str(raw.get("name", "")).strip(),
            value=str(raw.get("value", "")),
            field_key=str(field_key) if field_key is not None else None,
            key=extract_key(str(field_key)) if field_key is not None else None,
        )


@dataclass(frozen=True)
class MatchResult:
    status: str
    item: LiveCustomValue | None = None
    reason: str = ""


def extract_key(field_key: str) -> str | None:
    match = FIELD_KEY_RE.match(field_key.strip())
    return match.group(1) if match else None


class CustomValueIndex:
    def __init__(self, raw_values: list[dict[str, Any]]) -> None:
        self.items = [LiveCustomValue.from_api(item) for item in raw_values]
        self.by_key: dict[str, list[LiveCustomValue]] = {}
        self.by_name: dict[str, list[LiveCustomValue]] = {}
        for item in self.items:
            if item.key:
                self.by_key.setdefault(item.key, []).append(item)
            if item.name:
                self.by_name.setdefault(item.name, []).append(item)

    def match(self, key: str, rule: ValueRule) -> MatchResult:
        key_matches = self.by_key.get(key, [])
        if len(key_matches) == 1:
            return MatchResult("matched", key_matches[0])
        if len(key_matches) > 1:
            return MatchResult("ambiguous", reason="duplicate live fieldKey")

        if rule.display_name:
            name_matches = self.by_name.get(rule.display_name, [])
            if len(name_matches) == 1:
                return MatchResult("matched", name_matches[0])
            if len(name_matches) > 1:
                return MatchResult("ambiguous", reason="duplicate exact display name")
        return MatchResult("missing", reason="approved key does not exist in this location")

