from __future__ import annotations

from src.vimeo_folder_videos import VimeoVideoRecord
from src.vimeo_sync import (
    VimeoSyncConfig,
    NamedVideoRule,
    build_sync_report,
    classify_video,
    generated_player_url,
    render_sync_report,
)


def video(title: str, video_id: str) -> VimeoVideoRecord:
    return VimeoVideoRecord(
        advisor_name="Barry Goldwater",
        top_folder_name="Barry Goldwater",
        top_folder_id="folder-1",
        folder_path="Barry Goldwater",
        folder_id="folder-1",
        video_name=title,
        video_id=video_id,
        vimeo_link=f"https://vimeo.com/{video_id}",
        player_embed_url=f"https://player.vimeo.com/video/{video_id}?h=private",
        autoplay_embed_url="ignored-by-sync",
        video_uri=f"/videos/{video_id}",
    )


def config() -> VimeoSyncConfig:
    return VimeoSyncConfig(
        named=(
            NamedVideoRule("video_bridge", ("{advisor} - Bridge",)),
            NamedVideoRule("video_neverstop", ("B.Goldwater - NeverStop",)),
        )
    )


def live(key: str, value: str, item_id: str) -> dict[str, str]:
    return {
        "id": item_id,
        "name": key,
        "fieldKey": f"{{{{custom_values.{key}}}}}",
        "value": value,
    }


def test_named_mapping_is_exact_and_sequence_key_is_dynamic():
    named = video("Barry Goldwater - Bridge", "1182528123")
    sequence = video("12. Example Video", "1175572416")
    unmapped = video("AI Test Video", "1105581748")
    assert classify_video(named, "Barry Goldwater", config()) == (
        "named",
        "video_bridge",
    )
    assert classify_video(sequence, "Barry Goldwater", config()) == (
        "sequenced",
        "advisorvimeovideo12",
    )
    assert classify_video(unmapped, "Barry Goldwater", config()) == (
        "unmapped",
        None,
    )


def test_configured_semantic_sequence_key_overrides_numeric_fallback():
    semantic = VimeoSyncConfig(
        named=(),
        sequenced=(
            NamedVideoRule(
                "vimeo_01_built_for_rising_interest_rates",
                ("01. Built for Rising Interest Rates",),
            ),
        ),
    )
    assert classify_video(
        video("01. Built for Rising Interest Rates", "1175550578"),
        "Barry Goldwater",
        semantic,
    ) == ("sequenced", "vimeo_01_built_for_rising_interest_rates")


def test_dry_run_validates_live_keys_and_compares_exact_generated_urls():
    bridge_url = generated_player_url("1182528123")
    report = build_sync_report(
        "Barry Goldwater",
        "location-1",
        [
            video("Barry Goldwater - Bridge", "1182528123"),
            video("B.Goldwater - NeverStop", "1192689476"),
            video("01. Built for Rising Interest Rates", "1175550578"),
            video("12. Example Video", "1175572416"),
            video("AI Test Video", "1105581748"),
        ],
        [
            live("video_bridge", bridge_url, "cv-bridge"),
            live("video_neverstop", "old", "cv-neverstop"),
            live("advisorvimeovideo1", "old", "cv-1"),
        ],
        config(),
        apply=False,
    )
    by_title = {item.title: item for item in report.items}
    assert by_title["Barry Goldwater - Bridge"].status == "unchanged"
    assert by_title["B.Goldwater - NeverStop"].status == "would_update"
    assert by_title["01. Built for Rising Interest Rates"].status == "would_update"
    assert by_title["12. Example Video"].status == "no_target_key"
    assert by_title["AI Test Video"].status == "unmapped"
    assert by_title["B.Goldwater - NeverStop"].generated_url == (
        "https://player.vimeo.com/video/1192689476"
        "?title=0&byline=0&portrait=0&autoplay=1"
    )
    assert "?h=private" not in by_title["B.Goldwater - NeverStop"].generated_url
    assert report.counts == {
        "named_matched": 2,
        "sequenced_matched": 2,
        "would_update": 2,
        "updated": 0,
        "unchanged": 1,
        "missing_ghl_keys": 1,
        "unmapped_vimeo_videos": 1,
    }
    rendered = render_sync_report(report)
    assert "NO TARGET KEY — SKIPPED" in rendered
    assert "UNMAPPED — SKIPPED" in rendered
    assert "→ Current GHL value: old" in rendered
    assert (
        "→ Proposed GHL value: https://player.vimeo.com/video/1192689476"
        "?title=0&byline=0&portrait=0&autoplay=1"
    ) in rendered
    assert rendered.endswith("DRY RUN — NO CHANGES MADE")


def test_duplicate_target_is_skipped_instead_of_last_video_winning():
    report = build_sync_report(
        "Barry Goldwater",
        "location-1",
        [
            video("Barry Goldwater - Bridge", "1"),
            video("Barry Goldwater - Bridge", "2"),
        ],
        [live("video_bridge", "old", "cv-bridge")],
        config(),
    )
    assert [item.status for item in report.items] == [
        "duplicate_target",
        "duplicate_target",
    ]


def test_apply_classification_only_marks_existing_changed_targets_updated():
    report = build_sync_report(
        "Barry Goldwater",
        "location-1",
        [
            video("B.Goldwater - NeverStop", "1192689476"),
            video("12. Example Video", "1175572416"),
        ],
        [live("video_neverstop", "old", "cv-neverstop")],
        config(),
        apply=True,
    )
    assert report.items[0].status == "updated"
    assert report.items[0].custom_value_id == "cv-neverstop"
    assert report.items[1].status == "no_target_key"
