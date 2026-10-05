from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urljoin, urlparse

import yaml
from dotenv import load_dotenv

try:
    from playwright.sync_api import (
        BrowserContext,
        Locator,
        Page,
        Playwright,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
except ImportError:  # Keeps config/sanitizer unit tests usable before browser setup.
    BrowserContext = Any  # type: ignore[assignment,misc]
    Locator = Any  # type: ignore[assignment,misc]
    Page = Any  # type: ignore[assignment,misc]
    Playwright = Any  # type: ignore[assignment,misc]
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "https://app.webprez.com"
NOTICE_LABEL = "Video Page Hypertext Link WITH Viewing Notice"
FORBIDDEN_LABELS = (
    "Video Page Hypertext Link WITHOUT Viewing Notice",
    "Video Page Hypertext Link WITH Lead Capture",
)
SAFE_HTTP_METHODS = {"GET", "HEAD", "OPTIONS"}


class WebPrezPOCError(RuntimeError):
    pass


class SelectorFailure(WebPrezPOCError):
    def __init__(self, stage: str, message: str) -> None:
        super().__init__(message)
        self.stage = stage


class AmbiguousAdvisorError(WebPrezPOCError):
    pass


@dataclass(frozen=True)
class WebPrezTarget:
    category: str
    titles: tuple[str, ...]


@dataclass(frozen=True)
class WebPrezResult:
    advisor: str
    category: str
    video_title: str
    viewing_notice_url: str


@dataclass
class WebPrezOutput:
    advisor: str
    catalog: dict[str, list[str]]
    results: list[WebPrezResult]
    blocked_requests: list[str]
    missing_targets: list[dict[str, str]] = field(default_factory=list)
    mode: str = "read-only"

    def to_dict(self) -> dict[str, Any]:
        return {
            "advisor": self.advisor,
            "mode": self.mode,
            "catalog": self.catalog,
            "results": [asdict(item) for item in self.results],
            "missing_targets": self.missing_targets,
            "blocked_requests": self.blocked_requests,
            "ghl_writes": 0,
            "webprez_changes": 0,
        }


def load_targets(path: str | Path) -> list[WebPrezTarget]:
    config_path = Path(path)
    if not config_path.exists():
        raise WebPrezPOCError(f"WebPrez target configuration not found: {config_path}")
    try:
        payload = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise WebPrezPOCError(f"Unable to read WebPrez target configuration: {config_path}") from exc
    raw_targets = payload.get("targets")
    if not isinstance(raw_targets, list) or not raw_targets:
        raise WebPrezPOCError("WebPrez target configuration requires a non-empty targets list")
    targets: list[WebPrezTarget] = []
    for index, raw in enumerate(raw_targets, start=1):
        if not isinstance(raw, dict):
            raise WebPrezPOCError(f"WebPrez target {index} must be a mapping")
        category = raw.get("category")
        titles = raw.get("titles")
        if not isinstance(category, str) or not category.strip():
            raise WebPrezPOCError(f"WebPrez target {index} requires a category")
        if not isinstance(titles, list) or not titles or not all(
            isinstance(title, str) and title.strip() for title in titles
        ):
            raise WebPrezPOCError(f"WebPrez target {index} requires exact title aliases")
        targets.append(
            WebPrezTarget(
                category=category.strip(),
                titles=tuple(title.strip() for title in titles),
            )
        )
    return targets


def sanitize_html(html: str, limit: int = 50_000) -> str:
    """Remove executable content, field values, URLs, and common personal data."""
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?>.*?</\1>", "", html)
    text = re.sub(
        r"(?i)\b(value|href|src|action|data-url)\s*=\s*([\"']).*?\2",
        lambda match: f'{match.group(1)}="[REDACTED]"',
        text,
    )
    text = re.sub(
        r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b",
        "[REDACTED_EMAIL]",
        text,
    )
    text = re.sub(
        r"(?i)(authorization|access[_-]?token|refresh[_-]?token|password)"
        r"(\s*[=:]\s*)([^\s<>&\"']+)",
        r"\1\2[REDACTED]",
        text,
    )
    return text[:limit]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.casefold()).strip("-")
    return slug or "webprez"


def save_diagnostics(
    page: Page, artifact_root: str | Path, stage: str
) -> tuple[Path, Path]:
    root = Path(artifact_root)
    root.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    base = root / f"webprez-{_slug(stage)}-{stamp}"
    screenshot_path = base.with_suffix(".png")
    html_path = base.with_suffix(".html.txt")
    try:
        page.screenshot(path=str(screenshot_path), full_page=True)
        os.chmod(screenshot_path, 0o600)
    except Exception:
        screenshot_path.write_bytes(b"")
        os.chmod(screenshot_path, 0o600)
    try:
        sanitized = sanitize_html(page.content())
    except Exception as exc:
        sanitized = f"Unable to retrieve page HTML: {type(exc).__name__}"
    html_path.write_text(sanitized, encoding="utf-8")
    os.chmod(html_path, 0o600)
    return screenshot_path, html_path


def validate_storage_state(path: str | Path) -> Path:
    state_path = Path(path)
    if not state_path.exists() or not state_path.is_file():
        raise WebPrezPOCError(
            f"Authenticated WebPrez storage state not found: {state_path}. "
            "Capture it manually; credentials must not be placed in source code."
        )
    if stat.S_IMODE(state_path.stat().st_mode) & 0o077:
        raise WebPrezPOCError(
            f"Storage state permissions are too broad: {state_path}. Set mode to 0600."
        )
    try:
        payload = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WebPrezPOCError(f"Storage state is not valid JSON: {state_path}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("cookies", []), list):
        raise WebPrezPOCError(f"Storage state has an unexpected shape: {state_path}")
    return state_path


def _first_visible(locators: Iterable[Locator]) -> Locator | None:
    for locator in locators:
        try:
            for index in range(locator.count()):
                candidate = locator.nth(index)
                if candidate.is_visible():
                    return candidate
        except Exception:
            continue
    return None


def _click_exact(page: Page, text: str, stage: str, timeout_ms: int = 15_000) -> None:
    candidate = None
    deadline = time.monotonic() + timeout_ms / 1000
    while candidate is None and time.monotonic() < deadline:
        candidate = _first_visible(
            (
                page.get_by_role("link", name=text, exact=True),
                page.get_by_role("button", name=text, exact=True),
                page.get_by_role("tab", name=text, exact=True),
                page.get_by_text(text, exact=True),
            )
        )
        if candidate is None:
            page.wait_for_timeout(250)
    if candidate is None:
        raise SelectorFailure(stage, f"Exact visible control was not found: {text!r}")
    candidate.click()
    page.wait_for_load_state("domcontentloaded")


def _assert_authenticated(page: Page) -> None:
    lowered = page.url.casefold()
    password_fields = page.locator('input[type="password"]')
    visible_password = any(
        password_fields.nth(index).is_visible()
        for index in range(password_fields.count())
    )
    if any(part in lowered for part in ("/login", "/signin", "/sign-in")) or visible_password:
        raise WebPrezPOCError(
            "WebPrez storage state is missing or expired; authentication page was displayed."
        )


def _find_exact_advisor_container(page: Page, advisor: str) -> Locator:
    containers = page.locator("tr, [role='row'], article, [data-testid*='subaccount' i]")
    matches: list[Locator] = []
    for index in range(containers.count()):
        container = containers.nth(index)
        if not container.is_visible():
            continue
        if container.get_by_text(advisor, exact=True).count() > 0:
            matches.append(container)
    if len(matches) > 1:
        raise AmbiguousAdvisorError(
            f"Exact advisor name {advisor!r} matched {len(matches)} WebPrez sub-accounts; stopped."
        )
    if not matches:
        raise SelectorFailure(
            "advisor-match", f"No exact WebPrez sub-account match for advisor {advisor!r}"
        )
    return matches[0]


def _open_advisor(page: Page, advisor: str) -> Page:
    _click_exact(page, "My Group", "my-group-navigation")
    search = None
    search_deadline = time.monotonic() + 15
    while search is None and time.monotonic() < search_deadline:
        search = _first_visible(
            (
                page.get_by_role("textbox", name=re.compile(r"search", re.I)),
                page.locator('input[placeholder*="search" i]'),
                page.locator('input[type="search"]'),
            )
        )
        if search is None:
            page.wait_for_timeout(250)
    if search is None:
        raise SelectorFailure("advisor-search", "SubAccounts search box was not found")
    search.fill(advisor)
    container = None
    advisor_deadline = time.monotonic() + 15
    while container is None and time.monotonic() < advisor_deadline:
        try:
            container = _find_exact_advisor_container(page, advisor)
        except SelectorFailure:
            page.wait_for_timeout(250)
    if container is None:
        raise SelectorFailure(
            "advisor-match", f"No exact WebPrez sub-account match for advisor {advisor!r}"
        )
    open_control = _first_visible(
        (
            container.get_by_role(
                "button", name=re.compile(r"^(?:impersonate|view|open|login)\b", re.I)
            ),
            container.get_by_role(
                "link", name=re.compile(r"^(?:impersonate|view|open|login)\b", re.I)
            ),
            container.locator('[title*="view" i], [aria-label*="view" i]'),
        )
    )
    if open_control is None:
        icon_controls = container.locator("a, button")
        visible = [
            icon_controls.nth(index)
            for index in range(icon_controls.count())
            if icon_controls.nth(index).is_visible()
        ]
        if len(visible) != 1:
            raise SelectorFailure(
                "advisor-open",
                "Advisor row did not expose one unambiguous view/open control",
            )
        open_control = visible[0]
    context = page.context
    pages_before = len(context.pages)
    open_control.click()
    page.wait_for_timeout(750)
    advisor_page = context.pages[-1] if len(context.pages) > pages_before else page
    advisor_page.wait_for_load_state("domcontentloaded")
    return advisor_page


def _discover_titles(page: Page, configured_titles: Iterable[str]) -> list[str]:
    discovered: set[str] = set()
    main = page.locator("main")
    scope = main if main.count() and main.first.is_visible() else page.locator("body")
    for title in configured_titles:
        if scope.get_by_text(title, exact=True).count():
            discovered.add(title)
    headings = scope.locator("h3")
    for index in range(min(headings.count(), 300)):
        heading = headings.nth(index)
        if not heading.is_visible():
            continue
        text = " ".join(heading.inner_text().split())
        if 2 <= len(text) <= 160:
            discovered.add(text)
    return sorted(discovered, key=str.casefold)


def _extract_http_urls(scope: Locator) -> list[str]:
    values: list[str] = []
    fields = scope.locator("input, textarea")
    for index in range(fields.count()):
        value = fields.nth(index).input_value().strip()
        if value.startswith(("http://", "https://")):
            values.append(value)
    links = scope.locator('a[href^="http://"], a[href^="https://"]')
    for index in range(links.count()):
        value = (links.nth(index).get_attribute("href") or "").strip()
        if value:
            values.append(value)
    text = scope.inner_text()
    values.extend(re.findall(r"https?://[^\s<>\"']+", text))
    return list(dict.fromkeys(value.rstrip(".,);]") for value in values))


def _open_video_card(page: Page, title_heading: Locator, stage: str) -> None:
    """Open a visible video card using only its rendered WebPrez link.

    WebPrez currently renders cards as Next.js links. In some browser runs the
    client-side click is swallowed while the card remains visible, so prefer a
    normal authenticated GET to the rendered same-origin href. This still uses
    Playwright browser navigation and never calls a private API.
    """
    card_link = title_heading.locator("xpath=ancestor::a[1]")
    if not card_link.count():
        title_heading.click()
        page.wait_for_load_state("domcontentloaded")
        return

    href = (card_link.first.get_attribute("href") or "").strip()
    if not href:
        raise SelectorFailure(stage, "Video card did not expose a destination link")
    destination = urljoin(page.url, href)
    current = urlparse(page.url)
    target = urlparse(destination)
    if target.scheme not in {"http", "https"} or target.netloc != current.netloc:
        raise SelectorFailure(stage, "Video card destination was not a same-origin WebPrez URL")
    page.goto(destination, wait_until="domcontentloaded")


def _validate_notice_url(url: str, allowed_hosts: set[str]) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SelectorFailure("viewing-notice-url", "Extracted value is not an HTTP URL")
    hostname = (parsed.hostname or "").casefold()
    if allowed_hosts and hostname not in allowed_hosts:
        raise SelectorFailure(
            "viewing-notice-url",
            f"Viewing-notice URL host {hostname!r} is not in the configured allowlist",
        )
    return url


def _extract_viewing_notice_url(page: Page, allowed_hosts: set[str]) -> str:
    for forbidden in FORBIDDEN_LABELS:
        # These options may be present, but are deliberately never clicked or inspected.
        if forbidden == NOTICE_LABEL:
            raise AssertionError("Forbidden WebPrez label configuration is invalid")
    notice = _first_visible(
        (
            page.get_by_role("button", name=NOTICE_LABEL, exact=True),
            page.get_by_role("link", name=NOTICE_LABEL, exact=True),
            page.get_by_text(NOTICE_LABEL, exact=True),
        )
    )
    if notice is None:
        raise SelectorFailure(
            "viewing-notice-option", f"Exact option was not found: {NOTICE_LABEL!r}"
        )

    # Prefer reading the exact option's destination without navigating to it.
    # Some WebPrez layouts render this as a normal anchor, while others reveal
    # the URL only after the exact viewing-notice control is opened.
    for attribute in ("href", "data-href", "data-url"):
        value = (notice.get_attribute(attribute) or "").strip()
        if value.startswith(("http://", "https://")):
            return _validate_notice_url(value, allowed_hosts)

    option_container = notice.locator(
        "xpath=ancestor-or-self::*[self::li or self::tr or self::section "
        "or @role='group' or contains(@class, 'link')][1]"
    )
    if option_container.count():
        option_urls = _extract_http_urls(option_container.first)
        allowed_option_urls = [
            url
            for url in option_urls
            if (urlparse(url).hostname or "").casefold() in allowed_hosts
        ]
        if len(allowed_option_urls) == 1:
            return _validate_notice_url(allowed_option_urls[0], allowed_hosts)

    notice.click()
    page.wait_for_timeout(300)
    dialog = page.get_by_role("dialog")
    scope = dialog.last if dialog.count() and dialog.last.is_visible() else page.locator("body")
    urls = _extract_http_urls(scope)
    allowed = [url for url in urls if (urlparse(url).hostname or "").casefold() in allowed_hosts]
    if len(allowed) != 1:
        raise SelectorFailure(
            "viewing-notice-url",
            f"Expected exactly one viewing-notice URL, found {len(allowed)}",
        )
    return _validate_notice_url(allowed[0], allowed_hosts)


def _install_read_only_guards(context: BrowserContext, blocked: list[str]) -> None:
    def route_handler(route: Any) -> None:
        request = route.request
        method = request.method.upper()
        if method in SAFE_HTTP_METHODS:
            route.continue_()
            return
        blocked.append(f"{method} {urlparse(request.url).path}")
        # Keep non-read requests off the network while returning a benign local
        # response. WebPrez otherwise treats blocked analytics as a fatal UI
        # error even though those requests are unrelated to the requested data.
        route.fulfill(status=204, body="")

    context.route("**/*", route_handler)
    context.on("page", lambda page: page.on("download", lambda download: download.cancel()))


def capture_storage_state(
    playwright: Playwright, base_url: str, state_path: Path
) -> None:
    browser = playwright.chromium.launch(headless=False)
    context = browser.new_context()
    page = context.new_page()
    page.goto(base_url, wait_until="domcontentloaded")
    print("Log in to WebPrez in the opened browser, then return here.")
    input("Press Enter after the authenticated WebPrez page is visible: ")
    state_path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(state_path))
    os.chmod(state_path, 0o600)
    context.close()
    browser.close()
    print(f"Authenticated storage state saved: {state_path}")


def run_read_only_poc(
    playwright: Playwright,
    advisor: str,
    targets: list[WebPrezTarget],
    storage_state: Path,
    base_url: str,
    artifact_dir: Path,
    allowed_hosts: set[str],
    headless: bool,
) -> WebPrezOutput:
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(storage_state=str(storage_state), accept_downloads=False)
    blocked_requests: list[str] = []
    _install_read_only_guards(context, blocked_requests)
    page = context.new_page()
    try:
        page.goto(base_url, wait_until="domcontentloaded")
        _assert_authenticated(page)
        page = _open_advisor(page, advisor)
        _assert_authenticated(page)
        _click_exact(page, "Videos", "videos-navigation")
        videos_url = page.url

        grouped: dict[str, list[WebPrezTarget]] = {}
        for target in targets:
            grouped.setdefault(target.category, []).append(target)
        catalog: dict[str, list[str]] = {}
        for category, category_targets in grouped.items():
            _click_exact(page, category, f"category-{category}")
            page.wait_for_timeout(1_000)
            configured = [title for target in category_targets for title in target.titles]
            catalog[category] = _discover_titles(page, configured)

        results: list[WebPrezResult] = []
        missing_targets: list[dict[str, str]] = []
        for target in targets:
            # Reset the UI for every target. This is more reliable than relying
            # on the URL because WebPrez may use the same SPA URL for a video
            # catalog and an open video detail panel.
            try:
                _click_exact(page, "Videos", "videos-reset")
            except SelectorFailure:
                page.goto(videos_url, wait_until="domcontentloaded")
            _click_exact(page, target.category, f"category-{target.category}")
            title_control = None
            selected_title = ""
            title_deadline = time.monotonic() + 15
            while title_control is None and time.monotonic() < title_deadline:
                for alias in target.titles:
                    title_heading = _first_visible(
                        (
                            page.locator("h3").filter(
                                has_text=re.compile(rf"^\s*{re.escape(alias)}\s*$")
                            ),
                        )
                    )
                    if title_heading is None:
                        continue
                    title_control = title_heading
                    selected_title = alias
                    break
                if title_control is None:
                    page.wait_for_timeout(250)
            if title_control is None:
                missing_targets.append(
                    {
                        "category": target.category,
                        "title_aliases": " | ".join(target.titles),
                        "status": "NOT FOUND — SKIPPED",
                    }
                )
                continue
            _open_video_card(
                page,
                title_control,
                f"open-video-{selected_title}",
            )
            _click_exact(
                page,
                "Video Page Hypertext Links",
                f"hypertext-section-{selected_title}",
            )
            url = _extract_viewing_notice_url(page, allowed_hosts)
            results.append(
                WebPrezResult(
                    advisor=advisor,
                    category=target.category,
                    video_title=selected_title,
                    viewing_notice_url=url,
                )
            )
        return WebPrezOutput(
            advisor=advisor,
            catalog=catalog,
            results=results,
            blocked_requests=blocked_requests,
            missing_targets=missing_targets,
        )
    except SelectorFailure as exc:
        screenshot, html = save_diagnostics(page, artifact_dir, exc.stage)
        blocked_summary = ", ".join(blocked_requests) or "none"
        raise WebPrezPOCError(
            f"{exc}. Blocked request methods/paths: {blocked_summary}. "
            f"Screenshot: {screenshot}. Sanitized HTML: {html}"
        ) from exc
    except PlaywrightTimeoutError as exc:
        screenshot, html = save_diagnostics(page, artifact_dir, "playwright-timeout")
        raise WebPrezPOCError(
            f"A Playwright selector/navigation timed out. Screenshot: {screenshot}. "
            f"Sanitized HTML: {html}"
        ) from exc
    finally:
        context.close()
        browser.close()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Read-only Playwright proof of concept for WebPrez viewing-notice links."
    )
    parser.add_argument("--advisor", help="One exact WebPrez advisor name")
    parser.add_argument(
        "--storage-state",
        default=os.getenv(
            "WEBPREZ_STORAGE_STATE", str(PROJECT_ROOT / "playwright/.auth/webprez.json")
        ),
    )
    parser.add_argument(
        "--base-url", default=os.getenv("WEBPREZ_BASE_URL", DEFAULT_BASE_URL)
    )
    parser.add_argument(
        "--targets", default=str(PROJECT_ROOT / "config/webprez_targets.yml")
    )
    parser.add_argument(
        "--artifact-dir", default=str(PROJECT_ROOT / "logs/webprez-debug")
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=["smartmoney.talk"],
        help="Allowed viewing-notice URL hostname; repeat if needed.",
    )
    parser.add_argument("--headed", action="store_true", help="Show the browser window")
    parser.add_argument(
        "--capture-storage-state",
        action="store_true",
        help="Open a headed browser for manual login and save authenticated state.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    load_dotenv(PROJECT_ROOT / ".env")
    args = build_parser().parse_args(argv)
    if sync_playwright is None:
        print(
            "ERROR: Playwright is not installed. Install requirements and Chromium first.",
            file=sys.stderr,
        )
        return 2
    try:
        state_path = Path(args.storage_state)
        if not args.capture_storage_state:
            if not args.advisor or not args.advisor.strip():
                raise WebPrezPOCError("Exactly one --advisor is required")
            validate_storage_state(state_path)
            targets = load_targets(args.targets)

        with sync_playwright() as playwright:
            if args.capture_storage_state:
                capture_storage_state(playwright, args.base_url, state_path)
                return 0
            output = run_read_only_poc(
                playwright=playwright,
                advisor=args.advisor.strip(),
                targets=targets,
                storage_state=state_path,
                base_url=args.base_url,
                artifact_dir=Path(args.artifact_dir),
                allowed_hosts={host.casefold() for host in args.allowed_host},
                headless=not args.headed,
            )
            print(json.dumps(output.to_dict(), indent=2, ensure_ascii=False))
            print("READ ONLY — no WebPrez changes and no GHL writes were made")
            return 0
    except AmbiguousAdvisorError as exc:
        print(f"AMBIGUOUS ADVISOR: {exc}", file=sys.stderr)
        return 3
    except (WebPrezPOCError, PlaywrightTimeoutError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
