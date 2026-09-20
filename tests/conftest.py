from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolate_runtime_environment(tmp_path, monkeypatch):
    test_home = tmp_path / "home"
    test_home.mkdir()
    monkeypatch.setenv("HOME", str(test_home))
    monkeypatch.delenv("SMARTSTITCH_CONFIG_DIRECTORY", raising=False)
    monkeypatch.delenv("SMARTSTITCH_DATA_DIRECTORY", raising=False)
