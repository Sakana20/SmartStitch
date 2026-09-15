from __future__ import annotations

import hashlib
import json
import re
import subprocess
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .library import LibraryError, LibraryService
from .models import TimelineSliceRequest
from .timeline import TimelineAnalyzer, TimelineError


class SliceError(ValueError):
    pass


RunCommand = Callable[..., subprocess.CompletedProcess[str]]


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned or "video"


class TimelineSlicer:
    def __init__(
        self,
        timeline_analyzer: TimelineAnalyzer,
        library_service: LibraryService,
        runner: RunCommand = subprocess.run,
    ):
        self.timeline_analyzer = timeline_analyzer
        self.library_service = library_service
        self.runner = runner
        self._lock = threading.RLock()

    def export(self, request: TimelineSliceRequest) -> dict[str, Any]:
        with self._lock:
            return self._export(request)

    def _export(self, request: TimelineSliceRequest) -> dict[str, Any]:
        record = self.timeline_analyzer.load_record(request.analysis_id)
        review = record.get("review")
        if not isinstance(review, dict) or not isinstance(review.get("segments"), list):
            raise SliceError("请先保存时间线人工审核结果")
        segments = review["segments"]
        assignments = {item.segment_index: item.category for item in request.assignments}
        if len(assignments) != len(request.assignments):
            raise SliceError("同一片段不能重复指定入库类别")
        expected_indexes = {int(segment["index"]) for segment in segments}
        if set(assignments) != expected_indexes:
            raise SliceError("必须为每个审核片段指定入库类别")

        config = self.library_service.config_store.load(request.config_id)
        root = Path(config.source_root).expanduser().resolve()
        inspected = self.library_service.inspect_by_config(request.config_id)
        if not inspected.get("managed") or inspected.get("health") != "healthy":
            raise LibraryError("批量切片只能写入布局健康的受管视频库")

        manifest_key = hashlib.sha256(
            f"{request.analysis_id}\0{request.config_id}\0{request.client_request_id}".encode()
        ).hexdigest()[:16]
        manifest_directory = root / "工作记录/切片清单"
        manifest_path = manifest_directory / f"slice-{manifest_key}.json"
        if manifest_path.is_file():
            saved = json.loads(manifest_path.read_text(encoding="utf-8"))
            saved["idempotent"] = True
            return saved

        source = Path(str(record.get("source_path", ""))).expanduser().resolve()
        if not source.is_file():
            raise TimelineError("原视频不存在，无法切片")
        fps = float(record.get("fps") or 0)
        if fps <= 0:
            raise SliceError("分析记录中的帧率无效")
        stat = source.stat()
        batch = {
            "ok": True,
            "idempotent": False,
            "slice_batch_id": manifest_key,
            "analysis_id": request.analysis_id,
            "config_id": request.config_id,
            "library_id": inspected.get("library_id"),
            "created_at": datetime.now(UTC).isoformat(),
            "source": {
                "path": str(source),
                "size_bytes": stat.st_size,
                "modified_at_ns": stat.st_mtime_ns,
                "fps": fps,
            },
            "items": [],
            "success_count": 0,
            "failure_count": 0,
            "manifest_path": str(manifest_path),
        }
        source_stem = _safe_stem(source.stem)
        for segment in segments:
            index = int(segment["index"])
            category = assignments[index]
            start_frame = int(segment["start_frame"])
            end_frame = int(segment["end_frame"])
            target_directory = self.library_service.resolve_slice_target(
                request.config_id, category
            )
            output = self._unique_output(
                target_directory
                / f"{source_stem}__{index:03d}_f{start_frame}-{end_frame}.mp4"
            )
            temporary = output.with_name(f".{output.stem}.{manifest_key}.part.mp4")
            item: dict[str, Any] = {
                "segment_index": index,
                "category": category,
                "start_frame": start_frame,
                "end_frame": end_frame,
                "start_seconds": start_frame / fps,
                "end_seconds": end_frame / fps,
                "output_path": str(output),
                "status": "failed",
                "error": None,
            }
            try:
                self._render_slice(
                    source,
                    temporary,
                    start_seconds=start_frame / fps,
                    duration_seconds=(end_frame - start_frame) / fps,
                )
                self._verify_slice(temporary)
                temporary.replace(output)
                item["status"] = "succeeded"
                batch["success_count"] += 1
            except (OSError, subprocess.SubprocessError, SliceError) as exc:
                item["error"] = str(exc)
                batch["failure_count"] += 1
                temporary.unlink(missing_ok=True)
            batch["items"].append(item)

        batch["ok"] = batch["failure_count"] == 0
        _atomic_json(manifest_path, batch)
        return batch

    def _render_slice(
        self,
        source: Path,
        output: Path,
        *,
        start_seconds: float,
        duration_seconds: float,
    ) -> None:
        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-ss",
            f"{start_seconds:.9f}",
            "-t",
            f"{duration_seconds:.9f}",
            "-map",
            "0:v:0",
            "-map",
            "0:a:0?",
            "-c:v",
            "libx264",
            "-preset",
            "fast",
            "-crf",
            "18",
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ]
        result = self.runner(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise SliceError(result.stderr.strip() or "FFmpeg 切片失败")

    def _verify_slice(self, path: Path) -> None:
        result = self.runner(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SliceError(result.stderr.strip() or "ffprobe 无法校验切片")
        try:
            streams = json.loads(result.stdout).get("streams", [])
        except json.JSONDecodeError as exc:
            raise SliceError("ffprobe 返回了无效结果") from exc
        if not streams or not path.is_file() or path.stat().st_size <= 0:
            raise SliceError("切片结果中没有可用视频流")

    @staticmethod
    def _unique_output(path: Path) -> Path:
        if not path.exists():
            return path
        version = 2
        while True:
            candidate = path.with_name(f"{path.stem}-v{version}{path.suffix}")
            if not candidate.exists():
                return candidate
            version += 1
