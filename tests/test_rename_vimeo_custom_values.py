from __future__ import annotations

import pytest

from src.rename_vimeo_custom_values import RenameError, RenameRule, build_plan, canonical_key


def test_canonical_key_accepts_merge_field_format():
    assert canonical_key("{{ custom_values.advisorvimeovideo1 }}") == "advisorvimeovideo1"


def test_plan_requires_unique_source_and_clear_destination():
    rule = RenameRule("advisorvimeovideo1", "vimeo_01_title", "01. Title")
    values = [
        {
            "id": "one",
            "name": "advisor-vimeo-video-1",
            "fieldKey": "{{ custom_values.advisorvimeovideo1 }}",
            "value": "https://example.test/video",
        }
    ]
    plan = build_plan(values, [rule])
    assert plan[0].value == "https://example.test/video"
    with pytest.raises(RenameError, match="Destination keys already exist"):
        build_plan(
            values
            + [
                {
                    "id": "two",
                    "name": "collision",
                    "fieldKey": "{{ custom_values.vimeo_01_title }}",
                }
            ],
            [rule],
        )
