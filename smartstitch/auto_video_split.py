"""Frame-accurate splitting at sustained black separators, with shared naming."""
from __future__ import annotations

import copy
import json
import math
import re
import shutil
import subprocess
import threading
from datetime import date
from pathlib import Path

from .models import AppConfig, Asset, AutoVideoSplitRequest, MediaProbe
from .naming import append_duplicate_suffix, derive_plan_naming, render_plan_filename
from .renderer import RenderError, _probe_rendered_duration, _run_ffmpeg_command
from .scanner import VIDEO_EXTENSIONS
from .video_encoding import (
    fallback_codec_attempts, hardware_decode_supported, hardware_decoder_available,
    preferred_encoder_plan, video_encoder_arguments,
)

BLACK_RANGE = re.compile(r"black_start:([\d.eE+-]+)\s+black_end:([\d.eE+-]+)\s+black_duration:([\d.eE+-]+)")
AUTO_FLASH_TOLERANCE_SECONDS = 0.3


def fingerprint(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]


def detection_key(request: AutoVideoSplitRequest) -> tuple:
    path = Path(request.source_file).expanduser().resolve()
    if not path.is_file() or path.suffix.lower() not in VIDEO_EXTENSIONS:
        raise ValueError("请选择一个 MP4、MOV、M4V、MKV 或 WebM 视频文件")
    return (str(path), *fingerprint(path), request.minimum_black_seconds,
            request.pixel_threshold, request.picture_threshold)


def content_ranges(gaps: list[tuple[float, float]], duration: float) -> list[dict]:
    """Complement of separator ranges; leading/trailing black makes no empty clips."""
    cursor = 0.0
    ranges = []
    for start, end in sorted(gaps):
        start, end = min(duration, max(0.0, start)), min(duration, end)
        if start > cursor + 1e-5:
            ranges.append({"start_seconds": cursor, "end_seconds": start,
                           "duration_seconds": start - cursor})
        cursor = max(cursor, end)
    if duration > cursor + 1e-5:
        ranges.append({"start_seconds": cursor, "end_seconds": duration,
                       "duration_seconds": duration - cursor})
    return ranges


def detect_video(request: AutoVideoSplitRequest) -> dict:
    key = detection_key(request)
    path = Path(key[0])
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise ValueError("找不到 FFmpeg 或 ffprobe")
    result = subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,duration,pix_fmt,sample_rate,channels",
        "-of", "json", str(path),
    ], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(result.stderr.strip() or "无法读取视频信息")
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not video:
        raise ValueError("文件没有可识别的视频画面")
    try:
        numerator, denominator = video["r_frame_rate"].split("/")
        fps = float(numerator) / float(denominator)
        duration = float(video.get("duration") or data["format"]["duration"])
    except (KeyError, ValueError, ZeroDivisionError) as exc:
        raise ValueError("无法读取有效的视频时长或帧率") from exc
    if not math.isfinite(duration) or not math.isfinite(fps) or duration <= 0 or fps <= 0:
        raise ValueError("视频时长或帧率无效")
    probe = MediaProbe(duration=duration, width=video.get("width"), height=video.get("height"),
                       fps=fps, video_codec=video.get("codec_name"), pixel_format=video.get("pix_fmt"),
                       has_audio=audio is not None, audio_codec=audio.get("codec_name") if audio else None)
    # Automatic mode measures each run rather than assuming separators last 5/10 s.
    detection_seconds = request.minimum_black_seconds if request.minimum_black_seconds is not None else 0.05
    detection = subprocess.run([
        "ffmpeg", "-hide_banner", "-nostdin", "-nostats", "-i", str(path), "-an",
        "-vf", f"scale=180:-2,blackdetect=d={detection_seconds}:pix_th={request.pixel_threshold}:pic_th={request.picture_threshold},metadata=mode=print:key=lavfi.black_end",
        "-f", "null", "-",
    ], capture_output=True, text=True, timeout=max(120, duration * 2))
    if detection.returncode:
        raise ValueError(detection.stderr[-2000:] or "黑屏检测失败")
    if detection_key(request) != key:
        raise ValueError("检测时源文件已变化，请重新预览")
    gaps = [(float(start), float(end)) for start, end, _ in BLACK_RANGE.findall(detection.stderr)]
    # EOF flushes a log at the last frame's PTS, but emits no black_end frame tag.
    # A genuine final nonblack frame emits the tag and must remain as a short clip.
    normal_ends = [float(value) for value in re.findall(r"lavfi.black_end=([\d.eE+-]+)", detection.stderr)]
    gaps = [(s, duration if duration - e <= 1 / fps + 1e-5
             and not any(abs(normal - e) < 1e-5 for normal in normal_ends) else e) for s, e in gaps]
    if gaps and not content_ranges(gaps, duration):
        raise ValueError("视频全部为黑屏，没有可导出的片段")
    detected_gaps = list(gaps)
    if request.minimum_black_seconds is None:
        # Ignore only brief flashes; separators of different lengths all remain.
        gaps = [(s, e) for s, e in gaps if e - s + 1e-5 >= AUTO_FLASH_TOLERANCE_SECONDS]
    segments = content_ranges(gaps, duration)
    if not segments:
        raise ValueError("视频全部为黑屏，没有可导出的片段")
    return {"source_file": str(path), "source_name": path.name, "fingerprint": fingerprint(path),
            "probe": probe.model_dump(mode="json"), "duration_seconds": duration,
            "black_ranges": [{"start_seconds": s, "end_seconds": min(duration, e),
                              "duration_seconds": min(duration, e) - s} for s, e in gaps],
            "detected_black_ranges": [{"start_seconds": s, "end_seconds": e,
                                       "duration_seconds": e - s} for s, e in detected_gaps],
            "black_detection": {"mode": "automatic" if request.minimum_black_seconds is None else "manual",
                                "ignored_short_count": len(detected_gaps) - len(gaps),
                                "duration_min_seconds": min((e - s for s, e in gaps), default=None),
                                "duration_max_seconds": max((e - s for s, e in gaps), default=None)},
            "segments": segments,
            "warnings": [] if gaps else ["没有检测到可用于分段的黑屏，将完整视频导出为一段。"]}


def split_config(preview: dict, output_root: Path) -> AppConfig:
    return AppConfig.model_validate({
        "schema_version": 3, "workflow_type": "generic", "id": "auto-video-split", "name": "自动分割视频",
        "source_root": str(Path(preview["source_file"]).parent), "timeline": ["pool_1"],
        "sources": {"pool_1": {"label": "源视频", "mode": "required", "directory": "."}},
        "benefit_overlays": {"mode": "disabled", "file": ""},
        "output": {"directory": str(output_root), "width": preview["probe"]["width"],
                   "height": preview["probe"]["height"], "fps": preview["probe"]["fps"],
                   "naming": preview.get("naming") or {}},
        "batch": {"minimum_free_space_gb": 0.5, "retry_count": 0},
    })


def name_segments(request: AutoVideoSplitRequest, detection: dict) -> dict:
    preview = copy.deepcopy(detection)
    preview["naming"] = request.naming.model_dump(mode="json") if request.naming else None
    config = split_config(preview, Path(preview["source_file"]).parent)
    asset = Asset(id="split-source", category="pool_1", path=preview["source_file"],
                  name=preview["source_name"], media_type="video", probe=preview["probe"])
    start = config.output.naming.sequence_start if config.output.naming.enabled else request.sequence_start
    if start + len(preview["segments"]) - 1 > 999999:
        raise ValueError("分段序号不能超过 999999")
    used_names = set()
    naming_date = date.today()
    for index, segment in enumerate(preview["segments"], 1):
        sequence = start + index - 1
        metadata = (derive_plan_naming(config, {"pool_1": asset}, sequence, naming_date)
                    if config.output.naming.enabled else None)
        base = render_plan_filename(config, metadata) if metadata else f"{sequence:02d}.mp4"
        output_name, serial = base, 1
        while output_name.casefold() in used_names:
            serial += 1
            candidate = append_duplicate_suffix(config, base, serial)
            if candidate.casefold() == output_name.casefold():
                raise ValueError("重名后缀必须包含递增序号")
            output_name = candidate
        used_names.add(output_name.casefold())
        segment.update(index=index, sequence=sequence, output_name=output_name,
                       naming=metadata.model_dump(mode="json") if metadata else None)
    preview["count"] = len(preview["segments"])
    return preview


def render_split_item(config: AppConfig, item: dict, output_path: Path, cancel_event: threading.Event,
                      on_progress=None, process_callback=None) -> dict:
    source = Path(item["selections"]["pool_1"]["path"])
    probe = item["selections"]["pool_1"]["probe"]
    if fingerprint(source) != item["source_fingerprint"]:
        raise ValueError("源文件已变化，请重新提交分割任务")
    temp_path = output_path.with_name(f".{output_path.stem}.part.mp4")
    duration = item["end_seconds"] - item["start_seconds"]
    software_only = config.output.software_codec_enabled
    plan = preferred_encoder_plan(software_only=software_only)
    initial = plan["planned_video_encoder"]
    planned = "libx264" if software_only else config.output.video_codec
    hardware_decode = not software_only and hardware_decode_supported(probe["video_codec"]) and hardware_decoder_available()
    queue, tried, attempts = [(initial, hardware_decode)], set(), []
    fallback_reason, decoder_reason = plan["capability_error"], None
    try:
        while queue:
            encoder, decode = queue.pop(0)
            if (encoder, decode) in tried:
                continue
            tried.add((encoder, decode))
            if cancel_event.is_set():
                raise RenderError("任务已取消")
            command = ["ffmpeg", "-hide_banner", "-nostdin", "-y"]
            if decode:
                command += ["-hwaccel", "videotoolbox"]
            command += ["-ss", f'{item["start_seconds"]:.9f}', "-i", str(source),
                        "-t", f"{duration:.9f}", "-map", "0:v:0", "-map", "0:a:0?",
                        *video_encoder_arguments(encoder), "-threads", "2", "-fps_mode", "passthrough",
                        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
                        "-progress", "pipe:1", "-nostats", str(temp_path)]
            attempt = {"encoder": encoder, "hardware_decode_requested": decode, "status": "running"}
            attempts.append(attempt)
            try:
                _run_ffmpeg_command(command, duration, temp_path, cancel_event, on_progress, process_callback)
                actual = _probe_rendered_duration(temp_path, duration)
            except RenderError as exc:
                attempt.update(status="cancelled" if cancel_event.is_set() else "failed", error=str(exc))
                candidates = [pair for pair in fallback_codec_attempts(encoder, decode, str(exc)) if pair not in tried]
                if cancel_event.is_set() or not candidates:
                    raise
                queue = candidates
                if candidates[0][0] != encoder:
                    fallback_reason = str(exc)
                if decode and not candidates[0][1]:
                    decoder_reason = str(exc)
                continue
            attempt["status"] = "succeeded"
            break
        else:
            raise RenderError("没有可用的编解码路径")
        if cancel_event.is_set():
            raise RenderError("任务已取消")
        if fingerprint(source) != item["source_fingerprint"]:
            raise ValueError("源文件已变化，请重新提交分割任务")
        temp_path.replace(output_path)
        return {"actual_duration": actual, "planned_video_encoder": planned,
                "actual_video_encoder": encoder, "hardware_acceleration": encoder == "h264_videotoolbox",
                "encoder_fallback_reason": fallback_reason, "decoder_fallback_reason": decoder_reason,
                "video_decode_status": "requested_unconfirmed" if decode else "software", "codec_attempts": attempts}
    finally:
        temp_path.unlink(missing_ok=True)
