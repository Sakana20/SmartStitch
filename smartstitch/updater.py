"""Check GitHub Releases and open the latest release in the system browser."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable
from typing import Any


GITHUB_REPOSITORY = "Sakana20/SmartStitch"
RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"
LATEST_RELEASE_API_URL = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
)
_VERSION_PATTERN = re.compile(
    r"^v?(?P<major>0|[1-9]\d*)\."
    r"(?P<minor>0|[1-9]\d*)\."
    r"(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?$"
)


class UpdateCheckError(RuntimeError):
    """The release service returned no safe, usable release information."""


def _version_key(version: str) -> tuple[int, int, int, tuple[tuple[int, Any], ...]]:
    match = _VERSION_PATTERN.fullmatch(version.strip())
    if not match:
        raise ValueError(f"不支持的版本号：{version}")
    prerelease = match.group("prerelease")
    # A stable release sorts after every prerelease of the same numeric version.
    prerelease_key: tuple[tuple[int, Any], ...] = ((2, ""),)
    if prerelease is not None:
        prerelease_key = tuple(
            (0, int(part)) if part.isdigit() else (1, part.casefold())
            for part in prerelease.split(".")
        )
    return (
        int(match.group("major")),
        int(match.group("minor")),
        int(match.group("patch")),
        prerelease_key,
    )


def is_newer_version(candidate: str, current: str) -> bool:
    """Return whether a SemVer-like GitHub tag is newer than the app version."""

    return _version_key(candidate) > _version_key(current)


def _safe_release_url(value: object) -> str:
    url = str(value or "").strip()
    parsed = urllib.parse.urlparse(url)
    expected_prefix = f"/{GITHUB_REPOSITORY}/releases/"
    if (
        parsed.scheme != "https"
        or parsed.hostname != "github.com"
        or not parsed.path.startswith(expected_prefix)
    ):
        raise UpdateCheckError("GitHub 返回了无效的 Release 地址")
    return url


class GitHubReleaseChecker:
    def __init__(
        self,
        *,
        opener: Callable[..., Any] | None = None,
        browser_opener: Callable[[str], bool] | None = None,
        timeout: float = 4.0,
    ):
        self.opener = opener or urllib.request.urlopen
        self.browser_opener = browser_opener or webbrowser.open
        self.timeout = timeout
        self.latest_release_url: str | None = None

    def check(self, current_version: str) -> dict[str, object]:
        request = urllib.request.Request(
            LATEST_RELEASE_API_URL,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": f"SmartStitch/{current_version}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with self.opener(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
            raise UpdateCheckError("暂时无法连接 GitHub 检查更新") from exc

        if not isinstance(payload, dict):
            raise UpdateCheckError("GitHub 返回了无效的更新信息")
        latest_version = str(payload.get("tag_name") or "").strip()
        release_url = _safe_release_url(payload.get("html_url"))
        try:
            update_available = is_newer_version(latest_version, current_version)
        except ValueError as exc:
            raise UpdateCheckError(str(exc)) from exc

        self.latest_release_url = release_url
        return {
            "current_version": current_version,
            "latest_version": latest_version.removeprefix("v"),
            "update_available": update_available,
            "release_url": release_url,
            "release_name": str(payload.get("name") or latest_version),
            "published_at": payload.get("published_at"),
        }

    def open_latest_release(self) -> dict[str, object]:
        url = self.latest_release_url or RELEASES_URL
        try:
            opened = bool(self.browser_opener(url))
        except OSError as exc:
            raise UpdateCheckError("无法打开系统浏览器") from exc
        if not opened:
            raise UpdateCheckError("系统浏览器未能打开 GitHub Releases")
        return {"opened": True, "release_url": url}
