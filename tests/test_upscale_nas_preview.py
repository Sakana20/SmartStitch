import json
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartstitch import cluster_upscale, video_upscale


def test_probe_uses_recorded_frames_without_full_video_read(tmp_path, monkeypatch):
    source = tmp_path / "video.mp4"
    source.write_bytes(b"video")
    calls = []
    payload = {"streams": [{"codec_type": "video", "width": 720, "height": 1280,
                            "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
                            "r_frame_rate": "30/1", "nb_frames": "90"}]}

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(video_upscale.shutil, "which", lambda _: "ffprobe")
    monkeypatch.setattr(video_upscale.subprocess, "run", run)
    assert video_upscale.probe_video(source)["frames"] == 90
    assert len(calls) == 1
    assert "-count_frames" not in calls[0]


def test_probe_counts_exact_frames_if_metadata_is_missing(tmp_path, monkeypatch):
    source = tmp_path / "video.mkv"
    source.write_bytes(b"video")
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        video = {"codec_type": "video", "width": 720, "height": 1280,
                 "pix_fmt": "yuv420p", "avg_frame_rate": "30/1",
                 "r_frame_rate": "30/1", "nb_frames": "N/A"}
        if "-count_frames" in command:
            video["nb_read_frames"] = "91"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [video]}), stderr="")

    monkeypatch.setattr(video_upscale.shutil, "which", lambda _: "ffprobe")
    monkeypatch.setattr(video_upscale.subprocess, "run", run)
    assert video_upscale.probe_video(source)["frames"] == 91
    assert len(calls) == 2
    assert "-count_frames" not in calls[0] and "-count_frames" in calls[1]


def _nas_manager(tmp_path):
    import copy

    source = tmp_path / "超分" / "原素材"
    output = tmp_path / "超分" / "已处理"
    source.mkdir(parents=True)
    output.mkdir()

    class Database:
        def __init__(self):
            self.jobs = {}
            self.saved = []

        def ensure_job_table(self, _table):
            pass

        def mark_active_interrupted(self, _table):
            pass

        def save(self, _table, job):
            self.jobs[job["id"]] = copy.deepcopy(job)
            self.saved.append(copy.deepcopy(job))

        def get(self, _table, job_id):
            return copy.deepcopy(self.jobs.get(job_id))

    single = SimpleNamespace(cluster=SimpleNamespace(config_store=SimpleNamespace(directory=tmp_path)))
    manager = cluster_upscale.ClusterUpscaleBatchManager(single, Database())
    return manager, source, output


def test_nas_refresh_only_lists_files_and_model_outputs(tmp_path, monkeypatch):
    manager, source, output = _nas_manager(tmp_path)
    for name in ["a.mp4", "b.mkv", ".hidden.mp4", "notes.txt"]:
        (source / name).write_bytes(b"not even valid video")
    (source / "folder.mp4").mkdir()
    (output / "a_SR2x_原尺寸.mp4").write_bytes(b"rendered")

    def forbidden_probe(*_args):
        pytest.fail("refresh must not open media or count frames")

    monkeypatch.setattr(cluster_upscale, "probe_video", forbidden_probe)
    first = manager.preview()
    assert first["scanned_only"] is True
    assert first["pending_count"] == 1 and first["skipped_count"] == 1
    assert [item["name"] for item in first["items"]] == ["a.mp4", "b.mkv"]
    assert all("width" not in item and item["frames"] == 0 for item in first["items"])
    assert manager.preview("animevideo")["pending_count"] == 2
    (source / "b.mkv").unlink()
    assert manager.preview()["pending_count"] == 0
    external = tmp_path / "outside.mp4"
    external.write_bytes(b"video")
    (source / "escape.mp4").symlink_to(external)
    with pytest.raises(ValueError, match="指向目录外"):
        manager.preview()


def test_batch_accepts_queue_before_media_checks_and_reports_bad_file(tmp_path, monkeypatch):
    manager, source, output = _nas_manager(tmp_path)
    for index in range(5):
        (source / f"{index}.mp4").write_bytes(b"video")
    launches = []
    manager._launch = launches.append
    calls = []
    lock = threading.Lock()
    barrier = threading.Barrier(4)

    def probe(path):
        with lock:
            calls.append(path.name)
            initial = len(calls) <= 4
        if initial:
            barrier.wait(timeout=2)
        if path.name == "4.mp4":
            raise ValueError("视频损坏")
        stat = path.stat()
        return {"source": str(path), "frames": 90, "width": 720, "height": 1280,
                "fps": "30/1", "size_bytes": stat.st_size, "modified_ns": stat.st_mtime_ns}

    monkeypatch.setattr(cluster_upscale, "probe_video", probe)
    created = manager.create()
    assert calls == []
    assert launches == [created["id"]]
    assert created["status"] == "queued" and created["total_frames"] == 0
    children = []

    def create_child(source_path, output_directory, model, *, probed_info):
        assert probed_info["source"] == source_path
        children.append(source_path)
        target = Path(output_directory) / (Path(source_path).stem + "_SR2x_原尺寸.mp4")
        target.write_bytes(b"rendered")
        return {"id": str(len(children)), "status": "completed", "processed_frames": 90,
                "output_path": str(target)}

    manager.single.create = create_child
    manager.single.active_count = lambda: 0
    manager._run(created["id"], threading.Event())
    finished = manager.get(created["id"])
    assert len(calls) == 5 and len(children) == 4
    assert finished["status"] == "partial_failed"
    assert finished["total_frames"] == 360 and finished["processed_frames"] == 360
    assert finished["prechecked_files"] == 5
    assert finished["failed_files"] == 1 and finished["completed_files"] == 4
    assert "视频损坏" in finished["items"][4]["error"]
    assert any(job["phase"] == "prechecking" for job in manager.database.saved)


def test_cancel_during_background_precheck_never_dispatches(tmp_path, monkeypatch):
    manager, source, _output = _nas_manager(tmp_path)
    (source / "a.mp4").write_bytes(b"video")
    manager._launch = lambda _id: None
    created = manager.create()
    entered = threading.Event()
    release = threading.Event()
    cancel = threading.Event()

    def probe(path):
        entered.set()
        assert release.wait(timeout=2)
        stat = path.stat()
        return {"source": str(path), "frames": 90, "width": 720, "height": 1280,
                "fps": "30/1", "size_bytes": stat.st_size, "modified_ns": stat.st_mtime_ns}

    monkeypatch.setattr(cluster_upscale, "probe_video", probe)
    manager.single.create = lambda *_args, **_kwargs: pytest.fail("cancelled batch dispatched")
    thread = threading.Thread(target=manager._run, args=(created["id"], cancel))
    thread.start()
    assert entered.wait(timeout=2)
    cancel.set()
    release.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert manager.get(created["id"])["status"] == "cancelled"
