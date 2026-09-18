from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from .library import LibraryError, LibraryService
from .models import TimelineSliceRequest
from .timeline import TimelineAnalyzer, TimelineError


class SliceError(ValueError):
    pass


class SliceConflictError(SliceError):
    pass


class SliceCancelled(SliceError):
    pass


class SliceProcessError(SliceError):
    def __init__(self, message: str, return_code: int):
        super().__init__(message)
        self.return_code = return_code


RunCommand = Callable[..., subprocess.CompletedProcess[str]]
BACKGROUND_SLICE_NICE = 5
SOFTWARE_VIDEO_ENCODER = "libx264"
VIDEOTOOLBOX_VIDEO_ENCODER = "h264_videotoolbox"
VIDEOTOOLBOX_QUALITY = 65


def _lower_background_process_priority(process: subprocess.Popen[str]) -> None:
    """Best-effort priority reduction that must never break slice encoding."""
    if os.name == "posix" and hasattr(os, "setpriority"):
        try:
            os.setpriority(os.PRIO_PROCESS, process.pid, BACKGROUND_SLICE_NICE)
        except OSError:
            pass


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    temporary.replace(path)


def _safe_stem(value: str) -> str:
    cleaned = re.sub(r"[^\w.-]+", "_", value, flags=re.UNICODE).strip("._")
    return cleaned or "video"


def slice_request_key(request: TimelineSliceRequest) -> str:
    return hashlib.sha256(
        (
            f"{request.analysis_id}\0{request.config_id}\0"
            f"{request.review_revision}\0{request.client_request_id}"
        ).encode()
    ).hexdigest()[:16]


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
        self._prepare_lock = threading.RLock()
        self._execution_lock = threading.RLock()
        self._encoder_capability_lock = threading.Lock()
        self._background_encoder_plan_cache: dict[str, Any] | None = None

    def _background_encoder_plan(self) -> dict[str, Any]:
        with self._encoder_capability_lock:
            if self._background_encoder_plan_cache is not None:
                return dict(self._background_encoder_plan_cache)

            plan: dict[str, Any] = {
                "policy": "videotoolbox_preferred",
                "planned_video_encoder": SOFTWARE_VIDEO_ENCODER,
                "hardware_encoder": VIDEOTOOLBOX_VIDEO_ENCODER,
                "hardware_acceleration_available": False,
                "capability_error": None,
            }
            machine = platform.machine().lower()
            if sys.platform != "darwin" or machine not in {"arm64", "aarch64"}:
                plan["capability_error"] = "not_apple_silicon"
            elif self.runner is not subprocess.run:
                plan["capability_error"] = "capability_check_unavailable"
            else:
                try:
                    result = self.runner(
                        ["ffmpeg", "-hide_banner", "-encoders"],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )
                except (OSError, subprocess.SubprocessError) as exc:
                    plan["capability_error"] = str(exc)
                else:
                    output = f"{result.stdout}\n{result.stderr}"
                    if result.returncode == 0 and re.search(
                        rf"\b{VIDEOTOOLBOX_VIDEO_ENCODER}\b", output
                    ):
                        plan["planned_video_encoder"] = VIDEOTOOLBOX_VIDEO_ENCODER
                        plan["hardware_acceleration_available"] = True
                    else:
                        plan["capability_error"] = (
                            result.stderr.strip()
                            or f"FFmpeg 未提供 {VIDEOTOOLBOX_VIDEO_ENCODER}"
                        )
            self._background_encoder_plan_cache = dict(plan)
            return plan

    @staticmethod
    def _software_encoder_plan() -> dict[str, Any]:
        return {
            "policy": "software_only",
            "planned_video_encoder": SOFTWARE_VIDEO_ENCODER,
            "hardware_encoder": VIDEOTOOLBOX_VIDEO_ENCODER,
            "hardware_acceleration_available": False,
            "capability_error": None,
        }

    def export(self, request: TimelineSliceRequest) -> dict[str, Any]:
        with self._execution_lock:
            with self._prepare_lock:
                batch = self._prepare(request, initial_status="running")
            if batch.get("idempotent"):
                return batch
            return self._execute(batch)

    def prepare(
        self,
        request: TimelineSliceRequest,
        *,
        reserved_paths: set[str] | None = None,
        job_id: str | None = None,
        write_manifest: bool = True,
        prefer_hardware: bool = False,
    ) -> dict[str, Any]:
        with self._prepare_lock:
            return self._prepare(
                request,
                initial_status="queued",
                reserved_paths=reserved_paths,
                job_id=job_id,
                write_manifest=write_manifest,
                prefer_hardware=prefer_hardware,
            )

    def _prepare(
        self,
        request: TimelineSliceRequest,
        *,
        initial_status: str,
        reserved_paths: set[str] | None = None,
        job_id: str | None = None,
        write_manifest: bool = True,
        prefer_hardware: bool = False,
    ) -> dict[str, Any]:
        record = self.timeline_analyzer.load_record(request.analysis_id)
        review = record.get("review")
        if not isinstance(review, dict) or not isinstance(review.get("segments"), list):
            raise SliceError("请先保存时间线人工审核结果")
        if review.get("review_revision") != request.review_revision:
            raise SliceConflictError("断点审核已变更，请重新确认待切片清单")
        segments = review["segments"]
        segments_by_index = {int(segment["index"]): segment for segment in segments}
        if len(segments_by_index) != len(segments):
            raise SliceError("审核记录包含重复片段序号")

        requested_indexes = {
            index
            for assignment in request.assignments
            for index in assignment.segment_indexes
        }
        unknown_indexes = requested_indexes - set(segments_by_index)
        if unknown_indexes:
            raise SliceError("待切片清单包含不存在的片段")

        seen_indexes: set[int] = set()
        normalized_assignments: list[dict[str, Any]] = []
        for unit_index, assignment in enumerate(request.assignments, start=1):
            duplicated = seen_indexes.intersection(assignment.segment_indexes)
            if duplicated:
                raise SliceError("同一片段不能同时属于多个输出单元")
            seen_indexes.update(assignment.segment_indexes)
            ordered_segments = sorted(
                (segments_by_index[index] for index in assignment.segment_indexes),
                key=lambda segment: int(segment["start_frame"]),
            )
            parts = []
            for segment in ordered_segments:
                index = int(segment["index"])
                start_frame = int(segment["start_frame"])
                end_frame = int(segment["end_frame"])
                if end_frame <= start_frame:
                    raise SliceError(f"片段 {index} 的帧区间无效")
                if parts and start_frame < int(parts[-1]["end_frame"]):
                    raise SliceError("组合片段的成员区间不能重叠")
                parts.append(
                    {
                        "segment_index": index,
                        "segment_id": str(
                            segment.get("segment_id")
                            or f"f{start_frame:09d}-f{end_frame:09d}"
                        ),
                        "start_frame": start_frame,
                        "end_frame": end_frame,
                        "start_seconds": start_frame / float(record.get("fps") or 1),
                        "end_seconds": end_frame / float(record.get("fps") or 1),
                    }
                )
            normalized_assignments.append(
                {
                    "unit_index": unit_index,
                    "client_unit_id": assignment.client_unit_id,
                    "category": assignment.category,
                    "parts": parts,
                }
            )

        config = self.library_service.config_store.load(request.config_id)
        if (
            self.library_service.config_store.content_hash(request.config_id)
            != request.current_config_hash
        ):
            raise SliceConflictError("配置已变更，请刷新分类后重试")
        root = Path(config.source_root).expanduser().resolve()
        inspected = self.library_service.inspect_by_config(request.config_id)
        if not inspected.get("managed") or inspected.get("health") != "healthy":
            raise LibraryError("批量切片只能写入布局健康的受管视频库")

        manifest_key = slice_request_key(request)
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
        fingerprint = record.get("source_fingerprint")
        if isinstance(fingerprint, dict) and (
            fingerprint.get("size_bytes") != stat.st_size
            or fingerprint.get("modified_at_ns") != stat.st_mtime_ns
        ):
            raise SliceConflictError("原视频已变更，请重新分析")

        target_directories: dict[str, Path] = {}
        for category in {item["category"] for item in normalized_assignments}:
            if category == "skip":
                continue
            target_directories[category] = self.library_service.resolve_slice_target(
                request.config_id, category
            )

        created_at = datetime.now(UTC).isoformat()
        resolved_job_id = job_id or uuid.uuid4().hex
        encoding_plan = (
            self._background_encoder_plan()
            if prefer_hardware
            else self._software_encoder_plan()
        )
        batch = {
            "ok": True,
            "idempotent": False,
            "id": resolved_job_id,
            "short_id": resolved_job_id[:8],
            "job_type": "timeline_slice",
            "request_key": manifest_key,
            "client_request_id": request.client_request_id,
            "status": initial_status,
            "progress": 0.0,
            "slice_batch_id": manifest_key,
            "analysis_id": request.analysis_id,
            "review_revision": request.review_revision,
            "config_id": request.config_id,
            "config_hash": request.current_config_hash,
            "library_id": inspected.get("library_id"),
            "created_at": created_at,
            "started_at": created_at if initial_status == "running" else None,
            "finished_at": None,
            "source": {
                "path": str(source),
                "name": source.name,
                "size_bytes": stat.st_size,
                "modified_at_ns": stat.st_mtime_ns,
                "fps": fps,
            },
            "items": [],
            "output_unit_count": len(normalized_assignments),
            "source_segment_count": sum(
                len(item["parts"]) for item in normalized_assignments
            ),
            "success_count": 0,
            "failure_count": 0,
            "cancelled_count": 0,
            "skipped_count": 0,
            "manifest_path": str(manifest_path),
            "manifest_sync_error": None,
            "encoding": {
                **encoding_plan,
                "actual_video_encoders": [],
                "hardware_acceleration_used": False,
                "fallback_count": 0,
                "fallback_reason": None,
            },
        }
        source_stem = _safe_stem(source.stem)
        for assignment in normalized_assignments:
            unit_index = int(assignment["unit_index"])
            category = str(assignment["category"])
            parts = assignment["parts"]
            segment_indexes = [int(part["segment_index"]) for part in parts]
            segment_ids = [str(part["segment_id"]) for part in parts]
            total_duration_seconds = sum(
                (int(part["end_frame"]) - int(part["start_frame"])) / fps
                for part in parts
            )
            excluded_gap_frames = sum(
                max(0, int(current["start_frame"]) - int(previous["end_frame"]))
                for previous, current in zip(parts, parts[1:])
            )
            for part in parts:
                part["duration_seconds"] = (
                    int(part["end_frame"]) - int(part["start_frame"])
                ) / fps
            single = len(parts) == 1
            item: dict[str, Any] = {
                "unit_index": unit_index,
                "client_unit_id": assignment["client_unit_id"],
                "composite": not single,
                "category": category,
                "segment_indexes": segment_indexes,
                "segment_ids": segment_ids,
                "parts": parts,
                "total_duration_seconds": total_duration_seconds,
                "excluded_gap_frames": excluded_gap_frames,
                # Preserve the original manifest fields for single-segment consumers.
                "segment_index": segment_indexes[0] if single else None,
                "segment_id": segment_ids[0] if single else None,
                "start_frame": parts[0]["start_frame"] if single else None,
                "end_frame": parts[0]["end_frame"] if single else None,
                "start_seconds": parts[0]["start_seconds"] if single else None,
                "end_seconds": parts[0]["end_seconds"] if single else None,
                "target_directory": None,
                "output_path": None,
                "status": "pending",
                "phase": "waiting",
                "progress": 0.0,
                "started_at": None,
                "finished_at": None,
                "attempts": 0,
                "temporary_path": None,
                "ffmpeg_exit_code": None,
                "ffprobe_result": None,
                "planned_video_encoder": encoding_plan["planned_video_encoder"],
                "actual_video_encoder": None,
                "hardware_acceleration_used": False,
                "encoder_fallback_reason": None,
                "encoder_attempts": [],
                "error": None,
            }
            if category == "skip":
                item["status"] = "skipped"
                item["phase"] = "done"
                item["progress"] = 1.0
                item["finished_at"] = created_at
                batch["items"].append(item)
                batch["skipped_count"] += 1
                continue
            target_directory = target_directories[category]
            category_stem = _safe_stem(category.replace("_", "-"))
            if single:
                part = parts[0]
                filename = (
                    f"{source_stem}__{segment_indexes[0]:03d}_{category_stem}"
                    f"_f{part['start_frame']}-{part['end_frame']}.mp4"
                )
            else:
                filename = (
                    f"{source_stem}__g{unit_index:03d}_{category_stem}"
                    f"_p{segment_indexes[0]:03d}-{segment_indexes[-1]:03d}.mp4"
                )
            output = self._unique_output(
                target_directory / filename, reserved_paths or set()
            )
            item["target_directory"] = str(target_directory)
            item["output_path"] = str(output)
            batch["items"].append(item)

        if write_manifest:
            _atomic_json(manifest_path, batch)
        return batch

    def execute(
        self,
        batch: dict[str, Any],
        *,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
        low_priority: bool = False,
    ) -> dict[str, Any]:
        with self._execution_lock:
            return self._execute(
                batch,
                on_update=on_update,
                cancel_event=cancel_event,
                process_callback=process_callback,
                low_priority=low_priority,
            )

    def _execute(
        self,
        batch: dict[str, Any],
        *,
        on_update: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
        low_priority: bool = False,
    ) -> dict[str, Any]:
        source = Path(str(batch["source"]["path"]))
        fps = float(batch["source"]["fps"])
        manifest_path = Path(str(batch["manifest_path"]))
        manifest_key = str(batch["request_key"])
        batch["status"] = "running"
        batch["started_at"] = batch.get("started_at") or datetime.now(UTC).isoformat()
        batch.setdefault("encoding", self._software_encoder_plan())
        self._publish(batch, manifest_path, on_update)
        source_has_audio: bool | None = None
        hardware_disabled_reason: str | None = None
        for item in batch["items"]:
            if item["status"] in {"skipped", "succeeded"}:
                continue
            if cancel_event is not None and cancel_event.is_set():
                item["status"] = "cancelled"
                item["phase"] = "done"
                item["progress"] = 1.0
                item["finished_at"] = datetime.now(UTC).isoformat()
                batch["cancelled_count"] += 1
                continue
            temporary = Path(item["output_path"]).with_name(
                f".{Path(item['output_path']).stem}.{manifest_key}.part.mp4"
            )
            item["status"] = "running"
            item["phase"] = "encoding"
            item["started_at"] = datetime.now(UTC).isoformat()
            item["attempts"] = int(item.get("attempts") or 0) + 1
            item["temporary_path"] = str(temporary)
            item.setdefault("planned_video_encoder", SOFTWARE_VIDEO_ENCODER)
            item.setdefault("actual_video_encoder", None)
            item.setdefault("hardware_acceleration_used", False)
            item.setdefault("encoder_fallback_reason", None)
            item.setdefault("encoder_attempts", [])
            self._publish(batch, manifest_path, on_update)

            last_persisted_progress = float(item.get("progress") or 0)
            last_persisted_at = 0.0

            def update_progress(progress: float) -> None:
                nonlocal last_persisted_at, last_persisted_progress
                progress = max(last_persisted_progress, min(progress, 0.99))
                now = time.monotonic()
                if progress - last_persisted_progress < 0.01 and now - last_persisted_at < 0.5:
                    return
                item["progress"] = progress
                last_persisted_progress = progress
                last_persisted_at = now
                self._update_batch_progress(batch)
                self._publish(batch, manifest_path, on_update)

            try:
                if item["composite"] and source_has_audio is None:
                    item["phase"] = "probing_audio"
                    self._publish(batch, manifest_path, on_update)
                    source_has_audio = self._source_has_audio(source)

                planned_encoder = str(item["planned_video_encoder"])
                encoders = [planned_encoder]
                if planned_encoder == VIDEOTOOLBOX_VIDEO_ENCODER:
                    if hardware_disabled_reason:
                        encoders = [SOFTWARE_VIDEO_ENCODER]
                        item["encoder_fallback_reason"] = hardware_disabled_reason
                    else:
                        encoders.append(SOFTWARE_VIDEO_ENCODER)

                for encoder in encoders:
                    item["phase"] = (
                        "encoding_fallback"
                        if encoder == SOFTWARE_VIDEO_ENCODER
                        and planned_encoder != SOFTWARE_VIDEO_ENCODER
                        else "encoding"
                    )
                    item["actual_video_encoder"] = encoder
                    item["hardware_acceleration_used"] = (
                        encoder == VIDEOTOOLBOX_VIDEO_ENCODER
                    )
                    item["ffmpeg_exit_code"] = None
                    item["ffprobe_result"] = None
                    encoder_attempt = {
                        "encoder": encoder,
                        "started_at": datetime.now(UTC).isoformat(),
                        "finished_at": None,
                        "status": "running",
                        "ffmpeg_exit_code": None,
                        "ffprobe_result": None,
                        "error": None,
                    }
                    item["encoder_attempts"].append(encoder_attempt)
                    self._update_encoding_summary(batch)
                    self._publish(batch, manifest_path, on_update)
                    try:
                        self._render_and_verify_item(
                            item,
                            source,
                            temporary,
                            fps=fps,
                            source_has_audio=source_has_audio,
                            encoder=encoder,
                            on_progress=update_progress,
                            cancel_event=cancel_event,
                            process_callback=process_callback,
                            low_priority=low_priority,
                            on_update=lambda: self._publish(
                                batch, manifest_path, on_update
                            ),
                        )
                    except SliceCancelled:
                        encoder_attempt.update(
                            status="cancelled",
                            finished_at=datetime.now(UTC).isoformat(),
                            ffmpeg_exit_code=item.get("ffmpeg_exit_code"),
                            ffprobe_result=item.get("ffprobe_result"),
                            error="任务已取消",
                        )
                        raise
                    except (subprocess.SubprocessError, SliceError) as exc:
                        if isinstance(exc, SliceProcessError):
                            item["ffmpeg_exit_code"] = exc.return_code
                        elif item.get("ffmpeg_exit_code") == 0:
                            item["ffprobe_result"] = "failed"
                        encoder_attempt.update(
                            status="failed",
                            finished_at=datetime.now(UTC).isoformat(),
                            ffmpeg_exit_code=item.get("ffmpeg_exit_code"),
                            ffprobe_result=item.get("ffprobe_result"),
                            error=str(exc),
                        )
                        temporary.unlink(missing_ok=True)
                        if encoder == VIDEOTOOLBOX_VIDEO_ENCODER:
                            hardware_disabled_reason = str(exc)
                            item["encoder_fallback_reason"] = str(exc)
                            encoding = batch.setdefault("encoding", {})
                            encoding["fallback_reason"] = str(exc)
                            self._update_encoding_summary(batch)
                            self._publish(batch, manifest_path, on_update)
                            continue
                        raise
                    else:
                        encoder_attempt.update(
                            status="succeeded",
                            finished_at=datetime.now(UTC).isoformat(),
                            ffmpeg_exit_code=0,
                            ffprobe_result="passed",
                        )
                        break

                if cancel_event is not None and cancel_event.is_set():
                    raise SliceCancelled("任务已取消")
                item["phase"] = "committing"
                self._publish(batch, manifest_path, on_update)
                temporary.replace(Path(item["output_path"]))
                item["status"] = "succeeded"
                item["phase"] = "done"
                item["progress"] = 1.0
                item["finished_at"] = datetime.now(UTC).isoformat()
                batch["success_count"] += 1
            except SliceCancelled as exc:
                item["status"] = "cancelled"
                item["phase"] = "done"
                item["progress"] = 1.0
                item["error"] = str(exc)
                item["finished_at"] = datetime.now(UTC).isoformat()
                batch["cancelled_count"] += 1
                temporary.unlink(missing_ok=True)
            except (OSError, subprocess.SubprocessError, SliceError) as exc:
                item["status"] = "failed"
                item["phase"] = "done"
                item["progress"] = 1.0
                item["error"] = str(exc)
                item["finished_at"] = datetime.now(UTC).isoformat()
                batch["failure_count"] += 1
                temporary.unlink(missing_ok=True)
            self._update_encoding_summary(batch)
            self._update_batch_progress(batch)
            self._publish(batch, manifest_path, on_update)

        batch["ok"] = batch["failure_count"] == 0 and batch["cancelled_count"] == 0
        if batch["cancelled_count"]:
            batch["status"] = "cancelled"
        elif batch["ok"]:
            batch["status"] = "completed"
        elif batch["success_count"]:
            batch["status"] = "partial_failed"
        else:
            batch["status"] = "failed"
        batch["progress"] = 1.0
        batch["finished_at"] = datetime.now(UTC).isoformat()
        self._publish(batch, manifest_path, on_update)
        return batch

    def _render_and_verify_item(
        self,
        item: dict[str, Any],
        source: Path,
        temporary: Path,
        *,
        fps: float,
        source_has_audio: bool | None,
        encoder: str,
        on_progress: Callable[[float], None] | None,
        cancel_event: threading.Event | None,
        process_callback: Callable[[subprocess.Popen[str] | None], None] | None,
        low_priority: bool,
        on_update: Callable[[], None],
    ) -> None:
        if item["composite"]:
            self._render_composite_slice(
                source,
                temporary,
                parts=item["parts"],
                fps=fps,
                has_audio=bool(source_has_audio),
                encoder=encoder,
                on_progress=on_progress,
                cancel_event=cancel_event,
                process_callback=process_callback,
                low_priority=low_priority,
            )
        else:
            self._render_slice(
                source,
                temporary,
                start_seconds=float(item["start_seconds"]),
                duration_seconds=float(item["total_duration_seconds"]),
                encoder=encoder,
                on_progress=on_progress,
                cancel_event=cancel_event,
                process_callback=process_callback,
                low_priority=low_priority,
            )
        item["ffmpeg_exit_code"] = 0
        item["phase"] = "verifying"
        item["progress"] = 0.99
        on_update()
        if item["composite"]:
            self._verify_slice(
                temporary,
                expected_duration=float(item["total_duration_seconds"]),
                expect_audio=source_has_audio,
                fps=fps,
            )
        else:
            self._verify_slice(temporary)
        item["ffprobe_result"] = "passed"

    def _render_slice(
        self,
        source: Path,
        output: Path,
        *,
        start_seconds: float,
        duration_seconds: float,
        encoder: str = SOFTWARE_VIDEO_ENCODER,
        on_progress: Callable[[float], None] | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
        low_priority: bool = False,
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
            *self._video_encoder_arguments(encoder),
            "-c:a",
            "aac",
            "-movflags",
            "+faststart",
            str(output),
        ]
        self._run_ffmpeg(
            command,
            duration_seconds,
            on_progress=on_progress,
            cancel_event=cancel_event,
            process_callback=process_callback,
            error_message="FFmpeg 切片失败",
            low_priority=low_priority,
        )

    def _source_has_audio(self, source: Path) -> bool:
        result = self.runner(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "a:0",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "json",
                str(source),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise SliceError(result.stderr.strip() or "ffprobe 无法检查源视频音轨")
        try:
            streams = json.loads(result.stdout).get("streams", [])
        except json.JSONDecodeError as exc:
            raise SliceError("ffprobe 返回了无效音轨信息") from exc
        return any(stream.get("codec_type") == "audio" for stream in streams)

    def _render_composite_slice(
        self,
        source: Path,
        output: Path,
        *,
        parts: list[dict[str, Any]],
        fps: float,
        has_audio: bool,
        encoder: str = SOFTWARE_VIDEO_ENCODER,
        on_progress: Callable[[float], None] | None = None,
        cancel_event: threading.Event | None = None,
        process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
        low_priority: bool = False,
    ) -> None:
        count = len(parts)
        if count < 2:
            raise SliceError("组合片段至少需要两个成员")

        filters = [
            f"[0:v]split={count}"
            + "".join(f"[vsrc{index}]" for index in range(count))
        ]
        for index, part in enumerate(parts):
            filters.append(
                f"[vsrc{index}]"
                f"trim=start_frame={int(part['start_frame'])}:end_frame={int(part['end_frame'])},"
                f"setpts=PTS-STARTPTS[v{index}]"
            )

        if has_audio:
            filters.append(
                f"[0:a]asplit={count}"
                + "".join(f"[asrc{index}]" for index in range(count))
            )
            for index, part in enumerate(parts):
                start = int(part["start_frame"]) / fps
                end = int(part["end_frame"]) / fps
                filters.append(
                    f"[asrc{index}]atrim=start={start:.9f}:end={end:.9f},"
                    f"asetpts=PTS-STARTPTS[a{index}]"
                )
            concat_inputs = "".join(
                f"[v{index}][a{index}]" for index in range(count)
            )
            filters.append(
                f"{concat_inputs}concat=n={count}:v=1:a=1[vout][aout]"
            )
        else:
            concat_inputs = "".join(f"[v{index}]" for index in range(count))
            filters.append(f"{concat_inputs}concat=n={count}:v=1:a=0[vout]")

        command = [
            "ffmpeg",
            "-hide_banner",
            "-nostdin",
            "-y",
            "-i",
            str(source),
            "-filter_complex",
            ";".join(filters),
            "-map",
            "[vout]",
        ]
        if has_audio:
            command.extend(["-map", "[aout]"])
        command.extend(self._video_encoder_arguments(encoder))
        if has_audio:
            command.extend(["-c:a", "aac"])
        command.extend(["-movflags", "+faststart", str(output)])
        self._run_ffmpeg(
            command,
            sum(float(part["duration_seconds"]) for part in parts),
            on_progress=on_progress,
            cancel_event=cancel_event,
            process_callback=process_callback,
            error_message="FFmpeg 组合片段失败",
            low_priority=low_priority,
        )

    @staticmethod
    def _video_encoder_arguments(encoder: str) -> list[str]:
        if encoder == VIDEOTOOLBOX_VIDEO_ENCODER:
            return [
                "-c:v",
                VIDEOTOOLBOX_VIDEO_ENCODER,
                "-q:v",
                str(VIDEOTOOLBOX_QUALITY),
                "-profile:v",
                "high",
                "-pix_fmt",
                "yuv420p",
            ]
        return [
            "-c:v",
            SOFTWARE_VIDEO_ENCODER,
            "-preset",
            "fast",
            "-crf",
            "18",
            "-pix_fmt",
            "yuv420p",
        ]

    def _run_ffmpeg(
        self,
        command: list[str],
        expected_duration: float,
        *,
        on_progress: Callable[[float], None] | None,
        cancel_event: threading.Event | None,
        process_callback: Callable[[subprocess.Popen[str] | None], None] | None,
        error_message: str,
        low_priority: bool,
    ) -> None:
        if self.runner is not subprocess.run:
            result = self.runner(command, capture_output=True, text=True, check=False)
            if result.returncode != 0:
                raise SliceProcessError(
                    result.stderr.strip() or error_message,
                    int(result.returncode),
                )
            if on_progress is not None:
                on_progress(0.98)
            return

        progress_command = [*command[:-1], "-progress", "pipe:1", "-nostats", command[-1]]
        process = subprocess.Popen(
            progress_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        if low_priority:
            _lower_background_process_priority(process)
        stderr_tail: deque[str] = deque(maxlen=30)
        stderr_done = threading.Event()

        def read_stderr() -> None:
            if process.stderr is not None:
                for line in process.stderr:
                    stderr_tail.append(line.rstrip())
            stderr_done.set()

        threading.Thread(target=read_stderr, daemon=True).start()
        if process_callback is not None:
            process_callback(process)
        try:
            if process.stdout is not None:
                for raw_line in process.stdout:
                    if cancel_event is not None and cancel_event.is_set():
                        process.terminate()
                    key, separator, raw_value = raw_line.strip().partition("=")
                    if not separator or key not in {"out_time_us", "out_time_ms"}:
                        continue
                    try:
                        seconds = float(raw_value) / 1_000_000
                    except ValueError:
                        continue
                    if on_progress is not None and expected_duration > 0:
                        on_progress(min(seconds / expected_duration, 0.98))
            return_code = process.wait()
            stderr_done.wait(timeout=1)
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            raise
        finally:
            if process_callback is not None:
                process_callback(None)
        if cancel_event is not None and cancel_event.is_set():
            raise SliceCancelled("任务已取消")
        if return_code != 0:
            raise SliceProcessError(
                "\n".join(stderr_tail).strip() or error_message,
                return_code,
            )

    def _verify_slice(
        self,
        path: Path,
        *,
        expected_duration: float | None = None,
        expect_audio: bool | None = None,
        fps: float | None = None,
    ) -> None:
        result = self.runner(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration:stream=codec_type",
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
            data = json.loads(result.stdout)
            streams = data.get("streams", [])
        except json.JSONDecodeError as exc:
            raise SliceError("ffprobe 返回了无效结果") from exc
        if (
            not any(stream.get("codec_type") == "video" for stream in streams)
            or not path.is_file()
            or path.stat().st_size <= 0
        ):
            raise SliceError("切片结果中没有可用视频流")
        if expect_audio and not any(
            stream.get("codec_type") == "audio" for stream in streams
        ):
            raise SliceError("组合切片结果缺少音频流")
        if expected_duration is not None:
            try:
                actual_duration = float(data.get("format", {}).get("duration"))
            except (TypeError, ValueError) as exc:
                raise SliceError("无法校验组合切片时长") from exc
            tolerance = max(2 / fps, 0.08) if fps else 0.08
            if abs(actual_duration - expected_duration) > tolerance:
                raise SliceError(
                    "组合切片时长异常: "
                    f"期望 {expected_duration:.3f}s，实际 {actual_duration:.3f}s"
                )

    @staticmethod
    def _unique_output(path: Path, reserved_paths: set[str] | None = None) -> Path:
        reserved = reserved_paths or set()
        if not path.exists() and str(path) not in reserved:
            return path
        version = 2
        while True:
            candidate = path.with_name(f"{path.stem}-v{version}{path.suffix}")
            if not candidate.exists() and str(candidate) not in reserved:
                return candidate
            version += 1

    @staticmethod
    def _update_encoding_summary(batch: dict[str, Any]) -> None:
        encoding = batch.setdefault("encoding", {})
        actual_encoders = sorted(
            {
                str(item["actual_video_encoder"])
                for item in batch.get("items", [])
                if item.get("actual_video_encoder")
            }
        )
        encoding["actual_video_encoders"] = actual_encoders
        encoding["hardware_acceleration_used"] = any(
            item.get("actual_video_encoder") == VIDEOTOOLBOX_VIDEO_ENCODER
            and item.get("status") in {"running", "succeeded"}
            for item in batch.get("items", [])
        )
        encoding["fallback_count"] = sum(
            bool(item.get("encoder_fallback_reason"))
            for item in batch.get("items", [])
        )
        encoding.setdefault("fallback_reason", None)

    @staticmethod
    def _update_batch_progress(batch: dict[str, Any]) -> None:
        active = [item for item in batch["items"] if item["status"] != "skipped"]
        total_duration = sum(float(item["total_duration_seconds"]) for item in active)
        if total_duration <= 0:
            batch["progress"] = 1.0 if not active else 0.0
            return
        completed = sum(
            float(item["total_duration_seconds"]) * float(item.get("progress") or 0)
            for item in active
        )
        batch["progress"] = min(completed / total_duration, 1.0)

    @staticmethod
    def _publish(
        batch: dict[str, Any],
        manifest_path: Path,
        on_update: Callable[[dict[str, Any]], None] | None,
    ) -> None:
        if on_update is not None:
            on_update(batch)
        else:
            _atomic_json(manifest_path, batch)
