from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartstitch.api import create_app
from smartstitch.prores_alpha import ALPHA_FILTER, build_command


def test_command_matches_source_script(tmp_path: Path) -> None:
    command = build_command(tmp_path / "输入.mp4", tmp_path / "输出.mov", "ffmpeg")
    assert command[command.index("-vf") + 1] == ALPHA_FILTER
    assert command[command.index("-profile:v") + 1] == "4"
    assert command[command.index("-qscale:v") + 1] == "64"
    assert command[command.index("-pix_fmt") + 1] == "yuva444p10le"
    assert "-an" in command


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="FFmpeg required")
def test_prores_alpha_api_converts_black_to_transparent(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    source_dir = tmp_path / "源视频"
    source_dir.mkdir()
    source = source_dir / "黑底.MP4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=32x32:r=1:d=1", "-frames:v", "1", str(source)],
        check=True,
    )
    client = TestClient(create_app(tmp_path))
    missing = client.post("/api/v1/tools/prores-alpha", json={"source_directory": str(tmp_path / "missing")})
    assert missing.status_code == 422
    response = client.post("/api/v1/tools/prores-alpha", json={"source_directory": str(source_dir)})
    assert response.status_code == 200
    job_id = response.json()["id"]
    assert client.get("/api/v1/tools/prores-alpha/latest").json()["id"] == job_id
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/tools/prores-alpha/{job_id}").json()
        if job["status"] in {"completed", "partial_failed", "failed"}:
            break
        time.sleep(.05)
    assert job["status"] == "completed", job
    assert job["succeeded"] == 1
    output = source_dir / "黑底_Alpha.mov"
    assert output.is_file() and source.is_file()
    assert not (source_dir / "Alpha输出").exists()
    repeat_preview = client.post("/api/v1/tools/prores-alpha/preview", json={"source_directory": str(source_dir)})
    assert repeat_preview.json()["files"] == ["黑底.MP4"]
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_name,codec_type", "-of", "csv=p=0", str(output)],
        capture_output=True, text=True, check=True,
    )
    assert "prores,video" in probe.stdout
    assert "audio" not in probe.stdout
    decoded = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(output), "-frames:v", "1", "-pix_fmt", "rgba", "-f", "rawvideo", "-"],
        capture_output=True, check=True,
    )
    assert decoded.stdout[:4] == bytes((0, 0, 0, 0))


def test_duplicate_stems_are_rejected(tmp_path: Path) -> None:
    from smartstitch.prores_alpha import ProResAlphaManager

    (tmp_path / "clip.mp4").touch()
    (tmp_path / "clip.mov").touch()
    with pytest.raises(ValueError, match="输出文件名会冲突"):
        ProResAlphaManager().create(str(tmp_path))


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_each_video_can_use_a_different_shortcut_destination(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    source_dir = tmp_path / "sources"
    source_dir.mkdir()
    first = source_dir / "first.mp4"
    second = source_dir / "second.mov"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "color=black:s=32x32:r=1:d=1", "-frames:v", "1", str(first)],
        check=True,
    )
    shutil.copyfile(first, second)
    client = TestClient(create_app(tmp_path))
    libraries = client.get("/api/v1/global-assets/visual-effect-libraries").json()
    created = client.post("/api/v1/global-assets/visual-effect-libraries", json={"name": "第二特效库", "library_revision": libraries["revision"]})
    assert created.status_code == 200
    third = client.post("/api/v1/global-assets/visual-effect-libraries", json={"name": "第三特效库", "library_revision": created.json()["revision"]})
    assert third.status_code == 200
    assert third.json()["library"]["library_id"] == "effect_3"
    root = tmp_path / "config" / "全局素材库" / "视觉特效"
    existing = root / "effect_1" / "first_Alpha.mov"
    existing.write_bytes(b"existing asset")
    preview = client.post("/api/v1/tools/prores-alpha/preview", json={"source_directory": str(source_dir)})
    assert preview.json()["files"] == ["first.mp4", "second.mov"]
    invalid = client.post("/api/v1/tools/prores-alpha", json={"source_directory": str(source_dir), "destinations": {"first.mp4": "../other"}})
    assert invalid.status_code == 422
    assert "重新读取视频" in invalid.json()["detail"]
    invalid_path = client.post("/api/v1/tools/prores-alpha", json={"source_directory": str(source_dir), "destinations": {"first.mp4": "../other", "second.mov": "effect_3"}})
    assert invalid_path.status_code == 422
    assert "保存位置无效" in invalid_path.json()["detail"]
    missing_library = client.post("/api/v1/tools/prores-alpha", json={"source_directory": str(source_dir), "destinations": {"first.mp4": "effect_99", "second.mov": "effect_3"}})
    assert missing_library.status_code == 422
    response = client.post("/api/v1/tools/prores-alpha", json={
        "source_directory": str(source_dir),
        "destinations": {"first.mp4": "effect_1", "second.mov": "effect_3"},
    })
    assert response.status_code == 200
    job_id = response.json()["id"]
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        job = client.get(f"/api/v1/tools/prores-alpha/{job_id}").json()
        if job["status"] in {"completed", "partial_failed", "failed"}:
            break
        time.sleep(.05)
    assert job["status"] == "completed", job
    assert existing.read_bytes() == b"existing asset"
    assert (root / "effect_1" / "first_Alpha_2.mov").is_file()
    assert (root / "effect_3" / "second_Alpha.mov").is_file()
    assert not (source_dir / "Alpha输出" / "first_Alpha.mov").exists()
    refreshed = client.get("/api/v1/global-assets/visual-effect-libraries").json()
    by_id = {library["library_id"]: library for library in refreshed["libraries"]}
    assert any(asset["display_name"] == "first_Alpha_2.mov" for asset in by_id["effect_1"]["assets"])
    assert any(asset["display_name"] == "second_Alpha.mov" for asset in by_id["effect_3"]["assets"])
