from __future__ import annotations

import subprocess
from pathlib import Path

from smartstitch.audio_preview import create_audio_preview
from smartstitch.models import AppConfig, LoudnessPreviewRequest
from smartstitch.scanner import probe_config_audio


def generate_clip(path: Path, duration: float = 0.5) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c=red:s=180x320:r=24:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
    )


def test_audio_preview_is_generated_and_cached(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    generate_clip(source)
    request = LoudnessPreviewRequest(
        config_id="preview-test",
        asset_path=str(source),
        normalized=True,
        target_lufs=-14,
        duration_seconds=3,
    )

    first = create_audio_preview(request, source, tmp_path / "cache")
    second = create_audio_preview(request, source, tmp_path / "cache")

    assert first == second
    assert first.suffix == ".m4a"
    assert first.stat().st_size > 0


def test_probe_config_audio_only_accepts_configured_video(tmp_path: Path) -> None:
    source_directory = tmp_path / "source"
    source_directory.mkdir()
    source = source_directory / "source.mp4"
    generate_clip(source)
    config = AppConfig.model_validate(
        {
            "id": "preview-test",
            "name": "试听测试",
            "source_root": str(tmp_path),
            "timeline": ["hook", "benefit_video", "ending"],
            "sources": {
                category: {"directory": str(source_directory)}
                for category in ["hook", "benefit_video", "ending"]
            },
            "benefit_overlays": {"mode": "disabled"},
            "output": {"directory": str(tmp_path / "output")},
        }
    )

    assert probe_config_audio(config, source).has_audio is True
    outside = tmp_path / "outside.mp4"
    generate_clip(outside)
    try:
        probe_config_audio(config, outside)
    except ValueError as exc:
        assert "不在当前配置" in str(exc)
    else:
        raise AssertionError("配置目录外的文件不应允许试听")
