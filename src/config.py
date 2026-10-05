from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


class ConfigurationError(ValueError):
    """Raised when configuration is unsafe or malformed."""


@dataclass(frozen=True)
class ValueRule:
    key: str
    enabled: bool
    display_name: str | None = None
    category: str | None = None
    status: str | None = None
    preferred_key: str | None = None


@dataclass(frozen=True)
class AppConfig:
    custom_values: dict[str, ValueRule]
    legacy_values: dict[str, ValueRule]
    custom_fields: dict[str, dict[str, Any]]

    @property
    def enabled_keys(self) -> set[str]:
        return {key for key, rule in self.custom_values.items() if rule.enabled}


def _read_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        raise ConfigurationError(f"Configuration file not found: {path}")
    with path.open(encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    if not isinstance(data, dict):
        raise ConfigurationError(f"Configuration must be a mapping: {path}")
    return data


def _rules(data: dict[str, Any], source: Path) -> dict[str, ValueRule]:
    result: dict[str, ValueRule] = {}
    for key, raw in data.items():
        if not isinstance(key, str) or not key.strip():
            raise ConfigurationError(f"Invalid key in {source}")
        if not isinstance(raw, dict):
            raise ConfigurationError(f"Rule for {key!r} in {source} must be a mapping")
        if "enabled" not in raw or not isinstance(raw["enabled"], bool):
            raise ConfigurationError(f"Rule for {key!r} must contain boolean enabled")
        result[key] = ValueRule(
            key=key,
            enabled=raw["enabled"],
            display_name=raw.get("display_name"),
            category=raw.get("category"),
            status=raw.get("status"),
            preferred_key=raw.get("preferred_key"),
        )
    return result


def load_config(config_dir: str | Path) -> AppConfig:
    root = Path(config_dir)
    custom_path = root / "custom_values.yml"
    legacy_path = root / "legacy_values.yml"
    fields_path = root / "custom_fields.yml"
    custom_values = _rules(_read_yaml(custom_path), custom_path)
    legacy_values = _rules(_read_yaml(legacy_path), legacy_path)
    custom_fields = _read_yaml(fields_path)

    overlap = set(custom_values) & set(legacy_values)
    active_overlap = {key for key in overlap if custom_values[key].enabled}
    if active_overlap:
        joined = ", ".join(sorted(active_overlap))
        raise ConfigurationError(f"Active Custom Values also exist in legacy config: {joined}")
    if any(bool(rule.enabled) for rule in legacy_values.values()):
        raise ConfigurationError("Legacy configuration may not enable keys in Phase 1")
    if any(bool(value.get("enabled")) for value in custom_fields.values() if isinstance(value, dict)):
        raise ConfigurationError("Custom Fields must remain disabled in Phase 1")
    return AppConfig(custom_values, legacy_values, custom_fields)

