from __future__ import annotations

from src.vimeo_folder_videos import (
    VimeoConnectivityClient,
    VimeoFolderSummary,
    collect_folder_videos,
    list_folder_items,
    match_advisor_folders,
)


class Response:
    def __init__(self, status=200, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload


class Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.responses.pop(0)


class Audit:
    def info(self, *_args, **_kwargs):
        pass


def test_matches_exact_normalized_names_and_keeps_duplicate_folders():
    folders = [
        VimeoFolderSummary("Carter Wilcoxson", "1", "/users/9/projects/1"),
        VimeoFolderSummary("Carter Wilcoxson", "2", "/users/9/projects/2"),
        VimeoFolderSummary("Barry Goldwater", "3", "/users/9/projects/3"),
    ]
    matches, unmatched = match_advisor_folders(
        folders, ["Carter Wilcoxson", "Barry Goldwater", "Missing Person"]
    )
    assert [(name, folder.folder_id) for name, folder in matches] == [
        ("Carter Wilcoxson", "1"),
        ("Carter Wilcoxson", "2"),
        ("Barry Goldwater", "3"),
    ]
    assert unmatched == ["Missing Person"]


def test_folder_items_paginate_with_get_only():
    session = Session(
        [
            Response(
                payload={
                    "data": [{"type": "video", "video": {"uri": "/videos/1"}}],
                    "paging": {"next": "/users/9/projects/8/items?page=2"},
                }
            ),
            Response(
                payload={
                    "data": [{"type": "video", "video": {"uri": "/videos/2"}}],
                    "paging": {"next": None},
                }
            ),
        ]
    )
    items = list_folder_items(VimeoConnectivityClient("token", session=session), "9", "8")
    assert [item["video"]["uri"] for item in items] == ["/videos/1", "/videos/2"]
    assert all(call[0] == "GET" for call in session.calls)
    assert session.calls[1][2]["params"] is None


def test_collects_videos_recursively_and_builds_links():
    session = Session(
        [
            Response(
                payload={
                    "data": [
                        {
                            "type": "video",
                            "video": {"uri": "/videos/11", "name": "Welcome"},
                        },
                        {
                            "type": "folder",
                            "folder": {"uri": "/users/9/projects/22", "name": "Nested"},
                        },
                    ],
                    "paging": {"next": None},
                }
            ),
            Response(
                payload={
                    "data": [
                        {
                            "type": "video",
                            "video": {
                                "uri": "/videos/33",
                                "name": "Deep Video",
                                "link": "https://vimeo.com/33",
                                "player_embed_url": "https://player.vimeo.com/video/33",
                            },
                        }
                    ],
                    "paging": {"next": None},
                }
            ),
        ]
    )
    records, errors = collect_folder_videos(
        VimeoConnectivityClient("token", session=session),
        "9",
        "Advisor",
        VimeoFolderSummary("Advisor", "8", "/users/9/projects/8"),
        Audit(),
    )
    assert errors == []
    assert [(row.video_id, row.folder_path) for row in records] == [
        ("11", "Advisor"),
        ("33", "Advisor / Nested"),
    ]
    assert records[0].vimeo_link == "https://vimeo.com/11"
    assert records[0].autoplay_embed_url.endswith(
        "?title=0&byline=0&portrait=0&autoplay=1"
    )
    assert records[1].autoplay_embed_url == (
        "https://player.vimeo.com/video/33?title=0&byline=0&portrait=0&autoplay=1"
    )
    assert all(call[0] == "GET" for call in session.calls)


def test_preserves_unlisted_privacy_hash_in_autoplay_link():
    session = Session(
        [
            Response(
                payload={
                    "data": [
                        {
                            "type": "video",
                            "video": {
                                "uri": "/videos/44",
                                "name": "Unlisted",
                                "player_embed_url": (
                                    "https://player.vimeo.com/video/44?h=privacyhash"
                                ),
                            },
                        }
                    ],
                    "paging": {"next": None},
                }
            )
        ]
    )
    records, errors = collect_folder_videos(
        VimeoConnectivityClient("token", session=session),
        "9",
        "Advisor",
        VimeoFolderSummary("Advisor", "8", "/users/9/projects/8"),
        Audit(),
    )
    assert errors == []
    assert records[0].autoplay_embed_url == (
        "https://player.vimeo.com/video/44?h=privacyhash"
        "&title=0&byline=0&portrait=0&autoplay=1"
    )
