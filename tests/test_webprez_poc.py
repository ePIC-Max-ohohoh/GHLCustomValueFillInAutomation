from __future__ import annotations

import json
import os

import pytest

from src.webprez_poc import (
    AmbiguousAdvisorError,
    WebPrezOutput,
    WebPrezPOCError,
    WebPrezResult,
    _find_exact_advisor_container,
    _install_read_only_guards,
    _validate_notice_url,
    load_targets,
    sanitize_html,
    validate_storage_state,
)


def test_loads_exact_target_aliases(tmp_path):
    path = tmp_path / "targets.yml"
    path.write_text(
        "targets:\n"
        "  - category: Estate Life Insurance\n"
        "    titles: [Living Trust, Avoid Probate with a Living Trust]\n",
        encoding="utf-8",
    )
    targets = load_targets(path)
    assert targets[0].category == "Estate Life Insurance"
    assert targets[0].titles == (
        "Living Trust",
        "Avoid Probate with a Living Trust",
    )


def test_sanitized_html_removes_secrets_urls_and_executable_content():
    raw = (
        '<script>access_token=secret</script><a href="https://secret.example/path">Link</a>'
        '<input value="credential"><div>person@example.com password=hunter2</div>'
    )
    sanitized = sanitize_html(raw)
    assert "secret.example" not in sanitized
    assert "credential" not in sanitized
    assert "person@example.com" not in sanitized
    assert "hunter2" not in sanitized
    assert "<script" not in sanitized
    assert "[REDACTED]" in sanitized


def test_storage_state_must_exist_be_json_and_mode_0600(tmp_path):
    state = tmp_path / "webprez.json"
    state.write_text(json.dumps({"cookies": [], "origins": []}), encoding="utf-8")
    os.chmod(state, 0o644)
    with pytest.raises(WebPrezPOCError, match="permissions are too broad"):
        validate_storage_state(state)
    os.chmod(state, 0o600)
    assert validate_storage_state(state) == state


def test_only_configured_viewing_notice_host_is_accepted():
    assert _validate_notice_url(
        "https://smartmoney.talk/n-n-example", {"smartmoney.talk"}
    ).startswith("https://smartmoney.talk/")
    with pytest.raises(WebPrezPOCError, match="not in the configured allowlist"):
        _validate_notice_url("https://wrong.example/video", {"smartmoney.talk"})


class FakeExactText:
    def __init__(self, count):
        self._count = count

    def count(self):
        return self._count


class FakeContainer:
    def __init__(self, exact):
        self.exact = exact

    def is_visible(self):
        return True

    def get_by_text(self, _advisor, exact=False):
        return FakeExactText(1 if exact and self.exact else 0)


class FakeContainers:
    def __init__(self, containers):
        self.containers = containers

    def count(self):
        return len(self.containers)

    def nth(self, index):
        return self.containers[index]


class FakeAdvisorPage:
    def __init__(self, matches):
        self.containers = FakeContainers(
            [FakeContainer(True) for _ in range(matches)]
        )

    def locator(self, _selector):
        return self.containers


def test_ambiguous_exact_advisor_match_stops():
    with pytest.raises(AmbiguousAdvisorError, match="matched 2"):
        _find_exact_advisor_container(FakeAdvisorPage(2), "Barry Goldwater")


class FakeRequest:
    def __init__(self, method, url="https://app.webprez.com/path"):
        self.method = method
        self.url = url


class FakeRoute:
    def __init__(self, method):
        self.request = FakeRequest(method)
        self.action = None

    def continue_(self):
        self.action = "continued"

    def fulfill(self, status, body):
        self.action = f"fulfilled:{status}:{body}"


class FakeContext:
    def route(self, _pattern, handler):
        self.handler = handler

    def on(self, _event, _handler):
        pass


def test_read_only_guard_allows_reads_and_locally_fulfills_write_methods():
    context = FakeContext()
    blocked = []
    _install_read_only_guards(context, blocked)
    get_route = FakeRoute("GET")
    context.handler(get_route)
    assert get_route.action == "continued"
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        route = FakeRoute(method)
        context.handler(route)
        assert route.action == "fulfilled:204:"
    assert blocked == [
        "POST /path",
        "PUT /path",
        "PATCH /path",
        "DELETE /path",
    ]


def test_structured_output_explicitly_reports_zero_writes():
    output = WebPrezOutput(
        advisor="Barry Goldwater",
        catalog={"Estate Life Insurance": ["Living Trust"]},
        results=[
            WebPrezResult(
                advisor="Barry Goldwater",
                category="Estate Life Insurance",
                video_title="Living Trust",
                viewing_notice_url="https://smartmoney.talk/n-n-example",
            )
        ],
        blocked_requests=[],
    ).to_dict()
    assert output["ghl_writes"] == 0
    assert output["webprez_changes"] == 0
    assert output["results"][0]["viewing_notice_url"].startswith(
        "https://smartmoney.talk/"
    )
