from __future__ import annotations

import subprocess
import time
from pathlib import Path

import yaml

from smartstitch.config import ConfigStore
from smartstitch.jobs import JobManager
from smartstitch.models import JobCreateRequest


def generate_clip(path: Path, color: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=120x200:r=20:d=0.2",
            "-f", "lavfi", "-i", "sine=frequency=330:sample_rate=44100:duration=0.2",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
    )


def test_job_manager_writes_outputs_and_manifests(tmp_path):
    source = tmp_path / "source"
    for category, color in [
        ("hook", "red"),
        ("benefit_1", "green"),
        ("benefit_2", "white"),
        ("ending", "blue"),
    ]:
        generate_clip(source / category / "one.mp4", color)
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    config_data = {
        "id": "job-test",
        "name": "任务测试",
        "source_root": str(source),
        "timeline": ["hook", "benefit_1", "benefit_2", "ending"],
        "sources": {
            category: {"mode": "required", "directory": category, "extensions": [".mp4"]}
            for category in ["hook", "benefit_1", "benefit_2", "ending"]
        },
        "benefit_overlays": {"mode": "disabled", "directory": "overlay"},
        "output": {
            "directory": str(tmp_path / "output"),
            "width": 120,
            "height": 200,
            "fps": 20,
            "video_preset": "ultrafast",
            "crf": 30,
        },
        "batch": {"concurrency": 2, "retry_count": 0, "minimum_free_space_gb": 0},
    }
    (config_directory / "job-test.yaml").write_text(
        yaml.safe_dump(config_data, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    manager = JobManager(ConfigStore(config_directory), tmp_path / "data")
    job = manager.create(JobCreateRequest(config_id="job-test", count=2, seed=42, auto_start=True))
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = manager.get_job(job["id"])
        if job["status"] in {"completed", "partial_failed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job["status"] == "completed"
    assert job["success_count"] == 2
    output_directory = Path(job["output_directory"])
    assert len(list(output_directory.glob("*.mp4"))) == 2
    assert (output_directory / "manifest.json").exists()
    assert (output_directory / "manifest.csv").exists()
    assert (output_directory / "config.snapshot.yaml").exists()
    manifest = yaml.safe_load((output_directory / "config.snapshot.yaml").read_text("utf-8"))
    assert manifest["schema_version"] == 2
    assert manifest["timeline"] == ["hook", "benefit_1", "benefit_2", "ending"]

    csv_header = (output_directory / "manifest.csv").read_text("utf-8-sig").splitlines()[0]
    assert csv_header.split(",")[2:6] == ["hook", "benefit_1", "benefit_2", "ending"]

    manager.delete(job["id"])
    assert manager.list_jobs() == []
    assert len(list(output_directory.glob("*.mp4"))) == 2
    assert output_directory.exists()
