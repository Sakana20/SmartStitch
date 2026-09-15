from __future__ import annotations

import hashlib
import json
import re
import secrets
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


VIDEO_EXTENSIONS = {".mp4", ".mov", ".mkv", ".m4v", ".webm"}
PTS_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
SILENCE_END_RE = re.compile(r"silence_end:\s*([0-9]+(?:\.[0-9]+)?)")


class TimelineError(ValueError):
    pass


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
        "format=duration:stream=codec_type,width,height,avg_frame_rate,r_frame_rate,nb_frames",
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
    return {
        "duration": duration,
        "fps": fps,
        "frame_count": max(frame_count, 1),
        "width": video.get("width"),
        "height": video.get("height"),
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


def _silence_end_times(path: Path, minimum_duration: float) -> list[float]:
    command = [
        "ffmpeg",
        "-hide_banner",
        "-nostdin",
        "-i",
        str(path),
        "-vn",
        "-af",
        f"silencedetect=noise=-35dB:d={minimum_duration}",
        "-f",
        "null",
        "-",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    # No audio stream is valid for timeline review; it simply contributes no candidates.
    if result.returncode != 0 and "matches no streams" not in result.stderr:
        return []
    return [float(match.group(1)) for match in SILENCE_END_RE.finditer(result.stderr)]


def merge_breakpoints(
    scene_times: list[float],
    silence_times: list[float],
    *,
    fps: float,
    frame_count: int,
    merge_window_seconds: float = 0.18,
) -> list[dict[str, Any]]:
    candidates = [
        (max(1, min(frame_count - 1, round(value * fps))), "scene_change")
        for value in scene_times
    ] + [
        (max(1, min(frame_count - 1, round(value * fps))), "silence_end")
        for value in silence_times
    ]
    candidates.sort()
    merge_window = max(1, round(merge_window_seconds * fps))
    groups: list[list[tuple[int, str]]] = []
    for candidate in candidates:
        if not groups or candidate[0] - groups[-1][-1][0] > merge_window:
            groups.append([candidate])
        else:
            groups[-1].append(candidate)

    merged: list[dict[str, Any]] = []
    for group in groups:
        reasons = sorted({reason for _, reason in group})
        # Prefer the visual cut frame when both signals agree; it is more stable for editing.
        visual_frames = [frame for frame, reason in group if reason == "scene_change"]
        frame = visual_frames[0] if visual_frames else round(sum(item[0] for item in group) / len(group))
        if frame <= 1 or frame >= frame_count - 1:
            continue
        confidence = 0.9 if len(reasons) > 1 else (0.72 if "scene_change" in reasons else 0.55)
        merged.append(
            {
                "frame_index": frame,
                "time_seconds": round(frame / fps, 6),
                "reasons": reasons,
                "confidence": confidence,
                "review_status": "machine_suggested",
            }
        )
    return merged


class TimelineAnalyzer:
    def __init__(self, data_directory: Path):
        self.data_directory = data_directory
        self.data_directory.mkdir(parents=True, exist_ok=True)
        self._media_tokens: dict[str, Path] = {}

    def _analysis_id(self, path: Path, scene_threshold: float, silence_duration: float) -> str:
        stat = path.stat()
        payload = (
            f"{path}\0{stat.st_size}\0{stat.st_mtime_ns}\0{scene_threshold}\0"
            f"{silence_duration}\0{datetime.now(UTC).isoformat()}\0{secrets.token_hex(8)}"
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
        silence_times = _silence_end_times(path, silence_duration_seconds)
        breakpoints = merge_breakpoints(
            scene_times,
            silence_times,
            fps=metadata["fps"],
            frame_count=metadata["frame_count"],
        )
        # Every run is an immutable audit record; re-analysis never replaces a human review.
        analysis_id = self._analysis_id(path, scene_threshold, silence_duration_seconds)
        media_token = secrets.token_urlsafe(24)
        self._media_tokens[media_token] = path
        result = {
            "schema_version": 1,
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
            },
            "breakpoints": breakpoints,
            "media_url": f"/api/v1/timeline/media/{media_token}",
        }
        self._write(analysis_id, result)
        return result

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
