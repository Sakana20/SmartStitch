from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartstitch.api import create_app
from smartstitch.config import ConfigStore
from smartstitch.folder_concat import inspect_folders
from smartstitch.jobs import JobManager
from smartstitch.models import FolderConcatRequest
from smartstitch.scanner import probe_media


def make_video(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=120x200:r=20:d=0.25",
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=0.25",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    ], check=True)


def test_preview_pairs_sorted_videos_without_recursing(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    for path in [a / "z.mp4", a / "a.MOV", a / "nested" / "ignored.mp4", b / "2.mp4"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    result = inspect_folders(FolderConcatRequest(directory_a=str(a), directory_b=str(b)))
    assert result["count_a"] == 2
    assert result["pair_count"] == 1
    assert result["unpaired_a"] == 1
    assert [(pair["a_name"], pair["b_name"]) for pair in result["pairs"]] == [("a.MOV", "2.mp4")]
    with pytest.raises(ValueError, match="不同的文件夹"):
        inspect_folders(FolderConcatRequest(directory_a=str(a), directory_b=str(a)))


def test_folder_concat_preview_api_reports_pair_count(tmp_path):
    (tmp_path / "config").mkdir()
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    (a / "one.mp4").touch()
    (b / "two.mp4").touch()
    client = TestClient(create_app(tmp_path))
    response = client.post("/api/v1/tools/folder-concat/preview", json={
        "directory_a": str(a), "directory_b": str(b),
    })
    assert response.status_code == 200
    assert response.json()["pair_count"] == 1


def test_folder_concat_renders_a_then_b_and_records_result(tmp_path):
    a, b, output = tmp_path / "A", tmp_path / "B", tmp_path / "output"
    output.mkdir()
    make_video(a / "z.mp4", "red")
    make_video(a / "a.mp4", "green")
    make_video(b / "b.mp4", "blue")
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    manager = JobManager(ConfigStore(config_directory), tmp_path / "data")
    job = manager.create_folder_concat(FolderConcatRequest(
        directory_a=str(a), directory_b=str(b), output_directory=str(output),
    ))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = manager.get_job(job["id"])
        if job["status"] in {"completed", "partial_failed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job["status"] == "completed", [item["error"] for item in job["items"]]
    assert job["job_type"] == "folder_concat"
    assert job["count"] == 1
    assert job["items"][0]["selections"]["pool_1"]["name"] == "a.mp4"
    assert job["items"][0]["selections"]["pool_2"]["name"] == "b.mp4"
    rendered = Path(job["items"][0]["output_path"])
    assert rendered.name == "a_拼接.mp4"
    assert rendered.exists()
    assert probe_media(rendered).duration > 0.4
    assert len(list(a.glob("*.mp4"))) == 2
    assert (rendered.parent / "manifest.json").exists()
    assert (rendered.parent / "manifest.csv").exists()
