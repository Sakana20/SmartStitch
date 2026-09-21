from __future__ import annotations

import os
from pathlib import Path

import pytest

from smartstitch.runtime import (
    ApplicationInstanceAlreadyRunningError,
    ApplicationInstanceLock,
    configure_bundled_media_tools,
    seed_packaged_configs,
)


def test_application_instance_lock_rejects_same_data_directory(tmp_path):
    first = ApplicationInstanceLock(tmp_path / "data")
    second = ApplicationInstanceLock(tmp_path / "data")
    first.acquire()
    try:
        with pytest.raises(ApplicationInstanceAlreadyRunningError, match="已在使用"):
            second.acquire()
    finally:
        first.release()

    second.acquire()
    second.release()


def test_bundled_media_tools_take_precedence(tmp_path, monkeypatch):
    binary_directory = tmp_path / "bin"
    binary_directory.mkdir()
    for name in ("ffmpeg", "ffprobe"):
        path = binary_directory / name
        path.write_text("binary", encoding="utf-8")
        path.chmod(0o755)
    monkeypatch.setenv("PATH", "/usr/bin:/bin")

    selected = configure_bundled_media_tools(tmp_path)

    assert selected == binary_directory
    assert os.environ["PATH"].split(os.pathsep)[0] == str(binary_directory.resolve())


def test_seed_packaged_configs_never_overwrites_user_files(tmp_path):
    source = tmp_path / "resources" / "config"
    destination = tmp_path / "support" / "config"
    source.mkdir(parents=True)
    destination.mkdir(parents=True)
    (source / "template.commented.yaml").write_text("new", encoding="utf-8")
    (source / "default.yaml").write_text("default", encoding="utf-8")
    (destination / "template.commented.yaml").write_text("user", encoding="utf-8")

    seed_packaged_configs(source, destination)

    assert (destination / "template.commented.yaml").read_text("utf-8") == "user"
    assert (destination / "default.yaml").read_text("utf-8") == "default"


def test_frozen_app_falls_back_to_seeded_writable_directories(tmp_path, monkeypatch):
    from smartstitch import api

    resources = tmp_path / "bundle-resources"
    (resources / "config").mkdir(parents=True)
    (resources / "config" / "template.commented.yaml").write_text(
        "template", encoding="utf-8"
    )
    support = tmp_path / "Application Support" / "SmartStitch"

    monkeypatch.setattr(api, "resource_root", lambda: resources)
    monkeypatch.setattr(api, "is_frozen", lambda: True)
    monkeypatch.setattr(
        api,
        "writable_config_directory",
        lambda _root: support / "config",
    )
    monkeypatch.setattr(
        api,
        "writable_data_directory",
        lambda _root: support / "data",
    )
    monkeypatch.setattr(
        api,
        "resolve_config_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            api.SharedConfigUnavailableError("missing NAS")
        ),
    )

    application = api.create_app()

    assert application.state.config_store.directory == support / "config"
    assert application.state.data_directory == support / "data"
    assert (support / "config" / "template.commented.yaml").read_text("utf-8") == "template"
    assert (support / "data" / "smartstitch.db").is_file()


def test_source_app_preserves_missing_nas_error(tmp_path, monkeypatch):
    from smartstitch import api

    monkeypatch.delenv(api.CONFIG_DIRECTORY_ENV, raising=False)
    monkeypatch.setattr(api, "resource_root", lambda: tmp_path / "source")
    monkeypatch.setattr(api, "is_frozen", lambda: False)
    monkeypatch.setattr(
        api,
        "resolve_config_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            api.SharedConfigUnavailableError("未连接 NAS：测试共享目录不存在")
        ),
    )

    with pytest.raises(api.SharedConfigUnavailableError, match="未连接 NAS"):
        api.create_app()


def test_create_app_uses_explicit_isolated_directories(tmp_path, monkeypatch):
    from smartstitch import api

    resources = tmp_path / "resources"
    config_directory = tmp_path / "isolated-config"
    data_directory = tmp_path / "isolated-data"
    (resources / "frontend").mkdir(parents=True)
    config_directory.mkdir()
    monkeypatch.delenv(api.CONFIG_DIRECTORY_ENV, raising=False)

    application = api.create_app(
        resources,
        config_directory=config_directory,
        data_directory=data_directory,
    )

    assert application.state.config_store.directory == config_directory.resolve()
    assert application.state.data_directory == data_directory.resolve()
    assert (data_directory / "smartstitch.db").is_file()


def test_duplicate_app_start_does_not_interrupt_active_job(tmp_path):
    from smartstitch import api

    (tmp_path / "config").mkdir()
    application = api.create_app(tmp_path)
    active_job = {
        "id": "active-job",
        "status": "running",
        "finished_at": None,
    }
    application.state.database_store.save("jobs", active_job)

    try:
        with pytest.raises(ApplicationInstanceAlreadyRunningError):
            api.create_app(tmp_path)

        persisted = application.state.database_store.get("jobs", "active-job")
        assert persisted is not None
        assert persisted["status"] == "running"
        assert persisted["finished_at"] is None
    finally:
        application.state.instance_lock.release()
