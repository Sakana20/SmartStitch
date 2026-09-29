from __future__ import annotations

import errno
import threading
import subprocess
import time
from pathlib import Path

import pytest

from conftest import authenticated_client
from smartstitch.api import create_app
from smartstitch.codec_settings import CodecSettings, CodecSettingsStore
from smartstitch.models import AppConfig, Asset, MediaProbe, PlanItem
from smartstitch import renderer


def test_codec_settings_persist_and_write_failure_preserves_previous_value(tmp_path, monkeypatch):
    store = CodecSettingsStore(tmp_path)
    assert not store.get().software_codec_enabled
    store.save(CodecSettings(software_codec_enabled=True))
    assert CodecSettingsStore(tmp_path).get().software_codec_enabled
    import smartstitch.codec_settings as module
    def fail_replace(*_):
        raise OSError(errno.ENOSPC, "No space left on device")
    monkeypatch.setattr(module.os, "replace", fail_replace)
    with pytest.raises(OSError):
        store.save(CodecSettings(software_codec_enabled=False))
    assert store.get().software_codec_enabled
    assert not list(tmp_path.glob(".*.tmp"))


def test_codec_settings_api_validates_and_survives_restart(tmp_path):
    app = create_app(tmp_path)
    try:
        client = authenticated_client(app)
        assert client.get("/api/v1/settings/codecs").json() == {"software_codec_enabled": False}
        assert client.put("/api/v1/settings/codecs", json={"software_codec_enabled": True}).json() == {"software_codec_enabled": True}
        for invalid in ["false", 1, None]:
            assert client.put("/api/v1/settings/codecs", json={"software_codec_enabled": invalid}).status_code == 422
        assert client.put("/api/v1/settings/codecs", json={"unknown": True}).status_code == 422
    finally:
        app.state.instance_lock.release()
    app = create_app(tmp_path)
    try:
        assert authenticated_client(app).get("/api/v1/settings/codecs").json() == {"software_codec_enabled": True}
    finally:
        app.state.instance_lock.release()


def render_fixture(tmp_path):
    config = AppConfig.model_validate({
        "schema_version": 3, "workflow_type": "generic", "id": "codec-test", "name": "codec-test",
        "source_root": str(tmp_path), "timeline": ["pool_1"],
        "sources": {"pool_1": {"label": "素材", "directory": "."}},
        "benefit_overlays": {"mode": "disabled"}, "output": {"directory": str(tmp_path)},
    })
    asset = Asset(id="source", category="pool_1", path=str(tmp_path / "source.mp4"), name="source.mp4",
                  media_type="video", probe=MediaProbe(duration=1, width=720, height=1280, fps=30, video_codec="h264"))
    item = PlanItem(index=1, selections={"pool_1": asset}, output_name="out.mp4", estimated_duration=1)
    return config, item


@pytest.mark.parametrize("failure,expected", [
    ("Error initializing hwaccel", [("h264_videotoolbox", True), ("h264_videotoolbox", False)]),
    ("cannot create compression session", [("h264_videotoolbox", True), ("libx264", True)]),
])
def test_render_preserves_other_hardware_stage_on_failure(tmp_path, monkeypatch, failure, expected):
    config, item = render_fixture(tmp_path)
    calls = []
    monkeypatch.setattr(renderer, "_videotoolbox_capability", lambda: (True, None))
    monkeypatch.setattr(renderer, "hardware_decoder_available", lambda: True)
    monkeypatch.setattr(renderer, "_probe_rendered_duration", lambda *_: 1)
    def execute(command, duration, path, event, *_):
        calls.append((command[command.index("-c:v") + 1], "-hwaccel" in command))
        if len(calls) == 1:
            path.write_bytes(b"failed partial output")
            raise renderer.RenderError(failure)
        assert not path.exists()
        path.write_bytes(b"success")
    monkeypatch.setattr(renderer, "_run_ffmpeg_command", execute)
    result = renderer.render_item(config, item, tmp_path / "out.mp4", threading.Event())
    assert calls == expected
    assert result["codec_attempts"][0]["status"] == "failed"
    assert result["codec_attempts"][1]["status"] == "succeeded"
    assert (tmp_path / "out.mp4").read_bytes() == b"success"


def test_render_unknown_failures_are_bounded_and_forced_software_never_probes_hardware(tmp_path, monkeypatch):
    config, item = render_fixture(tmp_path)
    monkeypatch.setattr(renderer, "_videotoolbox_capability", lambda: (True, None))
    monkeypatch.setattr(renderer, "hardware_decoder_available", lambda: True)
    monkeypatch.setattr(renderer, "_probe_rendered_duration", lambda *_: 1)
    calls = []
    def execute(command, duration, path, event, *_):
        pair = (command[command.index("-c:v") + 1], "-hwaccel" in command)
        calls.append(pair)
        if pair[0] != "libx264":
            raise renderer.RenderError("unclassified codec failure")
        path.write_bytes(b"software success")
    monkeypatch.setattr(renderer, "_run_ffmpeg_command", execute)
    renderer.render_item(config, item, tmp_path / "out.mp4", threading.Event())
    assert calls == [("h264_videotoolbox", True), ("h264_videotoolbox", False), ("libx264", False)]
    config.output.software_codec_enabled = True
    def unexpected_probe():
        pytest.fail("forced software must not probe hardware")
    monkeypatch.setattr(renderer, "_videotoolbox_capability", unexpected_probe)
    monkeypatch.setattr(renderer, "hardware_decoder_available", unexpected_probe)
    calls.clear()
    result = renderer.render_item(config, item, tmp_path / "out2.mp4", threading.Event())
    assert calls == [("libx264", False)]
    assert result["video_decode_status"] == "software"


@pytest.mark.parametrize("reason", ["cancel", "disk", "missing", "source_changed"])
def test_render_does_not_retry_cancel_or_file_errors(tmp_path, monkeypatch, reason):
    config, item = render_fixture(tmp_path)
    monkeypatch.setattr(renderer, "_videotoolbox_capability", lambda: (True, None))
    monkeypatch.setattr(renderer, "hardware_decoder_available", lambda: True)
    event = threading.Event()
    calls = []
    def execute(*_):
        calls.append(True)
        if reason == "cancel":
            event.set()
            raise renderer.RenderError("hardware interrupted")
        if reason == "disk":
            raise OSError(errno.ENOSPC, "No space left on device")
        raise renderer.RenderError("No such file" if reason == "missing" else "源文件已变化")
    monkeypatch.setattr(renderer, "_run_ffmpeg_command", execute)
    with pytest.raises((OSError, renderer.RenderError)):
        renderer.render_item(config, item, tmp_path / "out.mp4", event)
    assert len(calls) == 1


def test_conversion_software_setting_is_snapshotted(tmp_path):
    from smartstitch.database import SQLiteStore
    from smartstitch.landscape_to_portrait import LandscapeToPortraitManager
    source = tmp_path / "source"
    source.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=320x180:r=25:d=0.16",
                    "-c:v", "libx264", str(source / "sample.mp4")], check=True)
    settings = CodecSettingsStore(tmp_path)
    settings.save(CodecSettings(software_codec_enabled=True))
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        job = manager.create(manager.preview(str(source))["id"])
        settings.save(CodecSettings(software_codec_enabled=False))
        deadline = time.monotonic() + 20
        while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
            time.sleep(.02)
            job = manager.get(job["id"])
        assert job["status"] == "completed", job
        assert job["software_codec_enabled"] is True
        assert job["items"][0]["actual_video_encoder"] == "libx264"
        assert job["items"][0]["hardware_decode_requested"] is False
        assert len(job["items"][0]["encoder_attempts"]) == 1
    finally:
        manager.shutdown()


def test_folder_concat_preferences_are_in_config_snapshot(tmp_path, monkeypatch):
    import yaml
    from smartstitch.config import ConfigStore
    from smartstitch.jobs import JobManager
    from smartstitch.models import FolderConcatRequest
    for name in ["A", "B"]:
        folder = tmp_path / name
        folder.mkdir()
        subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x240:r=25:d=0.16",
                        "-c:v", "libx264", str(folder / "sample.mp4")], check=True)
    manager = JobManager(ConfigStore(tmp_path / "config"), tmp_path / "data")
    monkeypatch.setattr(manager, "start", lambda *_: None)
    request = FolderConcatRequest(directory_a=str(tmp_path / "A"), directory_b=str(tmp_path / "B"), output_directory=str(tmp_path))
    manager.codec_settings.save(CodecSettings(software_codec_enabled=True))
    job = manager.create_folder_concat(request)
    manager.codec_settings.save(CodecSettings(software_codec_enabled=False))
    snapshot = yaml.safe_load(Path(job["config_snapshot_path"]).read_text())
    assert snapshot["output"]["software_codec_enabled"] is True
    assert snapshot["output"]["video_codec"] == "libx264"
    new_job = manager.create_folder_concat(request)
    new_snapshot = yaml.safe_load(Path(new_job["config_snapshot_path"]).read_text())
    assert new_snapshot["output"]["video_codec"] == "h264_videotoolbox"
    assert new_snapshot["output"]["software_codec_enabled"] is False
    # A legacy H.264 software setting must not defeat the application's hardware preference.
    from smartstitch.models import JobCreateRequest
    config, _ = render_fixture(tmp_path)
    config.source_root = str(tmp_path / "A")
    config.output.video_codec = "libx264"
    config.batch.minimum_free_space_gb = 0
    manager.config_store.save_config(config.id, config)
    legacy_job = manager.create(JobCreateRequest(config_id=config.id, count=1, auto_start=False))
    snapshot = yaml.safe_load(Path(legacy_job["config_snapshot_path"]).read_text())
    assert snapshot["output"]["video_codec"] == "h264_videotoolbox"
    assert manager.config_store.load(config.id).output.video_codec == "libx264"
