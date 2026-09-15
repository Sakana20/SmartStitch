from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from smartstitch.models import AppConfig, Asset, MediaProbe, PlanItem
from smartstitch.renderer import build_ffmpeg_command, render_item
from smartstitch.scanner import probe_media


def generate_clip(path: Path, color: str, duration: float = 0.35) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", f"color=c={color}:s=180x320:r=24:d={duration}",
            "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}",
            "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path),
        ],
        check=True,
    )


def test_render_three_part_timeline(tmp_path):
    assets = {}
    for category, color in [("hook", "red"), ("benefit_1", "green"), ("ending", "blue")]:
        path = tmp_path / f"{category}.mp4"
        generate_clip(path, color)
        probe = probe_media(path)
        assets[category] = Asset(
            id=category,
            category=category,
            path=str(path),
            name=path.name,
            media_type="video",
            probe=probe,
        )
    config = AppConfig.model_validate(
        {
            "id": "render-test",
            "name": "渲染测试",
            "source_root": str(tmp_path),
            "timeline": ["hook", "benefit_1", "ending"],
            "sources": {category: {"directory": "."} for category in assets},
            "benefit_overlays": {"mode": "disabled", "directory": "overlay"},
            "output": {"directory": str(tmp_path / "out"), "width": 180, "height": 320, "fps": 24, "video_preset": "ultrafast"},
        }
    )
    estimated = sum(asset.probe.duration for asset in assets.values())
    item = PlanItem(index=1, selections=assets, output_name="result.mp4", estimated_duration=estimated)
    output = tmp_path / "result.mp4"
    result = render_item(config, item, output, threading.Event())
    assert output.exists()
    assert result["actual_duration"] > 0.8
    assert probe_media(output).fps == 24
    config.output.rate_control = "vbr"
    config.output.video_bitrate_kbps = 3000
    command, _ = build_ffmpeg_command(config, item, tmp_path / "vbr.mp4")
    assert command[command.index("-b:v") + 1] == "3000k"
    assert "-crf" not in command
    config.output.loudness.enabled = True
    config.output.loudness.target_lufs = -14
    command, _ = build_ffmpeg_command(config, item, tmp_path / "loudness.mp4")
    filter_complex = command[command.index("-filter_complex") + 1]
    assert filter_complex.count("loudnorm=I=-14:LRA=7:TP=-1.5") == 3


def test_render_with_highest_layer_overlay(tmp_path):
    assets = {}
    for category, color in [
        ("hook", "red"),
        ("benefit_1", "green"),
        ("benefit_2", "white"),
        ("ending", "blue"),
    ]:
        path = tmp_path / f"{category}.mp4"
        generate_clip(path, color)
        assets[category] = Asset(
            id=category,
            category=category,
            path=str(path),
            name=path.name,
            media_type="video",
            probe=probe_media(path),
        )
    overlay_path = tmp_path / "overlay.png"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "color=c=yellow@0.8:s=40x40", "-frames:v", "1", str(overlay_path)],
        check=True,
    )
    overlay = Asset(
        id="overlay",
        category="benefit_overlay",
        path=str(overlay_path),
        name=overlay_path.name,
        media_type="image",
        probe=probe_media(overlay_path),
    )
    config = AppConfig.model_validate(
        {
            "id": "overlay-test",
            "name": "叠图测试",
            "source_root": str(tmp_path),
            "timeline": ["hook", "benefit_1", "benefit_2", "ending"],
            "sources": {category: {"directory": "."} for category in assets},
            "benefit_overlays": {
                "mode": "required",
                "file": str(overlay_path),
                "timing": {"scope": "benefits"},
                "placement": {"x": "(W-w)/2", "y": "(H-h)/2"},
            },
            "output": {"directory": str(tmp_path / "out"), "width": 180, "height": 320, "fps": 24, "video_preset": "ultrafast"},
        }
    )
    estimated = sum(asset.probe.duration for asset in assets.values())
    item = PlanItem(index=1, selections=assets, overlay=overlay, output_name="overlay-result.mp4", estimated_duration=estimated)
    output = tmp_path / "overlay-result.mp4"
    render_item(config, item, output, threading.Event())
    assert output.exists()

    command, _ = build_ffmpeg_command(config, item, tmp_path / "benefits-overlay-result.mp4")
    filter_complex = command[command.index("-filter_complex") + 1]
    first_benefit_start = assets["hook"].probe.duration
    last_benefit_end = first_benefit_start + assets["benefit_1"].probe.duration + assets["benefit_2"].probe.duration
    assert f"enable='between(t,{first_benefit_start:.6f},{last_benefit_end:.6f})'" in filter_complex

    config.benefit_overlays.timing.scope = "full"
    command, _ = build_ffmpeg_command(config, item, tmp_path / "full-overlay-result.mp4")
    filter_complex = command[command.index("-filter_complex") + 1]
    assert "enable='gte(t,0.000000)'" in filter_complex
    assert "enable='between(t,0.000000" not in filter_complex
