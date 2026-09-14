from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from .models import LoudnessPreviewRequest


class AudioPreviewError(RuntimeError):
    pass


def create_audio_preview(
    request: LoudnessPreviewRequest,
    source_path: Path,
    cache_directory: Path,
) -> Path:
    stat = source_path.stat()
    fingerprint = "|".join(
        [
            str(source_path),
            str(stat.st_size),
            str(stat.st_mtime_ns),
            str(request.normalized),
            str(request.target_lufs),
            str(request.loudness_range_lu),
            str(request.true_peak_dbtp),
            str(request.duration_seconds),
        ]
    )
    digest = hashlib.sha1(fingerprint.encode("utf-8")).hexdigest()[:20]
    cache_directory.mkdir(parents=True, exist_ok=True)
    output_path = cache_directory / f"{digest}.m4a"
    if output_path.exists() and output_path.stat().st_size > 0:
        return output_path

    temp_path = output_path.with_suffix(".part.m4a")
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_path),
        "-t",
        str(request.duration_seconds),
        "-vn",
    ]
    if request.normalized:
        command.extend(
            [
                "-af",
                (
                    f"loudnorm=I={request.target_lufs}:LRA={request.loudness_range_lu}:"
                    f"TP={request.true_peak_dbtp},aresample=48000"
                ),
            ]
        )
    command.extend(["-c:a", "aac", "-b:a", "192k", str(temp_path)])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise AudioPreviewError(result.stderr.strip() or "FFmpeg 无法生成响度预览")
    if not temp_path.exists() or temp_path.stat().st_size == 0:
        raise AudioPreviewError("响度预览文件为空")
    temp_path.replace(output_path)
    return output_path

