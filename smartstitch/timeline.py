from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import struct
import subprocess
import sys
from array import array
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
SILENCE_EVENT_RE = re.compile(
    r"silence_(start|end):\s*([0-9]+(?:\.[0-9]+)?)"
)
SILENCE_NOISE_DB = -35.0
ADAPTIVE_PAUSE_WINDOW_SECONDS = 0.05
ADAPTIVE_PAUSE_PERCENTILE = 0.30
ADAPTIVE_PAUSE_MARGIN_DB = 2.0
ADAPTIVE_PAUSE_MAX_THRESHOLD_DB = -12.0
SPEECH_TAIL_PADDING_SECONDS = 0.12
NEXT_SPEECH_GUARD_SECONDS = 0.08
BREAKPOINT_MERGE_WINDOW_SECONDS = 0.18
WAVEFORM_SAMPLE_RATE = 8000
WAVEFORM_BUCKETS_PER_SECOND = 400
WAVEFORM_BYTES_PER_BUCKET = 4


class TimelineError(ValueError):
    pass


def _source_video_sort_key(path: Path) -> tuple[tuple[int, object], ...]:
    return tuple(
        (1, int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"([0-9]+)", path.name)
    )


def list_source_videos(source_directory: str) -> dict[str, Any]:
    directory = Path(source_directory).expanduser().resolve()
    if not directory.exists():
        raise TimelineError("源视频文件夹不存在")
    if not directory.is_dir():
        raise TimelineError("源视频路径必须是文件夹")

    videos = sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file()
            and not path.name.startswith(".")
            and path.suffix.lower() in VIDEO_EXTENSIONS
        ),
        key=_source_video_sort_key,
    )
    return {
        "source_directory": str(directory),
        "count": len(videos),
        "videos": [{"name": path.name, "path": str(path)} for path in videos],
    }


def _rate(value: str | None) -> float:
    if not value or value == "0/0":
        return 0.0
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return 0.0


def _probe(path: Path) -> dict[str, Any]:
    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        (
            "format=duration:stream=codec_type,width,height,avg_frame_rate,"
            "r_frame_rate,nb_frames,sample_rate,channels"
        ),
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise TimelineError(result.stderr.strip() or "ffprobe 无法读取视频")
    data = json.loads(result.stdout)
    video = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "video"),
        None,
    )
    if video is None:
        raise TimelineError("文件中没有视频流")
    duration = float(data.get("format", {}).get("duration") or 0)
    fps = _rate(video.get("avg_frame_rate")) or _rate(video.get("r_frame_rate"))
    if duration <= 0 or fps <= 0:
        raise TimelineError("无法确定视频时长或帧率")
    frame_count = int(video.get("nb_frames") or round(duration * fps))
    audio = next(
        (stream for stream in data.get("streams", []) if stream.get("codec_type") == "audio"),
        None,
    )
    return {
        "duration": duration,
        "fps": fps,
        "frame_count": max(frame_count, 1),
        "width": video.get("width"),
        "height": video.get("height"),
        "has_audio": audio is not None,
        "audio_channels": int(audio.get("channels") or 0) if audio else 0,
        "audio_sample_rate": int(audio.get("sample_rate") or 0) if audio else 0,
    }


def _scene_times(path: Path, threshold: float) -> list[float]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        str(path),
        "-an",
        "-vf",
        f"select='gt(scene,{threshold})',showinfo",
        "-vsync",
        "vfr",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise TimelineError(result.stderr.strip() or "FFmpeg 场景分析失败")
    return [float(match.group(1)) for match in PTS_TIME_RE.finditer(result.stderr)]


def _parse_silence_intervals(
    output: str, *, media_duration: float | None = None
) -> list[dict[str, Any]]:
    intervals: list[dict[str, Any]] = []
    start: float | None = None
    for match in SILENCE_EVENT_RE.finditer(output):
        event, raw_seconds = match.groups()
        seconds = float(raw_seconds)
        if event == "start":
            start = seconds
            continue
        if start is None or seconds < start:
            continue
        intervals.append(
            {
                "start_seconds": start,
                "end_seconds": seconds,
                "duration_seconds": seconds - start,
                "complete": True,
            }
        )
        start = None
    if start is not None and media_duration is not None and media_duration >= start:
        intervals.append(
            {
                "start_seconds": start,
                "end_seconds": media_duration,
                "duration_seconds": media_duration - start,
                "complete": False,
            }
        )
    return intervals


def _silence_intervals(
    path: Path, minimum_duration: float, *, media_duration: float
) -> list[dict[str, Any]]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        str(path),
        "-vn",
        "-af",
        f"silencedetect=noise={SILENCE_NOISE_DB:g}dB:d={minimum_duration}",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    # No audio stream is valid for timeline review; it simply contributes no candidates.
    if result.returncode != 0:
        if "matches no streams" in result.stderr:
            return []
        raise TimelineError(result.stderr.strip() or "FFmpeg 语音停顿分析失败")
    return _parse_silence_intervals(result.stderr, media_duration=media_duration)


def _adaptive_intervals_from_levels(
    levels_db: list[float],
    minimum_duration: float,
    *,
    media_duration: float,
    window_seconds: float = ADAPTIVE_PAUSE_WINDOW_SECONDS,
) -> tuple[list[dict[str, Any]], float]:
    if not levels_db:
        return [], SILENCE_NOISE_DB
    ordered = sorted(levels_db)
    percentile_index = round((len(ordered) - 1) * ADAPTIVE_PAUSE_PERCENTILE)
    threshold_db = min(
        ADAPTIVE_PAUSE_MAX_THRESHOLD_DB,
        max(SILENCE_NOISE_DB, ordered[percentile_index] + ADAPTIVE_PAUSE_MARGIN_DB),
    )
    quiet = [level <= threshold_db for level in levels_db]
    # Ignore a single 50 ms spike inside a low-energy pause.
    for index in range(1, len(quiet) - 1):
        if not quiet[index] and quiet[index - 1] and quiet[index + 1]:
            quiet[index] = True

    intervals: list[dict[str, Any]] = []
    start_index: int | None = None
    for index, is_quiet in enumerate([*quiet, False]):
        if is_quiet and start_index is None:
            start_index = index
            continue
        if is_quiet or start_index is None:
            continue
        start = start_index * window_seconds
        end = min(index * window_seconds, media_duration)
        if end - start + 1e-9 >= minimum_duration:
            intervals.append(
                {
                    "start_seconds": start,
                    "end_seconds": end,
                    "duration_seconds": end - start,
                    "complete": end < media_duration - window_seconds,
                    "detector": "adaptive_rms",
                    "threshold_db": threshold_db,
                }
            )
        start_index = None
    return intervals, threshold_db


def _adaptive_pause_intervals(
    path: Path, minimum_duration: float, *, media_duration: float
) -> tuple[list[dict[str, Any]], float]:
    samples_per_window = round(WAVEFORM_SAMPLE_RATE * ADAPTIVE_PAUSE_WINDOW_SECONDS)
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(WAVEFORM_SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise TimelineError("无法启动自适应语音停顿分析")
    levels_db: list[float] = []
    samples = array("h")
    pending_byte = b""
    try:
        while True:
            chunk = process.stdout.read(65536)
            if not chunk:
                break
            chunk = pending_byte + chunk
            even_length = len(chunk) - len(chunk) % 2
            pending_byte = chunk[even_length:]
            decoded = array("h")
            decoded.frombytes(chunk[:even_length])
            if sys.byteorder != "little":
                decoded.byteswap()
            samples.extend(decoded)
            while len(samples) >= samples_per_window:
                window = samples[:samples_per_window]
                del samples[:samples_per_window]
                mean_square = sum(sample * sample for sample in window) / len(window)
                rms = math.sqrt(mean_square) / 32768.0
                levels_db.append(20 * math.log10(max(rms, 1 / 32768)))
        error_output = process.stderr.read().decode("utf-8", errors="replace")
        return_code = process.wait()
        if return_code != 0 or not levels_db:
            raise TimelineError(error_output.strip() or "FFmpeg 无法分析音频能量")
    except Exception:
        process.kill()
        process.wait()
        raise
    return _adaptive_intervals_from_levels(
        levels_db,
        minimum_duration,
        media_duration=media_duration,
    )


def speech_pause_candidates(
    intervals: list[dict[str, Any]],
    *,
    fps: float,
    frame_count: int,
    tail_padding_seconds: float = SPEECH_TAIL_PADDING_SECONDS,
    next_speech_guard_seconds: float = NEXT_SPEECH_GUARD_SECONDS,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    leading_silence_limit = 1 / fps
    for interval in intervals:
        start = float(interval["start_seconds"])
        end = float(interval["end_seconds"])
        if not interval.get("complete", True) or start <= leading_silence_limit:
            continue
        first_silence_frame = math.ceil(start * fps)
        latest_safe_frame = math.floor((end - next_speech_guard_seconds) * fps)
        if latest_safe_frame < first_silence_frame:
            continue
        preferred_frame = round((start + tail_padding_seconds) * fps)
        frame = max(first_silence_frame, min(preferred_frame, latest_safe_frame))
        if frame <= 1 or frame >= frame_count - 1:
            continue
        candidates.append(
            {
                "frame_index": frame,
                "time_seconds": round(frame / fps, 6),
                "reason": "speech_pause",
                "evidence": {
                    "silence_start_seconds": round(start, 6),
                    "silence_end_seconds": round(end, 6),
                    "silence_duration_seconds": round(end - start, 6),
                    "speech_tail_padding_seconds": tail_padding_seconds,
                    "next_speech_guard_seconds": next_speech_guard_seconds,
                    "detector": interval.get("detector", "silencedetect"),
                    "noise_threshold_db": round(
                        float(interval.get("threshold_db", SILENCE_NOISE_DB)), 3
                    ),
                },
            }
        )
    return candidates


def merge_breakpoints(
    scene_times: list[float],
    speech_pauses: list[dict[str, Any] | float],
    *,
    fps: float,
    frame_count: int,
    merge_window_seconds: float = BREAKPOINT_MERGE_WINDOW_SECONDS,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = [
        {
            "frame_index": max(1, min(frame_count - 1, round(value * fps))),
            "reason": "scene_change",
        }
        for value in scene_times
    ]
    for pause in speech_pauses:
        if isinstance(pause, (int, float)):
            candidates.append(
                {
                    "frame_index": max(
                        1, min(frame_count - 1, round(float(pause) * fps))
                    ),
                    "reason": "speech_pause",
                }
            )
        else:
            candidates.append(pause)
    candidates.sort(key=lambda item: int(item["frame_index"]))
    merge_window = max(1, round(merge_window_seconds * fps))
    groups: list[list[dict[str, Any]]] = []
    for candidate in candidates:
        if (
            not groups
            or int(candidate["frame_index"])
            - int(groups[-1][-1]["frame_index"])
            > merge_window
        ):
            groups.append([candidate])
        else:
            groups[-1].append(candidate)

    merged: list[dict[str, Any]] = []
    for group in groups:
        reasons = sorted({str(item["reason"]) for item in group})
        # Prefer the visual cut frame when both signals agree; it is more stable for editing.
        visual_frames = [
            int(item["frame_index"])
            for item in group
            if item["reason"] == "scene_change"
        ]
        frame = visual_frames[0] if visual_frames else round(
            sum(int(item["frame_index"]) for item in group) / len(group)
        )
        if frame <= 1 or frame >= frame_count - 1:
            continue
        confidence = 0.9 if len(reasons) > 1 else (0.72 if "scene_change" in reasons else 0.55)
        point: dict[str, Any] = {
            "frame_index": frame,
            "time_seconds": round(frame / fps, 6),
            "reasons": reasons,
            "confidence": confidence,
            "review_status": "machine_suggested",
        }
        pause_evidence = [
            item["evidence"]
            for item in group
            if item.get("reason") == "speech_pause" and item.get("evidence")
        ]
        if pause_evidence:
            point["evidence"] = {"speech_pauses": pause_evidence}
        merged.append(point)
    return merged


def _generate_waveform(path: Path, target: Path) -> dict[str, Any]:
    samples_per_bucket = WAVEFORM_SAMPLE_RATE // WAVEFORM_BUCKETS_PER_SECOND
    command = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-nostdin",
        "-i",
        str(path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        str(WAVEFORM_SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "-",
    ]
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if process.stdout is None or process.stderr is None:
        process.kill()
        raise TimelineError("无法启动音频波形解码")

    bucket: list[int] = []
    bucket_count = 0
    pending_byte = b""
    try:
        with temporary.open("wb") as output:
            while True:
                chunk = process.stdout.read(65536)
                if not chunk:
                    break
                chunk = pending_byte + chunk
                even_length = len(chunk) - len(chunk) % 2
                pending_byte = chunk[even_length:]
                samples = array("h")
                samples.frombytes(chunk[:even_length])
                if sys.byteorder != "little":
                    samples.byteswap()
                encoded = bytearray()
                for sample in samples:
                    bucket.append(sample)
                    if len(bucket) < samples_per_bucket:
                        continue
                    encoded.extend(struct.pack("<hh", min(bucket), max(bucket)))
                    bucket.clear()
                    bucket_count += 1
                if encoded:
                    output.write(encoded)
            if bucket:
                output.write(struct.pack("<hh", min(bucket), max(bucket)))
                bucket_count += 1
        error_output = process.stderr.read().decode("utf-8", errors="replace")
        return_code = process.wait()
        if return_code != 0 or bucket_count == 0:
            raise TimelineError(error_output.strip() or "FFmpeg 无法生成音频波形")
        temporary.replace(target)
    except Exception:
        process.kill()
        process.wait()
        temporary.unlink(missing_ok=True)
        raise
    return {
        "waveform_sample_rate": WAVEFORM_SAMPLE_RATE,
        "buckets_per_second": WAVEFORM_BUCKETS_PER_SECOND,
        "bucket_count": bucket_count,
    }


def _aggregate_waveform(
    values: list[tuple[int, int]], output_count: int
) -> list[list[float]]:
    if not values or output_count <= 0:
        return []
    output_count = min(output_count, len(values))
    result: list[list[float]] = []
    for index in range(output_count):
        start = math.floor(index * len(values) / output_count)
        end = max(start + 1, math.ceil((index + 1) * len(values) / output_count))
        group = values[start:end]
        minimum = min(item[0] for item in group) / 32768.0
        maximum = max(item[1] for item in group) / 32768.0
        result.append([round(minimum, 5), round(maximum, 5)])
    return result


class TimelineAnalyzer:
    def __init__(self, data_directory: Path):
        self.data_directory = data_directory
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self.waveform_directory = self.data_directory / "waveforms"
        self.waveform_directory.mkdir(parents=True, exist_ok=True)
        self._media_tokens: dict[str, Path] = {}

    def _analysis_id(self, path: Path, scene_threshold: float, silence_duration: float) -> str:
        stat = path.stat()
        payload = (
            f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}\0{scene_threshold}\0"
            f"{silence_duration}\0{SILENCE_NOISE_DB}\0{ADAPTIVE_PAUSE_WINDOW_SECONDS}\0"
            f"{ADAPTIVE_PAUSE_PERCENTILE}\0{ADAPTIVE_PAUSE_MARGIN_DB}\0"
            f"{ADAPTIVE_PAUSE_MAX_THRESHOLD_DB}\0{SPEECH_TAIL_PADDING_SECONDS}\0"
            f"{NEXT_SPEECH_GUARD_SECONDS}\0{datetime.now(UTC).isoformat()}\0"
            f"{secrets.token_hex(8)}"
        ).encode()
        return hashlib.sha256(payload).hexdigest()[:24]

    def analyze(
        self, source_path: str, scene_threshold: float, silence_duration_seconds: float
    ) -> dict[str, Any]:
        path = Path(source_path).expanduser().resolve()
        if not path.exists() or not path.is_file():
            raise TimelineError("视频文件不存在")
        if path.suffix.lower() not in VIDEO_EXTENSIONS:
            raise TimelineError(f"不支持的视频格式: {path.suffix or '无扩展名'}")
        metadata = _probe(path)
        source_stat = path.stat()
        scene_times = _scene_times(path, scene_threshold)
        if metadata["has_audio"]:
            try:
                silence_intervals = _silence_intervals(
                    path,
                    silence_duration_seconds,
                    media_duration=float(metadata["duration"]),
                )
                pause_method = "silencedetect"
                adaptive_threshold_db = None
                if not silence_intervals:
                    silence_intervals, adaptive_threshold_db = _adaptive_pause_intervals(
                        path,
                        silence_duration_seconds,
                        media_duration=float(metadata["duration"]),
                    )
                    pause_method = "adaptive_rms"
                pause_analysis = {
                    "speech_pause_status": "ready",
                    "speech_pause_method": pause_method,
                }
                if adaptive_threshold_db is not None:
                    pause_analysis["adaptive_pause_threshold_db"] = round(
                        adaptive_threshold_db, 3
                    )
            except TimelineError as exc:
                silence_intervals = []
                pause_analysis = {
                    "speech_pause_status": "failed",
                    "speech_pause_error": str(exc),
                }
        else:
            silence_intervals = []
            pause_analysis = {"speech_pause_status": "unavailable"}
        pause_candidates = speech_pause_candidates(
            silence_intervals,
            fps=float(metadata["fps"]),
            frame_count=int(metadata["frame_count"]),
        )
        breakpoints = merge_breakpoints(
            scene_times,
            pause_candidates,
            fps=metadata["fps"],
            frame_count=metadata["frame_count"],
        )
        # Every run is an immutable audit record; re-analysis never replaces a human review.
        analysis_id = self._analysis_id(path, scene_threshold, silence_duration_seconds)
        waveform_target = self.waveform_directory / f"{analysis_id}.wfm"
        if metadata["has_audio"]:
            try:
                waveform_metadata = _generate_waveform(path, waveform_target)
                audio = {
                    "has_audio": True,
                    "channels": metadata["audio_channels"],
                    "sample_rate": metadata["audio_sample_rate"],
                    "waveform_status": "ready",
                    "waveform_url": f"/api/v1/timeline/waveforms/{analysis_id}",
                    **pause_analysis,
                    **waveform_metadata,
                }
            except (OSError, TimelineError, ValueError, struct.error) as exc:
                audio = {
                    "has_audio": True,
                    "channels": metadata["audio_channels"],
                    "sample_rate": metadata["audio_sample_rate"],
                    "waveform_status": "failed",
                    "waveform_error": str(exc),
                    **pause_analysis,
                }
        else:
            audio = {
                "has_audio": False,
                "channels": 0,
                "sample_rate": 0,
                "waveform_status": "unavailable",
                **pause_analysis,
            }
        media_token = secrets.token_urlsafe(24)
        self._media_tokens[media_token] = path
        result = {
            "schema_version": 2,
            "analysis_id": analysis_id,
            "source_path": str(path),
            "source_name": path.name,
            "source_fingerprint": {
                "size_bytes": source_stat.st_size,
                "modified_at_ns": source_stat.st_mtime_ns,
            },
            "created_at": datetime.now(UTC).isoformat(),
            **metadata,
            "settings": {
                "scene_threshold": scene_threshold,
                "silence_duration_seconds": silence_duration_seconds,
                "silence_noise_db": SILENCE_NOISE_DB,
                "adaptive_pause_window_seconds": ADAPTIVE_PAUSE_WINDOW_SECONDS,
                "adaptive_pause_percentile": ADAPTIVE_PAUSE_PERCENTILE,
                "adaptive_pause_margin_db": ADAPTIVE_PAUSE_MARGIN_DB,
                "adaptive_pause_max_threshold_db": ADAPTIVE_PAUSE_MAX_THRESHOLD_DB,
                "speech_tail_padding_seconds": SPEECH_TAIL_PADDING_SECONDS,
                "next_speech_guard_seconds": NEXT_SPEECH_GUARD_SECONDS,
            },
            "silence_intervals": silence_intervals,
            "breakpoints": breakpoints,
            "audio": audio,
            "media_url": f"/api/v1/timeline/media/{media_token}",
        }
        self._write(analysis_id, result)
        return result

    def waveform(
        self, analysis_id: str, start_frame: int, end_frame: int, width_px: int
    ) -> dict[str, Any]:
        data = self.load_record(analysis_id)
        frame_count = int(data["frame_count"])
        fps = float(data["fps"])
        normalized_start = max(0, min(frame_count, start_frame))
        normalized_end = max(normalized_start, min(frame_count, end_frame))
        if normalized_end <= normalized_start:
            raise TimelineError("波形区间必须包含至少一帧")
        audio = data.get("audio") or {}
        if audio.get("waveform_status") != "ready":
            raise TimelineError("该分析没有可用的音频波形")
        buckets_per_second = int(
            audio.get("buckets_per_second") or WAVEFORM_BUCKETS_PER_SECOND
        )
        total_buckets = int(audio.get("bucket_count") or 0)
        first_bucket = max(
            0, min(total_buckets, math.floor(normalized_start / fps * buckets_per_second))
        )
        last_bucket = max(
            first_bucket,
            min(total_buckets, math.ceil(normalized_end / fps * buckets_per_second)),
        )
        target = self.waveform_directory / f"{analysis_id}.wfm"
        if not target.is_file():
            raise TimelineError("音频波形缓存不存在，请重新分析")
        with target.open("rb") as source:
            source.seek(first_bucket * WAVEFORM_BYTES_PER_BUCKET)
            raw = source.read((last_bucket - first_bucket) * WAVEFORM_BYTES_PER_BUCKET)
        values = list(struct.iter_unpack("<hh", raw))
        peaks = _aggregate_waveform(values, min(width_px, len(values)))
        return {
            "analysis_id": analysis_id,
            "start_frame": normalized_start,
            "end_frame": normalized_end,
            "bucket_count": len(peaks),
            "peaks": peaks,
            "complete": True,
        }

    def media_path(self, token: str) -> Path:
        try:
            return self._media_tokens[token]
        except KeyError as exc:
            raise TimelineError("预览链接无效或服务已重启，请重新分析") from exc

    def save_decision(self, analysis_id: str, frame_indexes: list[int]) -> dict[str, Any]:
        path = self.data_directory / f"{analysis_id}.json"
        if not path.exists():
            raise TimelineError("分析记录不存在，请重新分析")
        data = json.loads(path.read_text(encoding="utf-8"))
        frame_count = int(data["frame_count"])
        fps = float(data["fps"])
        normalized = sorted(set(frame_indexes))
        if any(frame <= 0 or frame >= frame_count for frame in normalized):
            raise TimelineError("断点必须位于视频首尾帧之间")
        revision_payload = json.dumps(
            {
                "analysis_id": analysis_id,
                "source_fingerprint": data.get("source_fingerprint"),
                "frame_indexes": normalized,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        data["review"] = {
            "review_revision": hashlib.sha256(revision_payload).hexdigest(),
            "saved_at": datetime.now(UTC).isoformat(),
            "breakpoints": [
                {
                    "frame_index": frame,
                    "time_seconds": round(frame / fps, 6),
                    "review_status": "human_confirmed",
                }
                for frame in normalized
            ],
            "segments": [
                {
                    "index": index + 1,
                    "segment_id": f"f{start:09d}-f{end:09d}",
                    "start_frame": start,
                    "end_frame": end,
                    "start_seconds": round(start / fps, 6),
                    "end_seconds": round(end / fps, 6),
                }
                for index, (start, end) in enumerate(
                    zip([0, *normalized], [*normalized, frame_count], strict=True)
                )
            ],
        }
        self._write(analysis_id, data)
        return {"ok": True, "analysis_id": analysis_id, **data["review"]}

    def load_record(self, analysis_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"[a-f0-9]{24}", analysis_id):
            raise TimelineError("分析记录 ID 格式不正确")
        path = self.data_directory / f"{analysis_id}.json"
        if not path.is_file():
            raise TimelineError("分析记录不存在，请重新分析")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise TimelineError("分析记录格式不正确")
        return data

    def _write(self, analysis_id: str, data: dict[str, Any]) -> None:
        target = self.data_directory / f"{analysis_id}.json"
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(target)
