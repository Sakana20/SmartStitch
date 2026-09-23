from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "packaging"))
from release_to_nas import choose_version  # noqa: E402


@pytest.mark.parametrize(
    ("current", "latest", "tag_exists", "expected"),
    [
        ("0.1.7", None, True, "0.1.8"),
        ("0.1.8", None, False, "0.1.8"),
        ("0.1.8", "0.1.7", False, "0.1.8"),
        ("0.1.8", "0.1.8", False, "0.1.9"),
        ("0.1.8", "0.1.7", True, "0.1.9"),
    ],
)
def test_release_version_resumes_unpublished_builds(
    current, latest, tag_exists, expected
):
    assert choose_version(current, latest, tag_exists) == expected


def test_release_refuses_source_behind_nas():
    with pytest.raises(RuntimeError, match="先同步源码"):
        choose_version("0.1.7", "0.1.8", True)
