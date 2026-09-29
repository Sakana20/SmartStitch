import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

import pytest

from smartstitch import video_upscale as upscale
from smartstitch.vfr import segment_duration, verify_vfr_video


def make_vfr(path, *, audio=True, offset=0):
    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=64x96:r=30:d=2"]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=duration=2"]
    command += ["-vf", f"select='if(lt(n,30),1,not(mod(n,2)))',setpts=PTS+{offset}/TB",
                "-fps_mode", "vfr", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac"]
    subprocess.run([*command, str(path)], check=True)


@pytest.fixture
def identity_inference(monkeypatch):
    monkeypatch.setattr(upscale, "_load_model", lambda *_: (None, "", ""))
    monkeypatch.setattr(upscale, "_upscale_frame", lambda frame, *_: frame.tobytes())
    monkeypatch.setattr(upscale, "model_status", lambda *_: {"available": True})


@pytest.mark.parametrize("model", ["x2plus", "animevideo"])
@pytest.mark.parametrize("audio,offset", [(True, 0), (False, 0), (True, .5), (False, .5)])
def test_local_vfr_preserves_timestamps_duration_and_audio_origin(tmp_path, identity_inference, model, audio, offset):
    source = tmp_path / "source.mp4"
    make_vfr(source, audio=audio, offset=offset)
    info = upscale.probe_video(source)
    assert info["frame_rate_mode"] == "vfr" and info["frame_rate_check"] == "timestamps"
    manager = upscale.VideoUpscaleManager(tmp_path)
    job = manager.create(str(source), str(tmp_path), model)
    deadline = time.monotonic() + 10
    while job["status"] in {"queued", "running"} and time.monotonic() < deadline:
        time.sleep(.02)
        job = manager.get(job["id"])
    assert job["status"] == "completed", job.get("error")
    output = Path(job["output_path"])
    verify_vfr_video(output, info, 0, info["frames"], audio=audio, final=True)
    if audio:
        def audio_start(path):
            data = json.loads(subprocess.check_output([
                "ffprobe", "-v", "error", "-select_streams", "a:0", "-show_entries",
                "stream=start_time", "-of", "json", str(path)], text=True))
            return float(data["streams"][0]["start_time"])
        assert abs(audio_start(output) - audio_start(source)) < .03
    manager.shutdown()


def test_segments_partition_true_duration(tmp_path):
    source = tmp_path / "source.mp4"
    make_vfr(source)
    info = upscale.probe_video(source)
    assert float(segment_duration(info, 0, 32) + segment_duration(info, 32, info["frames"])) == pytest.approx(info["duration"])


def test_cancel_cleans_vfr_output(tmp_path, identity_inference):
    source, output = tmp_path / "source.mp4", tmp_path / "cancelled.mp4"
    make_vfr(source)
    info = upscale.probe_video(source)
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(RuntimeError, match="取消"):
        upscale.process_segment(source, output, info, 0, info["frames"], tmp_path, cancel)
    assert not output.exists() and source.exists()


def test_vfr_verification_rejects_cfr_conversion(tmp_path):
    source, output = tmp_path / "source.mp4", tmp_path / "incorrect.mp4"
    make_vfr(source)
    info = upscale.probe_video(source)
    subprocess.run(["ffmpeg", "-v", "error", "-i", str(source), "-an", "-fps_mode", "cfr",
                    "-r", "30", "-c:v", "libx264", str(output)], check=True)
    with pytest.raises(RuntimeError, match="校验失败"):
        verify_vfr_video(output, info, 0, info["frames"])


def test_browser_responses_exclude_internal_pts_indexes():
    from smartstitch.api import _upscale_response
    raw = {"frame_rate_mode": "vfr", "info": {"timing": {"pts": [0, 10, 30]}, "frames": 3},
           "items": [{"name": "video.mp4", "frame_rate_mode": "vfr", "probe_info": {"timing": {"pts": [0, 10, 30]}}}]}
    public = _upscale_response(raw)
    assert "timing" not in public["info"] and "probe_info" not in public["items"][0]
    assert raw["info"]["timing"]["pts"] == [0, 10, 30]


def test_old_workers_cannot_accept_vfr_jobs(tmp_path):
    from types import SimpleNamespace
    from smartstitch.cluster_upscale import ClusterUpscaleManager
    from smartstitch.database import SQLiteStore
    source = tmp_path / "source.mp4"
    make_vfr(source)
    node = {"node_id": "old", "online": True, "video_upscale": {"protocol": 2, "models": {
        "x2plus": {"available": True, "model_sha256": upscale.MODEL_CONFIGS["x2plus"]["sha256"]}}}}
    cluster = SimpleNamespace(config_store=SimpleNamespace(directory=tmp_path), node_statuses=lambda: [node])
    manager = ClusterUpscaleManager(cluster, SQLiteStore(tmp_path / "db.sqlite"))
    with pytest.raises(ValueError, match="新版工作机"):
        manager.create(str(source), str(tmp_path))


def test_worker_rejects_changed_timing_in_background(tmp_path, monkeypatch, identity_inference):
    from smartstitch import cluster
    from smartstitch.api import create_app
    from smartstitch.vfr import segment_timing
    shared = tmp_path / "shared"
    source = shared / "超分" / "原素材" / "source.mp4"
    source.parent.mkdir(parents=True)
    make_vfr(source)
    info = upscale.probe_video(source)
    monkeypatch.setattr(cluster, "model_status", lambda *_: {"available": True})
    app = create_app(tmp_path, config_directory=shared)
    worker = app.state.cluster_worker
    job_id, attempt = uuid.uuid4().hex, uuid.uuid4().hex
    timing = segment_timing(info, 0, info["frames"])
    timing["pts"][1] += 10
    payload = {"kind": "video_upscale", "protocol": 2, "attempt_id": attempt, "job_id": job_id,
               "canonical_root": str(shared), "source_relative": "超分/原素材/source.mp4",
               "source_sha256": upscale._sha256(source), "stage_relative": f".video-upscale/{job_id}/{attempt}.mp4",
               "start": 0, "end": info["frames"], "frames": info["frames"], "width": info["width"],
               "height": info["height"], "fps": info["fps"], "model": "x2plus",
               "model_sha256": upscale.MODEL_CONFIGS["x2plus"]["sha256"], "timing": timing}
    try:
        assert worker.submit_upscale(payload)["status"] == "queued"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            record = worker.get(attempt)
            if record["status"] in {"failed", "succeeded"}:
                break
            time.sleep(.02)
        assert record["status"] == "failed" and "时间戳协议不匹配" in record["error"]
        assert not (shared / payload["stage_relative"]).exists()
    finally:
        worker.stop()
        app.state.instance_lock.release()
