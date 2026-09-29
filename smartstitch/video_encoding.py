"""Encoder capability and arguments shared by local video tools."""
from __future__ import annotations

import platform
import re
import shutil
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any, Callable

SOFTWARE_VIDEO_ENCODER = "libx264"
VIDEOTOOLBOX_VIDEO_ENCODER = "h264_videotoolbox"
VIDEOTOOLBOX_QUALITY = 65


@lru_cache(maxsize=16)
def _cached_listing(path: str, mtime_ns: int, size: int, option: str, runner) -> tuple[int, str]:
    result = runner([path, "-hide_banner", option], capture_output=True, text=True, check=False, timeout=10)
    return result.returncode, f"{result.stdout}\n{result.stderr}"


def _capability_listing(option: str) -> tuple[int, str]:
    path = shutil.which("ffmpeg")
    if not path:
        raise FileNotFoundError("找不到 FFmpeg")
    stat = Path(path).stat()
    # Changing the binary or test runner invalidates the capability listing.
    return _cached_listing(path, stat.st_mtime_ns, stat.st_size, option, subprocess.run)


def preferred_encoder_plan(runner: Callable[..., Any] | None = None, *, software_only: bool = False) -> dict[str, Any]:
    runner = runner or subprocess.run
    plan: dict[str, Any] = {
        "policy": "videotoolbox_preferred",
        "planned_video_encoder": SOFTWARE_VIDEO_ENCODER,
        "hardware_encoder": VIDEOTOOLBOX_VIDEO_ENCODER,
        "hardware_acceleration_available": False,
        "capability_error": None,
    }
    if software_only:
        plan.update(policy="software_only", capability_error=None)
        return plan
    if sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        plan["capability_error"] = "not_apple_silicon"
    elif runner is not subprocess.run:
        plan["capability_error"] = "capability_check_unavailable"
    else:
        try:
            returncode, output = _capability_listing("-encoders")
            if returncode == 0 and re.search(rf"\b{VIDEOTOOLBOX_VIDEO_ENCODER}\b", output):
                plan.update(planned_video_encoder=VIDEOTOOLBOX_VIDEO_ENCODER,
                            hardware_acceleration_available=True)
            else:
                plan["capability_error"] = output.strip() or f"FFmpeg 未提供 {VIDEOTOOLBOX_VIDEO_ENCODER}"
        except (OSError, subprocess.SubprocessError) as exc:
            plan["capability_error"] = str(exc)
    return plan


def video_encoder_arguments(encoder: str, *, software_preset: str = "fast") -> list[str]:
    if encoder == VIDEOTOOLBOX_VIDEO_ENCODER:
        return ["-c:v", encoder, "-q:v", str(VIDEOTOOLBOX_QUALITY), "-profile:v", "high", "-pix_fmt", "yuv420p", "-allow_sw", "0"]
    return ["-c:v", SOFTWARE_VIDEO_ENCODER, "-preset", software_preset, "-crf", "18", "-pix_fmt", "yuv420p"]


def fallback_codec_attempts(encoder: str, hardware_decode: bool, error: str) -> list[tuple[str, bool]]:
    """Keep decode and encode independent; callers deduplicate and bound retries."""
    message = error.lower()
    if any(value in message for value in (
        "源文件已变化", "原视频已变更", "任务已取消", "no space left", "permission denied",
        "no such file", "invalid data found", "moov atom not found", "input/output error",
    )):
        return []
    encoder_failed = any(value in message for value in (
        "compression session", "opening encoder", "open encoder", "初始化编码",
        "invalid_hardware_encoder", "校验失败",
    ))
    if encoder == VIDEOTOOLBOX_VIDEO_ENCODER:
        if encoder_failed:
            return [(SOFTWARE_VIDEO_ENCODER, hardware_decode), (SOFTWARE_VIDEO_ENCODER, False)]
        return ([(VIDEOTOOLBOX_VIDEO_ENCODER, False)] if hardware_decode else []) + [(SOFTWARE_VIDEO_ENCODER, False)]
    return [(encoder, False)] if hardware_decode else []


def hardware_decode_supported(codec: str | None) -> bool:
    # Formats verified locally. Unknown formats remain software decoded.
    return codec in {"h264", "prores"}


def hardware_decoder_available() -> bool:
    if sys.platform != "darwin" or platform.machine().lower() not in {"arm64", "aarch64"}:
        return False
    try:
        returncode, output = _capability_listing("-hwaccels")
        return returncode == 0 and bool(re.search(r"\bvideotoolbox\b", output))
    except (OSError, subprocess.SubprocessError):
        return False
