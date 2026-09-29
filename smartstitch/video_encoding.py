"""Encoder capability and arguments shared by local video tools."""
from __future__ import annotations

import platform
import re
import subprocess
import sys
from typing import Any, Callable

SOFTWARE_VIDEO_ENCODER = "libx264"
VIDEOTOOLBOX_VIDEO_ENCODER = "h264_videotoolbox"
VIDEOTOOLBOX_QUALITY = 65


def preferred_encoder_plan(runner: Callable[..., Any] | None = None) -> dict[str, Any]:
    runner = runner or subprocess.run
    plan: dict[str, Any] = {
        "policy": "videotoolbox_preferred",
        "planned_video_encoder": SOFTWARE_VIDEO_ENCODER,
        "hardware_encoder": VIDEOTOOLBOX_VIDEO_ENCODER,
        "hardware_acceleration_available": False,
        "capability_error": None,
    }
    if sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        plan["capability_error"] = "not_apple_silicon"
    elif runner is not subprocess.run:
        plan["capability_error"] = "capability_check_unavailable"
    else:
        try:
            result = runner(["ffmpeg", "-hide_banner", "-encoders"], capture_output=True,
                            text=True, check=False, timeout=10)
            if result.returncode == 0 and re.search(rf"\b{VIDEOTOOLBOX_VIDEO_ENCODER}\b", f"{result.stdout}\n{result.stderr}"):
                plan.update(planned_video_encoder=VIDEOTOOLBOX_VIDEO_ENCODER,
                            hardware_acceleration_available=True)
            else:
                plan["capability_error"] = result.stderr.strip() or f"FFmpeg 未提供 {VIDEOTOOLBOX_VIDEO_ENCODER}"
        except (OSError, subprocess.SubprocessError) as exc:
            plan["capability_error"] = str(exc)
    return plan


def video_encoder_arguments(encoder: str, *, software_preset: str = "fast") -> list[str]:
    if encoder == VIDEOTOOLBOX_VIDEO_ENCODER:
        return ["-c:v", encoder, "-q:v", str(VIDEOTOOLBOX_QUALITY), "-profile:v", "high", "-pix_fmt", "yuv420p"]
    return ["-c:v", SOFTWARE_VIDEO_ENCODER, "-preset", software_preset, "-crf", "18", "-pix_fmt", "yuv420p"]
