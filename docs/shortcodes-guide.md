# Phase 1 shortcode guide

## Custom Values

Account-level values use `{{custom_values.key}}`. Only enabled keys in `config/custom_values.yml` are eligible for updates.

The application matches in this order:

1. exact live `fieldKey` extracted from `{{custom_values.key}}`;
2. exact configured display name, only if the API record has no usable key.

Duplicate matches are ambiguous and skipped. Similar names are never fuzzy-matched.

## Custom Fields

Contact-level fields use `{{contact.key}}`. They are documented in `config/custom_fields.yml` and disabled for Phase 1.

## System merge fields

Values such as `{{location.email}}`, `{{location.name}}`, `{{right_now.year}}`, and `{{contact.first_name}}` are populated by GHL and are outside this tool.

## Legacy values

Keys in `config/legacy_values.yml` belong to an old snapshot/page-builder set or remain unresolved. They are always reported and skipped. The tool never maps them to preferred modern keys automatically.

