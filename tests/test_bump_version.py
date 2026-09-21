import importlib.util
from pathlib import Path

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "packaging" / "bump_version.py"
SPEC = importlib.util.spec_from_file_location("bump_version", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
bump_version = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(bump_version)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("0.1.4", "0.1.4"), ("v2.10.3", "2.10.3")],
)
def test_normalize_version(value: str, expected: str) -> None:
    assert bump_version.normalize_version(value) == expected


@pytest.mark.parametrize("value", ["1.2", "1.2.3.4", "release-1.2.3", "1.2.x"])
def test_normalize_version_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        bump_version.normalize_version(value)


@pytest.mark.parametrize(
    ("current", "expected"),
    [("0.1.3", "0.1.4"), ("1.9.99", "1.9.100")],
)
def test_next_patch_version(current: str, expected: str) -> None:
    assert bump_version.next_patch_version(current) == expected


def test_replace_version_updates_only_first_matching_field() -> None:
    text = 'version = "0.1.3"\nversion = "dependency-version"\n'

    updated = bump_version.replace_version(
        text, bump_version.PROJECT_VERSION_PATTERN, "0.1.4"
    )

    assert updated == 'version = "0.1.4"\nversion = "dependency-version"\n'
