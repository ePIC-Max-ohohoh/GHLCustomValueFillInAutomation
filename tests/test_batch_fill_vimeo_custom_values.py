from src.batch_fill_vimeo_custom_values import plan_account
from src.vimeo_folder_videos import VimeoVideoRecord
from src.vimeo_sync import NamedVideoRule, VimeoSyncConfig


def _video(title: str, video_id: str) -> VimeoVideoRecord:
    return VimeoVideoRecord(
        "Advisor", "Advisor", "folder", "Advisor", "folder", title, video_id,
        f"https://vimeo.com/{video_id}", "", "", f"/videos/{video_id}"
    )


def test_plan_fills_only_blank_and_skips_nonempty():
    config = VimeoSyncConfig(
        named=(),
        sequenced=(
            NamedVideoRule("vimeo_01_title", ("01. Title",)),
            NamedVideoRule("vimeo_02_other", ("02. Other",)),
        ),
    )
    values = [
        {"id": "one", "name": "01. Title", "fieldKey": "{{custom_values.vimeo_01_title}}", "value": ""},
        {"id": "two", "name": "02. Other", "fieldKey": "{{custom_values.vimeo_02_other}}", "value": "keep-me"},
    ]
    items = plan_account(
        "Advisor", "loc", values,
        [_video("01. Title", "123"), _video("02. Other", "456")], config
    )
    by_key = {item.key: item for item in items}
    assert by_key["vimeo_01_title"].status == "would_fill"
    assert by_key["vimeo_02_other"].status == "nonempty_skipped"
