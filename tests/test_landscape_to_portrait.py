from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from conftest import authenticated_client
from smartstitch.api import create_app
from smartstitch.database import SQLiteStore
from smartstitch.landscape_to_portrait import LandscapeToPortraitManager, TABLE, TERMINAL, inspect, probe

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")


def clip(path: Path, size="160x90", fps="25", audio=False, duration="0.4") -> None:
    args = ["ffmpeg", "-v", "error", "-nostdin", "-y", "-f", "lavfi", "-i", f"testsrc2=s={size}:r={fps}:d={duration}"]
    if audio:
        args += ["-f", "lavfi", "-i", f"sine=frequency=440:duration={duration}", "-c:a", "aac"]
    subprocess.run([*args, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)], check=True)


def wait(manager, job_id):
    deadline = time.monotonic() + 35
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job["status"] in TERMINAL:
            return job
        time.sleep(.05)
    pytest.fail("conversion did not finish")


def test_batch_api_audio_pixels_names_and_history(tmp_path):
    (tmp_path / "config").mkdir()
    source = tmp_path / "含 空格素材"
    source.mkdir()
    clip(source / "横屏.mp4", audio=True)
    shutil.copyfile(source / "横屏.mp4", source / "横屏.mov")
    clip(source / "竖屏.mp4", size="90x160")
    (source / "损坏.mp4").write_text("broken")
    (source / "._横屏.mp4").write_text("ignore")
    (source / "sub").mkdir()
    shutil.copyfile(source / "横屏.mp4", source / "sub" / "nested.mp4")
    output = source / "横改竖"
    output.mkdir()
    existing = output / "横屏_横改竖.mp4"
    existing.write_bytes(b"must remain")
    app = create_app(tmp_path)
    client = authenticated_client(app)
    manager = app.state.portrait_manager
    try:
        response = client.post("/api/v1/tools/landscape-to-portrait/preview", json={"source_directory": str(source)})
        assert response.status_code == 200, response.text
        preview = response.json()
        assert (preview["pending_count"], preview["skipped_count"], preview["invalid_count"]) == (4, 0, 0)
        created = client.post("/api/v1/tools/landscape-to-portrait", json={"preview_id": preview["id"]})
        assert created.status_code == 200, created.text
        job = wait(manager, created.json()["id"])
        assert job["status"] == "completed", job
        assert (job["succeeded"], job["skipped_count"], job["invalid_count"], job["completed"]) == (2, 1, 1, 4)
        assert existing.read_bytes() == b"must remain"
        files = [Path(i["output_path"]) for i in job["items"] if i["status"] == "completed"]
        assert {p.name for p in files} == {"横屏_横改竖_2.mp4", "横屏_横改竖_3.mp4"}
        for path in files:
            info = inspect(path)
            assert (info["width"], info["height"], info["fps"], info["has_audio"]) == (720, 1280, "25", True)
        # Compare clean foreground to independently scaled source; compare background to it.
        def pixels(path, graph):
            return subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vf", graph,
                                   "-frames:v", "1", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"], capture_output=True, check=True).stdout
        import numpy as np
        expected = np.frombuffer(pixels(source / "横屏.mp4", "scale=720:404"), dtype=np.uint8).astype(float)
        foreground = np.frombuffer(pixels(files[0], "crop=720:404:0:438"), dtype=np.uint8).astype(float)
        assert np.abs(expected - foreground).mean() < 12
        full = np.frombuffer(pixels(files[0], "null"), dtype=np.uint8).reshape(1280, 720, 3).astype(float)
        assert np.abs(np.diff(full[30:330], axis=1)).mean() < np.abs(np.diff(full[450:800], axis=1)).mean()
        assert not list(output.glob(".*.tmp.mp4"))
        assert client.get("/api/v1/tools/landscape-to-portrait/jobs").json()[0]["id"] == job["id"]
        restarted = LandscapeToPortraitManager(app.state.database_store)
        assert restarted.get(job["id"])["status"] == "completed"
        assert client.delete(f"/api/v1/tools/landscape-to-portrait/{job['id']}").status_code == 200
        assert all(path.exists() for path in files)
    finally:
        manager.shutdown()
        app.state.instance_lock.release()


def test_changed_input_and_preflight_consumption(tmp_path):
    clip(tmp_path / "clip.mp4")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    preview = manager.preview(str(tmp_path))
    clip(tmp_path / "added.mp4")
    with pytest.raises(ValueError, match="已变化"):
        manager.create(preview["id"])
    assert not (tmp_path / "横改竖").exists()
    preview = manager.preview(str(tmp_path))
    job = manager.create(preview["id"])
    try:
        with pytest.raises(ValueError):
            manager.create(preview["id"])
        assert wait(manager, job["id"])["status"] == "completed"
        with pytest.raises(ValueError, match="重新读取"):
            manager.create(preview["id"])
    finally:
        manager.shutdown()


def test_fractional_fps_silent_and_rescan(tmp_path):
    clip(tmp_path / "silent.MP4", fps="30000/1001")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        preview = manager.preview(str(tmp_path), str(tmp_path))
        job = wait(manager, manager.create(preview["id"])["id"])
        assert job["status"] == "completed", job
        output = Path(job["items"][0]["output_path"])
        info = inspect(output)
        assert info["fps"] == "30000/1001" and not info["has_audio"]
        assert len(manager.preview(str(tmp_path))["items"]) == 1
    finally:
        manager.shutdown()


def test_cancel_and_restart_interruption(tmp_path, monkeypatch):
    import smartstitch.landscape_to_portrait as module
    clip(tmp_path / "clip.mp4", duration="1")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    original = module.command
    def slow_command(*args):
        command = original(*args)
        command.insert(command.index("-i"), "-re")
        return command
    monkeypatch.setattr(module, "command", slow_command)
    preview = manager.preview(str(tmp_path))
    job = manager.create(preview["id"])
    with pytest.raises(ValueError, match="不能删除"):
        manager.delete(job["id"])
    deadline = time.monotonic() + 3
    while manager.process is None and time.monotonic() < deadline:
        time.sleep(.01)
    assert manager.process is not None
    manager.cancel(job["id"])
    job = wait(manager, job["id"])
    assert job["status"] == "cancelled"
    assert all(i["status"] == "cancelled" for i in job["items"])
    assert not list((tmp_path / "横改竖").iterdir())
    job.update(id="interrupted", status="running")
    job["items"][0]["status"] = "running"
    manager.store.save(TABLE, job)
    restarted = LandscapeToPortraitManager(manager.store)
    assert restarted.get("interrupted")["items"][0]["status"] == "interrupted"


def test_rotation_sar_and_variable_rate(tmp_path):
    clip(tmp_path / "vertical.mp4", size="90x160")
    subprocess.run(["ffmpeg", "-v", "error", "-display_rotation", "90", "-noautorotate",
                    "-i", str(tmp_path / "vertical.mp4"), "-c", "copy", str(tmp_path / "rotated.mp4")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=120x90:r=25:d=0.4",
                    "-vf", "setsar=2/1", "-c:v", "libx264", str(tmp_path / "sar.mp4")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=25:d=0.8",
                    "-vf", "setpts='if(lt(N,5),N/(25*TB),(5/25+(N-5)/12.5)/TB)'",
                    "-fps_mode", "vfr", "-c:v", "libx264", str(tmp_path / "vfr.mp4")], check=True)
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        preview = manager.preview(str(tmp_path))
        assert all(i["status"] == "pending" and "width" not in i for i in preview["items"])
        job = wait(manager, manager.create(preview["id"])["id"])
        items = {i["name"]: i for i in job["items"]}
        assert items["vfr.mp4"]["status"] == "completed", items["vfr.mp4"]
        def timestamps(path):
            import json
            data = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                   "-show_frames", "-show_entries", "frame=best_effort_timestamp_time",
                                   "-of", "json", str(path)], capture_output=True, text=True, check=True)
            return [float(f["best_effort_timestamp_time"]) for f in json.loads(data.stdout)["frames"]]
        before = timestamps(tmp_path / "vfr.mp4")
        after = timestamps(Path(items["vfr.mp4"]["output_path"]))
        assert len(before) == len(after) and len(before) > 1
        assert after == pytest.approx(before, abs=.001)

        assert items["rotated.mp4"]["width"] == 160
        assert items["sar.mp4"]["width"] == 240
        assert (job["skipped_count"], job["invalid_count"], job["completed"]) == (1, 0, 4)
        assert job["status"] == "completed", job
        assert job["succeeded"] == 3
    finally:
        manager.shutdown()


def test_one_file_failure_continues(tmp_path, monkeypatch):
    import smartstitch.landscape_to_portrait as module
    clip(tmp_path / "a.mp4")
    clip(tmp_path / "b.mp4")
    original = module.command
    def failing_command(source, target, item):
        result = original(source, target, item)
        if source.name == "a.mp4":
            result.insert(1, "-invalid_portrait_test_option")
        return result
    monkeypatch.setattr(module, "command", failing_command)
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        job = wait(manager, manager.create(manager.preview(str(tmp_path))["id"])["id"])
        assert job["status"] == "partial_failed", job
        assert (job["succeeded"], job["failed"], job["completed"]) == (1, 1, 2)
        assert job["items"][0]["error"]
        assert not list((tmp_path / "横改竖").glob(".*.tmp.mp4"))
    finally:
        manager.shutdown()


def test_audio_offset_and_frame_count_are_preserved(tmp_path):
    source = tmp_path / "offset.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=160x90:r=24:d=0.5",
                    "-itsoffset", "0.2", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
                    "-c:v", "libx264", "-c:a", "aac", str(source)], check=True)
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        job = wait(manager, manager.create(manager.preview(str(tmp_path))["id"])["id"])
        assert job["status"] == "completed", job
        result = probe(Path(job["items"][0]["output_path"]))
        original = probe(source)
        def streams(data):
            return {s["codec_type"]: s for s in data["streams"]}
        before, after = streams(original), streams(result)
        assert before["video"]["nb_frames"] == after["video"]["nb_frames"] == "12"
        offset_before = float(before["audio"]["start_time"]) - float(before["video"]["start_time"])
        offset_after = float(after["audio"]["start_time"]) - float(after["video"]["start_time"])
        assert abs(offset_before - offset_after) < .03
    finally:
        manager.shutdown()


def test_1080p_api_export_and_resolution_validation(tmp_path):
    (tmp_path / "config").mkdir()
    source = tmp_path / "input"
    source.mkdir()
    clip(source / "clip.mp4", audio=True, duration="0.16")
    app = create_app(tmp_path)
    manager = app.state.portrait_manager
    client = authenticated_client(app)
    try:
        invalid = client.post("/api/v1/tools/landscape-to-portrait/preview", json={
            "source_directory": str(source), "resolution": "4k"})
        assert invalid.status_code == 422
        response = client.post("/api/v1/tools/landscape-to-portrait/preview", json={
            "source_directory": str(source), "resolution": "1080p"})
        assert response.status_code == 200, response.text
        preview = response.json()
        assert (preview["output_width"], preview["output_height"]) == (1080, 1920)
        created = client.post("/api/v1/tools/landscape-to-portrait", json={"preview_id": preview["id"]})
        assert created.status_code == 200, created.text
        job = wait(manager, created.json()["id"])
        assert job["status"] == "completed", job
        info = inspect(Path(job["items"][0]["output_path"]))
        assert (info["width"], info["height"], info["fps"], info["has_audio"]) == (1080, 1920, "25", True)
        assert probe(Path(job["items"][0]["output_path"]))["streams"][0]["nb_frames"] == "4"
        restarted = LandscapeToPortraitManager(app.state.database_store)
        assert restarted.get(job["id"])["resolution"] == "1080p"
        assert restarted.get(job["id"])["output_width"] == 1080
    finally:
        manager.shutdown()
        app.state.instance_lock.release()


def hardware_plan():
    return {"policy": "videotoolbox_preferred", "planned_video_encoder": "h264_videotoolbox",
            "hardware_encoder": "h264_videotoolbox", "hardware_acceleration_available": True,
            "capability_error": None}


def test_hardware_failure_retries_software_and_persists_reason(tmp_path, monkeypatch):
    import smartstitch.landscape_to_portrait as module
    monkeypatch.setattr(module, "preferred_encoder_plan", hardware_plan)
    clip(tmp_path / "a.mp4", duration="0.16")
    clip(tmp_path / "b.mp4", duration="0.16")
    calls = []
    original = module.command
    def reject_hardware(source, target, item):
        encoder = item["actual_video_encoder"]
        calls.append((source.name, encoder))
        if encoder == "h264_videotoolbox":
            return ["ffmpeg", "-invalid_hardware_encoder_test"]
        assert not target.exists(), "failed hardware output must be cleared before retry"
        return original(source, target, item)
    monkeypatch.setattr(module, "command", reject_hardware)
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        job = wait(manager, manager.create(manager.preview(str(tmp_path))["id"])["id"])
        assert job["status"] == "completed", job
        assert calls == [("a.mp4", "h264_videotoolbox"), ("a.mp4", "libx264"), ("b.mp4", "libx264")]
        assert job["encoding"]["actual_video_encoders"] == ["libx264"]
        assert job["encoding"]["fallback_count"] == 2
        assert [a["status"] for a in job["items"][0]["encoder_attempts"]] == ["failed", "succeeded"]
        assert all(i["encoder_fallback_reason"] for i in job["items"])
        assert not list((tmp_path / "横改竖").glob(".*.tmp.mp4"))
        restarted = LandscapeToPortraitManager(manager.store)
        assert restarted.get(job["id"])["encoding"]["fallback_reason"]
    finally:
        manager.shutdown()


def test_cancel_during_hardware_does_not_retry_software(tmp_path, monkeypatch):
    import smartstitch.landscape_to_portrait as module
    monkeypatch.setattr(module, "preferred_encoder_plan", hardware_plan)
    clip(tmp_path / "clip.mp4", duration="0.16")
    calls = []
    def cancel_render(job, item, _temporary):
        calls.append(item["actual_video_encoder"])
        manager.cancel(job["id"])
        raise ValueError("hardware session terminated by cancellation")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    monkeypatch.setattr(manager, "_render_and_verify", cancel_render)
    try:
        job = wait(manager, manager.create(manager.preview(str(tmp_path))["id"])["id"])
        assert job["status"] == "cancelled"
        assert calls == ["h264_videotoolbox"]
        assert job["encoding"]["fallback_count"] == 0
        assert job["items"][0]["encoder_attempts"][0]["status"] == "cancelled"
    finally:
        manager.shutdown()


def test_invalid_hardware_output_retries_before_publishing(tmp_path, monkeypatch):
    import smartstitch.landscape_to_portrait as module
    monkeypatch.setattr(module, "preferred_encoder_plan", hardware_plan)
    clip(tmp_path / "clip.mp4", duration="0.16")
    original = module.command
    def incorrect_hardware_output(source, target, item):
        simulated = {**item, "actual_video_encoder": "libx264"}
        if item["actual_video_encoder"] == "h264_videotoolbox":
            simulated.update(output_width=360, output_height=640)
        else:
            assert not target.exists()
        return original(source, target, simulated)
    monkeypatch.setattr(module, "command", incorrect_hardware_output)
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        job = wait(manager, manager.create(manager.preview(str(tmp_path))["id"])["id"])
        assert job["status"] == "completed", job
        assert "校验失败" in job["encoding"]["fallback_reason"]
        assert job["items"][0]["actual_video_encoder"] == "libx264"
        info = inspect(Path(job["items"][0]["output_path"]))
        assert (info["width"], info["height"]) == (720, 1280)
    finally:
        manager.shutdown()


@pytest.mark.skipif(os.environ.get("SMARTSTITCH_TEST_HARDWARE") != "1", reason="requires an available VideoToolbox session")
@pytest.mark.parametrize("resolution", ["720p", "1080p"])
def test_real_videotoolbox_export(tmp_path, resolution):
    clip(tmp_path / "clip.mp4", audio=True, fps="30000/1001", duration="0.2")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        job = wait(manager, manager.create(manager.preview(str(tmp_path), resolution=resolution)["id"])["id"])
        assert job["status"] == "completed", job
        assert job["encoding"]["actual_video_encoders"] == ["h264_videotoolbox"], job["encoding"]
        assert job["encoding"]["fallback_count"] == 0
        info = inspect(Path(job["items"][0]["output_path"]))
        assert (info["width"], info["height"]) == (job["output_width"], job["output_height"])
        assert info["fps"] == "30000/1001" and info["has_audio"]
    finally:
        manager.shutdown()


def test_listing_does_not_probe_stat_or_allocate_outputs(tmp_path, monkeypatch):
    import smartstitch.landscape_to_portrait as module
    (tmp_path / "broken.mp4").write_text("not a video")
    (tmp_path / "hidden_横改竖.mp4").write_text("generated")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    def forbidden(*args, **kwargs):
        pytest.fail("listing must not inspect videos or allocate output names")
    for name in ["inspect", "probe", "fingerprint"]:
        monkeypatch.setattr(module, name, forbidden)
    monkeypatch.setattr(manager, "_target", forbidden)
    preview = manager.preview(str(tmp_path))
    assert [i["name"] for i in preview["items"]] == ["broken.mp4"]
    assert preview["pending_count"] == 1
    assert set(preview["items"][0]) == {"name", "source", "status", "output_width", "output_height"}
    assert not (tmp_path / "横改竖").exists()


def test_metadata_is_read_after_start(tmp_path):
    source = tmp_path / "clip.mp4"
    clip(source, fps="25")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        preview = manager.preview(str(tmp_path))
        # Replacing an existing name before starting uses the current video.
        clip(source, fps="30", audio=True)
        job = wait(manager, manager.create(preview["id"])["id"])
        assert job["status"] == "completed", job
        assert (job["items"][0]["fps"], job["items"][0]["has_audio"]) == ("30", True)
    finally:
        manager.shutdown()


def test_cancel_during_metadata_inspection(tmp_path, monkeypatch):
    import threading
    import smartstitch.landscape_to_portrait as module
    clip(tmp_path / "a.mp4")
    clip(tmp_path / "b.mp4")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    inspecting = threading.Event()
    release = threading.Event()
    original = module.inspect
    def slow_inspect(source):
        inspecting.set()
        assert release.wait(3)
        return original(source)
    monkeypatch.setattr(module, "inspect", slow_inspect)
    try:
        job = manager.create(manager.preview(str(tmp_path))["id"])
        assert inspecting.wait(3)
        assert manager.get(job["id"])["current_phase"] == "inspecting"
        manager.cancel(job["id"])
        release.set()
        job = wait(manager, job["id"])
        assert job["status"] == "cancelled"
        assert job["invalid_count"] == job["failed"] == 0
        assert all(i["status"] == "cancelled" and not i["encoder_attempts"] for i in job["items"])
        assert not list((tmp_path / "横改竖").iterdir())
    finally:
        release.set()
        manager.shutdown()


def test_only_unprocessable_files_finish_without_outputs(tmp_path):
    clip(tmp_path / "vertical.mp4", size="90x160")
    (tmp_path / "broken.mp4").write_text("broken")
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        preview = manager.preview(str(tmp_path))
        assert preview["pending_count"] == 2
        job = wait(manager, manager.create(preview["id"])["id"])
        assert job["status"] == "completed", job
        assert (job["total"], job["completed"], job["succeeded"], job["skipped_count"], job["invalid_count"]) == (2, 2, 0, 1, 1)
        assert all(not i["encoder_attempts"] for i in job["items"])
        assert not list((tmp_path / "横改竖").iterdir())
    finally:
        manager.shutdown()


def test_conversion_never_scans_frame_timestamps(tmp_path, monkeypatch):
    import smartstitch.landscape_to_portrait as module
    clip(tmp_path / "clip.mp4")
    original_run, original_popen = subprocess.run, subprocess.Popen
    def guard(args):
        assert not any(flag in args for flag in ["-show_frames", "-show_packets", "-count_frames"])
    def run(args, *a, **kw):
        guard(args)
        return original_run(args, *a, **kw)
    def popen(args, *a, **kw):
        guard(args)
        return original_popen(args, *a, **kw)
    monkeypatch.setattr(module.subprocess, "run", run)
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    manager = LandscapeToPortraitManager(SQLiteStore(tmp_path / "jobs.db"))
    try:
        job = wait(manager, manager.create(manager.preview(str(tmp_path))["id"])["id"])
        assert job["status"] == "completed", job
    finally:
        manager.shutdown()
