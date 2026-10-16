from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

import pytest
import yaml

from conftest import authenticated_client
from smartstitch.api import create_app
from smartstitch.auto_video_split import content_ranges, detect_video, name_segments, render_split_item, split_config
from smartstitch.config import ConfigStore
from smartstitch.codec_settings import CodecSettings
from smartstitch.jobs import JobManager
from smartstitch.models import AutoVideoSplitRequest, OutputNamingConfig
from smartstitch.renderer import RenderError


def make_source(path: Path, *, audio=True, color="red", black_lengths=(10, 10)):
    command = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    # A short dark shot inside clip 1 must survive; only the 10 second runs split.
    for value, duration in [(color, .4), ("black", .2), ("green", .4), ("black", black_lengths[0]), ("blue", .4), ("black", black_lengths[1]), ("yellow", .4)]:
        command += ["-f", "lavfi", "-i", f"color={value}:s=96x128:r=30:d={duration}"]
    if audio:
        command += ["-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=48000:duration={sum(black_lengths) + 1.8}"]
    command += ["-filter_complex_threads", "1", "-filter_complex", "[0:v][1:v][2:v][3:v][4:v][5:v][6:v]concat=n=7:v=1:a=0[v]", "-map", "[v]"]
    if audio:
        command += ["-map", "7:a", "-c:a", "aac"]
    command += ["-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(command, check=True, capture_output=True)


def naming(start=21, *, sequence=True):
    blocks = [
        {"id": "source", "type": "source", "category": "pool_1", "variants": [{"sample_name": "商品|!|混剪.mp4", "field_count": 2, "field_index": 0}]},
        {"id": "text", "type": "text", "text": "-"},
    ]
    if sequence:
        blocks.append({"id": "seq", "type": "sequence"})
    return OutputNamingConfig(enabled=True, sequence_start=start, builder={"enabled": True, "blocks": blocks})


def manager(tmp_path):
    config = tmp_path / "config"
    config.mkdir(exist_ok=True)
    instance = JobManager(ConfigStore(config), tmp_path / "data")
    instance.codec_settings.save(CodecSettings(software_codec_enabled=True))
    return instance


def wait_job(instance, job):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        result = instance.get_job(job["id"])
        if result["status"] in {"completed", "partial_failed", "failed", "cancelled"}:
            return result
        time.sleep(.02)
    instance.cancel(job["id"])
    pytest.fail("分割任务未及时结束")


def test_ranges_remove_leading_trailing_and_overlapping_black():
    assert content_ranges([(0, 10), (11, 21), (20, 25)], 25) == [
        {"start_seconds": 10, "end_seconds": 11, "duration_seconds": 1}
    ]
    assert content_ranges([(0, 10)], 10) == []
    assert content_ranges([], 1)[0]["duration_seconds"] == 1


@pytest.mark.parametrize("audio,builder", [(True, False), (True, True), (False, False)])
def test_real_black_detection_and_frame_accurate_job(tmp_path, audio, builder):
    source = tmp_path / "商品|!|混剪.mp4"
    make_source(source, audio=audio)
    original = source.read_bytes()
    instance = manager(tmp_path)
    request = AutoVideoSplitRequest(source_file=str(source), sequence_start=21, naming=naming() if builder else None)
    preview = instance.preview_auto_video_split(request)
    assert preview["count"] == 3
    assert len(preview["black_ranges"]) == 2
    assert preview["black_detection"]["mode"] == "automatic"
    assert preview["black_detection"]["ignored_short_count"] == 1
    assert [s["duration_seconds"] for s in preview["segments"]] == pytest.approx([1, .4, .4], abs=1e-5)
    expected = [f"商品-{i}.mp4" if builder else f"{i}.mp4" for i in range(21, 24)]
    assert [s["output_name"] for s in preview["segments"]] == expected
    job = wait_job(instance, instance.create_auto_video_split(request))
    assert job["status"] == "completed", job["items"]
    assert job["job_type"] == "auto_video_split"
    assert [s["output_name"] for s in job["items"]] == expected
    assert source.read_bytes() == original
    batch = Path(job["output_directory"])
    assert (batch / "manifest.json").exists() and (batch / "manifest.csv").exists()
    snapshot = yaml.safe_load(Path(job["config_snapshot_path"]).read_text())
    assert snapshot["auto_video_split"]["analysis"]["count"] == 3
    assert "start_seconds" in (batch / "manifest.csv").read_text("utf-8-sig")
    for index, item in enumerate(job["items"]):
        output = batch / item["output_name"]
        data = json.loads(subprocess.check_output(["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]))
        video = next(s for s in data["streams"] if s["codec_type"] == "video")
        assert (video["width"], video["height"], video["r_frame_rate"]) == (96, 128, "30/1")
        assert int(video["nb_frames"]) == (30 if index == 0 else 12)
        assert any(s["codec_type"] == "audio" for s in data["streams"]) is audio
        # Full decode checks the actual first and last frames, including cuts off keyframes.
        raw = subprocess.check_output(["ffmpeg", "-v", "error", "-i", str(output), "-vf", "scale=1:1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"])
        assert max(raw[:3]) > 100 and max(raw[-3:]) > 100
    instance.shutdown()


def fake_detection(source):
    from smartstitch.auto_video_split import fingerprint
    return {"source_file": str(source), "source_name": source.name, "fingerprint": fingerprint(source),
            "probe": {"duration": 3, "width": 96, "height": 128, "fps": 30, "has_audio": False, "video_codec": "h264"},
            "duration_seconds": 3, "black_ranges": [], "warnings": [],
            "segments": [{"start_seconds": i, "end_seconds": i + 1, "duration_seconds": 1} for i in range(3)]}


def test_cache_reuses_detection_for_naming_but_invalidates_changed_input(tmp_path, monkeypatch):
    source = tmp_path / "商品|!|混剪.mp4"
    source.write_bytes(b"original")
    calls = []
    monkeypatch.setattr("smartstitch.jobs.detect_video", lambda request: calls.append(request) or fake_detection(source))
    instance = manager(tmp_path)
    request = AutoVideoSplitRequest(source_file=str(source))
    assert instance.preview_auto_video_split(request)["segments"][0]["output_name"] == "01.mp4"
    request.naming = naming()
    assert instance.preview_auto_video_split(request)["segments"][0]["output_name"] == "商品-21.mp4"
    assert len(calls) == 1
    source.write_bytes(b"changed-source")
    instance.preview_auto_video_split(request)
    assert len(calls) == 2
    request.minimum_black_seconds = 9
    instance.preview_auto_video_split(request)
    assert len(calls) == 3


def test_duplicate_names_and_invalid_source_block(tmp_path):
    source = tmp_path / "商品|!|混剪.mp4"
    source.write_bytes(b"x")
    request = AutoVideoSplitRequest(source_file=str(source), naming=naming(sequence=False))
    preview = name_segments(request, fake_detection(source))
    assert [s["output_name"] for s in preview["segments"]] == ["商品-.mp4", "商品--02.mp4", "商品--03.mp4"]
    request.naming.builder.blocks[0].category = "pool_2"
    with pytest.raises(ValueError, match="素材库"):
        name_segments(request, fake_detection(source))
    request = AutoVideoSplitRequest(source_file=str(source), sequence_start=999999)
    with pytest.raises(ValueError, match="999999"):
        name_segments(request, fake_detection(source))


def test_changed_source_rejected_before_encoder(tmp_path):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    preview = fake_detection(source)
    config = split_config(preview, tmp_path)
    item = {"source_fingerprint": preview["fingerprint"], "selections": {"pool_1": {"path": str(source), "probe": preview["probe"]}}}
    source.write_bytes(b"changed")
    with pytest.raises(ValueError, match="源文件已变化"):
        render_split_item(config, item, tmp_path / "01.mp4", threading.Event())


def test_cancel_queued_segments_and_remove_partial_output(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"original")
    monkeypatch.setattr("smartstitch.jobs.detect_video", lambda request: fake_detection(source))
    entered = threading.Event()
    def fake_run(command, duration, temp_path, cancel_event, on_progress, process_callback):
        temp_path.write_bytes(b"partial")
        entered.set()
        assert cancel_event.wait(5)
        raise RenderError("任务已取消")
    monkeypatch.setattr("smartstitch.auto_video_split._run_ffmpeg_command", fake_run)
    instance = manager(tmp_path)
    job = instance.create_auto_video_split(AutoVideoSplitRequest(source_file=str(source)))
    assert entered.wait(5)
    instance.cancel(job["id"])
    job = wait_job(instance, job)
    assert job["status"] == "cancelled"
    assert all(s["status"] == "cancelled" for s in job["items"])
    assert not list(Path(job["output_directory"]).glob("*.mp4"))
    assert not list(Path(job["output_directory"]).glob(".*.part.mp4"))


def test_api_preview_submit_and_validation(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"x")
    monkeypatch.setattr("smartstitch.jobs.detect_video", lambda request: fake_detection(source))
    app = create_app(tmp_path)
    monkeypatch.setattr(app.state.job_manager, "start", lambda job_id: app.state.job_manager.get_job(job_id))
    client = authenticated_client(app)
    payload = {"source_file": str(source), "sequence_start": 21}
    response = client.post("/api/v1/tools/auto-video-split/preview", json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["segments"][0]["output_name"] == "21.mp4"
    response = client.post("/api/v1/tools/auto-video-split", json=payload)
    assert response.status_code == 200, response.text
    job = response.json()
    assert client.get(f'/api/v1/jobs/{job["id"]}').status_code == 200
    assert any(j["id"] == job["id"] for j in client.get("/api/v1/jobs").json())
    for invalid in [{"minimum_black_seconds": 0}, {"sequence_start": 0}, {"source_file": "/missing.mp4"}, {"naming": {"enabled": True}}]:
        assert client.post("/api/v1/tools/auto-video-split/preview", json={**payload, **invalid}).status_code == 422


def test_naming_validation_does_not_probe_or_detect_video(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    client = authenticated_client(app)
    def forbidden(*args, **kwargs):
        pytest.fail("文件名验证不应调用媒体探测或黑屏检测")
    monkeypatch.setattr(subprocess, "run", forbidden)
    monkeypatch.setattr("smartstitch.jobs.detect_video", forbidden)
    monkeypatch.setattr("smartstitch.auto_video_split.detect_video", forbidden)
    monkeypatch.setattr(app.state.job_manager, "preview_auto_video_split", forbidden)
    # No video file is needed: only its name is used by this endpoint.
    source = tmp_path / "商品|!|混剪.mp4"
    assert not source.exists()
    payload = {"source_file": str(source), "naming": naming().model_dump(mode="json")}
    response = client.post("/api/v1/tools/auto-video-split/naming-preview", json=payload)
    assert response.status_code == 200, response.text
    data = response.json()
    assert data["output_name"] == "商品-21.mp4"
    assert data["source_name"] == source.name
    assert [block["value"] for block in data["block_values"]] == ["商品", "-", "21"]
    assert "segments" not in data and "black_ranges" not in data
    actual = name_segments(AutoVideoSplitRequest.model_validate(payload), {
        "source_file": str(source), "source_name": source.name,
        "probe": {"width": 96, "height": 128, "fps": 30, "duration": 1},
        "segments": [{"start_seconds": 0, "end_seconds": 1, "duration_seconds": 1}],
    })
    assert data["output_name"] == actual["segments"][0]["output_name"]
    payload["naming"]["builder"]["blocks"][0]["category"] = "pool_2"
    assert client.post("/api/v1/tools/auto-video-split/naming-preview", json=payload).status_code == 422
    assert client.post("/api/v1/tools/auto-video-split/naming-preview", json={"source_file": str(source), "sequence_start": 21}).json()["output_name"] == "21.mp4"
    assert client.post("/api/v1/tools/auto-video-split/naming-preview", json={"source_file": ""}).status_code == 422


def test_no_separator_and_all_black_video(tmp_path):
    for color in ["red", "black"]:
        source = tmp_path / f"{color}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"color={color}:s=96x128:r=30:d=1", "-c:v", "libx264", "-threads", "1", str(source)], check=True)
        request = AutoVideoSplitRequest(source_file=str(source), minimum_black_seconds=.5)
        if color == "red":
            detected = detect_video(request)
            assert len(detected["segments"]) == 1 and detected["warnings"]
        else:
            with pytest.raises(ValueError, match="全部为黑屏"):
                detect_video(request)


def test_single_nonblack_frame_at_eof_survives(tmp_path):
    source = tmp_path / "single-last-frame.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=96x128:r=30:d=1",
        "-f", "lavfi", "-i", "color=yellow:s=96x128:r=30:d=0.033333333",
        "-filter_complex_threads", "1", "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-c:v", "libx264", "-threads", "1", str(source),
    ], check=True)
    detected = detect_video(AutoVideoSplitRequest(source_file=str(source), minimum_black_seconds=.5))
    assert len(detected["segments"]) == 1
    assert detected["segments"][0]["start_seconds"] == pytest.approx(1)
    assert detected["segments"][0]["duration_seconds"] == pytest.approx(1 / 30, abs=1e-5)


@pytest.mark.parametrize("black_lengths", [(1, 3), (.5, 2), (10, 7)])
def test_automatic_detection_measures_mixed_separator_lengths(tmp_path, black_lengths):
    source = tmp_path / "mixed.mp4"
    make_source(source, black_lengths=black_lengths)
    request = AutoVideoSplitRequest(source_file=str(source))
    assert request.minimum_black_seconds is None
    detected = detect_video(request)
    assert [gap["duration_seconds"] for gap in detected["black_ranges"]] == pytest.approx(black_lengths, abs=1e-5)
    assert [segment["duration_seconds"] for segment in detected["segments"]] == pytest.approx([1, .4, .4], abs=1e-5)
    assert detected["black_detection"]["ignored_short_count"] == 1
    assert detected["black_detection"]["duration_min_seconds"] == pytest.approx(min(black_lengths), abs=1e-5)
    assert detected["black_detection"]["duration_max_seconds"] == pytest.approx(max(black_lengths), abs=1e-5)


def test_manual_duration_api_remains_available(tmp_path):
    source = tmp_path / "manual.mp4"
    make_source(source, black_lengths=(1, 3))
    detected = detect_video(AutoVideoSplitRequest(source_file=str(source), minimum_black_seconds=2))
    assert len(detected["black_ranges"]) == 1
    assert detected["black_ranges"][0]["duration_seconds"] == pytest.approx(3)
    assert detected["black_detection"]["mode"] == "manual"


def test_black_separators_with_logo_and_vertical_compliance_text(tmp_path):
    clean = tmp_path / "clean.mp4"
    source = tmp_path / "logo-and-compliance.mp4"
    make_source(clean, black_lengths=(1, 3))
    # Roughly 3% of the frame remains orange/white throughout the separator.
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-i", str(clean), "-vf",
        "drawbox=x=2:y=3:w=24:h=5:color=orange:t=fill,drawbox=x=2:y=30:w=4:h=70:color=white:t=fill",
        "-c:v", "libx264", "-threads", "1", "-c:a", "copy", str(source),
    ], check=True)
    strict = detect_video(AutoVideoSplitRequest(source_file=str(source), picture_threshold=.98))
    assert not strict["black_ranges"]
    assert len(strict["segments"]) == 1
    detected = detect_video(AutoVideoSplitRequest(source_file=str(source)))
    assert [gap["duration_seconds"] for gap in detected["black_ranges"]] == pytest.approx([1, 3], abs=1e-5)
    assert len(detected["segments"]) == 3
    assert detected["black_detection"]["ignored_short_count"] == 1


def test_dark_scene_with_visible_subject_is_not_a_separator(tmp_path):
    source = tmp_path / "dark-scene.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=96x128:r=30:d=1",
        "-vf", "drawbox=x=30:y=20:w=36:h=88:color=gray:t=fill", "-c:v", "libx264", "-threads", "1", str(source),
    ], check=True)
    detected = detect_video(AutoVideoSplitRequest(source_file=str(source)))
    assert not detected["black_ranges"]
    assert len(detected["segments"]) == 1
