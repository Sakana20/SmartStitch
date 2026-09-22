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


def generate_qtrle_border(path: Path, duration: float = 0.2) -> None:
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            (
                f"color=c=black@0.0:s=180x320:r=12:d={duration},format=argb,"
                "drawbox=x=0:y=0:w=iw:h=20:color=red@1:t=fill:replace=1"
            ),
            "-c:v", "qtrle", "-pix_fmt", "argb", str(path),
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
    assert result["planned_video_encoder"] == "h264_videotoolbox"
    if result["actual_video_encoder"] == "h264_videotoolbox":
        assert result["hardware_acceleration"] is True
        assert result["encoder_fallback_reason"] is None
    else:
        assert result["actual_video_encoder"] == "libx264"
        assert result["hardware_acceleration"] is False
        assert result["encoder_fallback_reason"]
    hardware_command, _ = build_ffmpeg_command(config, item, tmp_path / "hardware.mp4")
    filter_complex = hardware_command[hardware_command.index("-filter_complex") + 1]
    assert "[basev]split=2" not in filter_complex
    assert "[visual_border]" not in filter_complex
    assert hardware_command.count("-hwaccel") == 3
    assert hardware_command[hardware_command.index("-c:v") + 1] == "h264_videotoolbox"
    assert hardware_command[hardware_command.index("-q:v") + 1] == "65"
    assert "-maxrate" not in hardware_command
    assert "-crf" not in hardware_command
    config.output.rate_control = "vbr"
    config.output.video_bitrate_kbps = 3000
    command, _ = build_ffmpeg_command(
        config,
        item,
        tmp_path / "vbr.mp4",
        video_codec="libx264",
        hardware_decode=False,
    )
    assert command[command.index("-b:v") + 1] == "3000k"
    assert "-crf" not in command
    config.output.loudness.enabled = True
    config.output.loudness.target_lufs = -14
    command, _ = build_ffmpeg_command(config, item, tmp_path / "loudness.mp4")
    filter_complex = command[command.index("-filter_complex") + 1]
    assert filter_complex.count("loudnorm=I=-14:LRA=7:TP=-1.5") == 3


def test_render_falls_back_to_software_when_videotoolbox_is_unavailable(
    tmp_path, monkeypatch
):
    path = tmp_path / "source.mp4"
    generate_clip(path, "purple")
    asset = Asset(
        id="source",
        category="pool_1",
        path=str(path),
        name=path.name,
        media_type="video",
        probe=probe_media(path),
    )
    config = AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "fallback-test",
            "name": "硬件回退测试",
            "source_root": str(tmp_path),
            "timeline": ["pool_1"],
            "sources": {"pool_1": {"label": "主素材", "directory": "."}},
            "benefit_overlays": {"mode": "disabled", "directory": "overlay"},
            "output": {
                "directory": str(tmp_path / "out"),
                "width": 180,
                "height": 320,
                "fps": 24,
                "video_preset": "ultrafast",
            },
        }
    )
    item = PlanItem(
        index=1,
        selections={"pool_1": asset},
        output_name="fallback.mp4",
        estimated_duration=asset.probe.duration,
    )
    monkeypatch.setattr(
        "smartstitch.renderer._videotoolbox_capability",
        lambda: (False, "test unavailable"),
    )

    output = tmp_path / "fallback.mp4"
    result = render_item(config, item, output, threading.Event())

    assert output.exists()
    assert result["planned_video_encoder"] == "h264_videotoolbox"
    assert result["actual_video_encoder"] == "libx264"
    assert result["hardware_acceleration"] is False
    assert result["encoder_fallback_reason"] == "test unavailable"
    assert "-hwaccel" not in result["ffmpeg_command"]


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


def test_visual_dedup_renders_border_below_risk_overlay(tmp_path):
    source_path = tmp_path / "source.mp4"
    generate_clip(source_path, "blue", duration=0.5)
    source = Asset(
        id="source",
        category="pool_1",
        path=str(source_path),
        name=source_path.name,
        media_type="video",
        probe=probe_media(source_path),
    )
    border_path = tmp_path / "border.mov"
    generate_qtrle_border(border_path)
    border = Asset(
        id="border",
        category="visual_border",
        path=str(border_path),
        name=border_path.name,
        media_type="video",
        probe=probe_media(border_path),
    )
    risk_path = tmp_path / "risk.png"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=green:s=40x40",
            "-frames:v", "1", str(risk_path),
        ],
        check=True,
    )
    risk = Asset(
        id="risk",
        category="benefit_overlay",
        path=str(risk_path),
        name=risk_path.name,
        media_type="image",
        probe=probe_media(risk_path),
    )
    config = AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "visual-render",
            "name": "视觉去重渲染",
            "source_root": str(tmp_path),
            "timeline": ["pool_1"],
            "sources": {"pool_1": {"label": "主素材", "directory": "."}},
            "benefit_overlays": {
                "mode": "required",
                "file": str(risk_path),
                "timing": {"scope": "full"},
            },
            "visual_dedup": {
                "enabled": True,
                "foreground": {"scale": 0.9},
                "background": {"enabled": True, "sigma": 12, "steps": 2},
                "border_overlay": {
                    "mode": "required",
                    "file": str(border_path),
                    "scale_mode": "exact",
                },
            },
            "output": {
                "directory": str(tmp_path / "out"),
                "width": 180,
                "height": 320,
                "fps": 24,
                "video_codec": "libx264",
                "video_preset": "ultrafast",
            },
        }
    )
    item = PlanItem(
        index=1,
        selections={"pool_1": source},
        overlay=risk,
        visual_border=border,
        output_name="visual.mp4",
        estimated_duration=source.probe.duration,
    )

    command, _duration = build_ffmpeg_command(config, item, tmp_path / "command.mp4")
    filter_complex = command[command.index("-filter_complex") + 1]
    assert filter_complex.index("[basev]split=2") < filter_complex.index(
        "[dedupv][visual_border]"
    )
    assert filter_complex.index("[dedupv][visual_border]") < filter_complex.index(
        "[framedv][overlay]"
    )
    assert "-stream_loop" in command

    output = tmp_path / "visual.mp4"
    render_item(config, item, output, threading.Event())
    assert output.exists()
    assert probe_media(output).width == 180
    assert probe_media(output).height == 320

    background_only = config.model_copy(deep=True)
    background_only.visual_dedup.background.enabled = True
    background_only.visual_dedup.border_overlay.mode = "disabled"
    background_item = item.model_copy(update={"visual_border": None})
    command, _duration = build_ffmpeg_command(
        background_only, background_item, tmp_path / "background-only.mp4"
    )
    filter_complex = command[command.index("-filter_complex") + 1]
    assert "[basev]split=2" in filter_complex
    assert "gblur=" in filter_complex
    assert "[visual_border]" not in filter_complex

    neither = background_only.model_copy(deep=True)
    neither.visual_dedup.background.enabled = False
    command, _duration = build_ffmpeg_command(
        neither, background_item, tmp_path / "neither.mp4"
    )
    filter_complex = command[command.index("-filter_complex") + 1]
    assert "[basev]split=2" not in filter_complex
    assert "gblur=" not in filter_complex
    assert "[visual_border]" not in filter_complex

    master_disabled = config.model_copy(deep=True)
    master_disabled.visual_dedup.enabled = False
    command, _duration = build_ffmpeg_command(
        master_disabled, item, tmp_path / "master-disabled.mp4"
    )
    filter_complex = command[command.index("-filter_complex") + 1]
    assert "[basev]split=2" not in filter_complex
    assert "gblur=" not in filter_complex
    assert "[visual_border]" not in filter_complex

    def pixel(x, y):
        result = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-loglevel", "error", "-ss", "0.1",
                "-i", str(output), "-frames:v", "1", "-vf",
                f"crop=2:2:{x}:{y},scale=1:1",
                "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
            ],
            capture_output=True,
            check=True,
        )
        return tuple(result.stdout[:3])

    risk_pixel = pixel(5, 5)
    border_pixel = pixel(100, 5)
    assert risk_pixel[1] > risk_pixel[0]
    assert border_pixel[0] > border_pixel[1]


def test_visual_border_renders_without_blurred_background(tmp_path):
    source_path = tmp_path / "source.mp4"
    generate_clip(source_path, "blue", duration=0.5)
    source = Asset(
        id="source",
        category="pool_1",
        path=str(source_path),
        name=source_path.name,
        media_type="video",
        probe=probe_media(source_path),
    )
    border_path = tmp_path / "border.mov"
    generate_qtrle_border(border_path)
    border = Asset(
        id="border",
        category="visual_border",
        path=str(border_path),
        name=border_path.name,
        media_type="video",
        probe=probe_media(border_path),
    )
    config = AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "border-only-render",
            "name": "仅透明边框",
            "source_root": str(tmp_path),
            "timeline": ["pool_1"],
            "sources": {"pool_1": {"label": "主素材", "directory": "."}},
            "benefit_overlays": {"mode": "disabled", "timing": {"scope": "full"}},
            "visual_dedup": {
                "enabled": True,
                "background": {"enabled": False},
                "border_overlay": {
                    "mode": "required",
                    "file": str(border_path),
                    "scale_mode": "exact",
                },
            },
            "output": {
                "directory": str(tmp_path / "out"),
                "width": 180,
                "height": 320,
                "fps": 24,
                "video_codec": "libx264",
                "video_preset": "ultrafast",
            },
        }
    )
    item = PlanItem(
        index=1,
        selections={"pool_1": source},
        overlay=None,
        visual_border=border,
        output_name="border-only.mp4",
        estimated_duration=source.probe.duration,
    )

    command, _duration = build_ffmpeg_command(config, item, tmp_path / "border-only.mp4")
    filter_complex = command[command.index("-filter_complex") + 1]

    assert "[basev][visual_border]overlay=" in filter_complex
    assert "[basev]split=2" not in filter_complex
    assert "gblur=" not in filter_complex
    assert "[dedupv]" not in filter_complex

    output = tmp_path / "border-only.mp4"
    render_item(config, item, output, threading.Event())
    assert output.exists()
    assert probe_media(output).width == 180
    assert probe_media(output).height == 320
