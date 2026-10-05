from src.batch_create_vimeo_custom_values import plan_account
from src.rename_vimeo_custom_values import RenameRule


def test_account_plan_copies_legacy_value_and_skips_exact_destination():
    rules = [
        RenameRule("advisorvimeovideo1", "vimeo_01_title", "01. Title"),
        RenameRule("advisorvimeovideo2", "vimeo_02_title", "02. Title"),
    ]
    values = [
        {
            "id": "old-1",
            "name": "old one",
            "fieldKey": "{{ custom_values.advisorvimeovideo1 }}",
            "value": "url-1",
        },
        {
            "id": "new-2",
            "name": "02. Title",
            "fieldKey": "{{ custom_values.vimeo_02_title }}",
            "value": "url-2",
        },
    ]
    plan = plan_account("Advisor", "loc", values, rules)
    assert plan.existing == 1
    assert len(plan.creates) == 1
    assert plan.creates[0].value == "url-1"
