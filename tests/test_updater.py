from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from smartstitch.updater import NASUpdateChecker, UpdateCheckError, is_newer_version


def release(root: Path, content: bytes = b"disk image") -> Path:
    updates = root / "updates"
    target = updates / "releases" / "SmartStitch-v0.2.0-macOS-arm64.dmg"
    target.parent.mkdir(parents=True)
    target.write_bytes(content)
    (updates / "latest.json").write_text(json.dumps({
        "schema_version": 1, "version": "0.2.0", "platform": "macos",
        "architecture": "arm64", "filename": target.name,
        "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest(),
    }))
    return target


@pytest.mark.parametrize(("candidate", "current", "expected"), [
    ("v0.1.3", "0.1.2", True), ("0.1.2", "0.1.2", False),
    ("0.1.1", "0.1.2", False), ("1.0.0", "0.9.9", True),
])
def test_version_comparison(candidate, current, expected):
    assert is_newer_version(candidate, current) is expected


def test_missing_nas_and_unpublished_update(tmp_path):
    checker = NASUpdateChecker(None, tmp_path / "cache")
    assert checker.check("0.1.0")["status"] == "nas_unavailable"
    checker = NASUpdateChecker(tmp_path, tmp_path / "cache")
    assert checker.check("0.1.0")["status"] == "not_published"


def test_update_check_and_verified_local_copy(tmp_path, monkeypatch):
    source = release(tmp_path)
    checker = NASUpdateChecker(tmp_path, tmp_path / "cache")
    assert checker.check("0.1.0")["update_available"] is True
    opened = []
    monkeypatch.setattr("smartstitch.updater.sys.platform", "darwin")
    monkeypatch.setattr("smartstitch.updater.subprocess.run", lambda args, **kwargs: opened.append(args))
    assert checker.open_latest_release()["opened"] is True
    assert opened[0][1] == str(tmp_path / "cache" / source.name)
    assert (tmp_path / "cache" / source.name).read_bytes() == source.read_bytes()


def test_rejects_damaged_and_unsafe_release(tmp_path, monkeypatch):
    source = release(tmp_path)
    checker = NASUpdateChecker(tmp_path, tmp_path / "cache")
    checker.check("0.1.0")
    source.write_bytes(b"bad image!")
    monkeypatch.setattr("smartstitch.updater.sys.platform", "darwin")
    with pytest.raises(UpdateCheckError, match="校验失败"):
        checker.open_latest_release()
    assert not list((tmp_path / "cache").glob("*.part"))

    manifest = tmp_path / "updates" / "latest.json"
    payload = json.loads(manifest.read_text())
    payload["filename"] = "../other.dmg"
    manifest.write_text(json.dumps(payload))
    with pytest.raises(UpdateCheckError, match="清单无效"):
        checker.check("0.1.0")
