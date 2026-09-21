from __future__ import annotations

import io
import json

import pytest

from smartstitch.updater import (
    GitHubReleaseChecker,
    UpdateCheckError,
    is_newer_version,
)


class JsonResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def github_response(**overrides):
    payload = {
        "tag_name": "v0.2.0",
        "name": "SmartStitch 0.2.0",
        "html_url": "https://github.com/Sakana20/SmartStitch/releases/tag/v0.2.0",
        "published_at": "2026-09-21T08:00:00Z",
        **overrides,
    }
    return JsonResponse(json.dumps(payload).encode())


@pytest.mark.parametrize(
    ("candidate", "current", "expected"),
    [
        ("v0.1.3", "0.1.2", True),
        ("0.1.2", "0.1.2", False),
        ("0.1.1", "0.1.2", False),
        ("1.0.0", "0.9.9", True),
        ("1.0.0", "1.0.0-rc.1", True),
        ("1.0.0-rc.2", "1.0.0-rc.1", True),
    ],
)
def test_version_comparison(candidate, current, expected):
    assert is_newer_version(candidate, current) is expected


def test_check_returns_latest_release_and_caches_safe_url():
    requests = []

    def opener(request, timeout):
        requests.append((request, timeout))
        return github_response()

    checker = GitHubReleaseChecker(opener=opener)
    result = checker.check("0.1.2")

    assert result["update_available"] is True
    assert result["latest_version"] == "0.2.0"
    assert checker.latest_release_url == result["release_url"]
    assert requests[0][1] == 4.0
    assert requests[0][0].get_header("User-agent") == "SmartStitch/0.1.2"


def test_rejects_release_url_outside_expected_github_repository():
    checker = GitHubReleaseChecker(
        opener=lambda *_args, **_kwargs: github_response(
            html_url="https://example.com/download"
        )
    )

    with pytest.raises(UpdateCheckError, match="无效"):
        checker.check("0.1.2")


def test_open_latest_release_uses_cached_release_url():
    opened = []
    checker = GitHubReleaseChecker(
        opener=lambda *_args, **_kwargs: github_response(),
        browser_opener=lambda url: opened.append(url) or True,
    )
    checker.check("0.1.2")

    result = checker.open_latest_release()

    assert result["opened"] is True
    assert opened == ["https://github.com/Sakana20/SmartStitch/releases/tag/v0.2.0"]
