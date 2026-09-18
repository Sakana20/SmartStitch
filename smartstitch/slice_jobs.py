from __future__ import annotations

import copy
import json
import queue
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .database import SQLiteStore
from .models import TimelineSliceRequest
from .slicer import TimelineSlicer, _atomic_json, slice_request_key


SLICE_TERMINAL_STATES = {
    "completed",
    "partial_failed",
    "failed",
    "cancelled",
    "interrupted",
}
SLICE_ACTIVE_STATES = {"queued", "running", "cancelling"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


class SliceJobDatabase:
    def __init__(self, database: Path | SQLiteStore):
        self.store = (
            database if isinstance(database, SQLiteStore) else SQLiteStore(database)
        )
        self.path = self.store.path
        self.lock = self.store.lock
        self.store.ensure_job_table("slice_jobs", request_key=True)
        self.store.mark_active_interrupted("slice_jobs")

    def save(self, job: dict[str, Any]) -> None:
        self.store.save("slice_jobs", job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get("slice_jobs", job_id)

    def get_by_request_key(self, request_key: str) -> dict[str, Any] | None:
        return self.store.get_by_request_key("slice_jobs", request_key)

    def list(self) -> list[dict[str, Any]]:
        return self.store.list("slice_jobs")

    def delete(self, job_id: str) -> bool:
        return self.store.delete("slice_jobs", job_id)


class SliceJobManager:
    def __init__(self, slicer: TimelineSlicer, database_store: SQLiteStore):
        self.slicer = slicer
        self.database = SliceJobDatabase(database_store)
        self.lock = threading.RLock()
        self.pending: queue.Queue[str] = queue.Queue()
        self.cancel_events: dict[str, threading.Event] = {}
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.worker = threading.Thread(
            target=self._worker_loop,
            name="slice-job-worker",
            daemon=True,
        )
        self.worker.start()

    def create(self, request: TimelineSliceRequest) -> dict[str, Any]:
        request_key = slice_request_key(request)
        with self.lock:
            existing = self.database.get_by_request_key(request_key)
            if existing is not None:
                result = self._summary(existing)
                result["idempotent"] = True
                return result

            config = self.slicer.library_service.config_store.load(request.config_id)
            manifest_path = (
                Path(config.source_root).expanduser().resolve()
                / "工作记录"
                / "切片清单"
                / f"slice-{request_key}.json"
            )
            if manifest_path.is_file():
                try:
                    saved = json.loads(manifest_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    raise ValueError("已有切片清单损坏，无法确认重复请求") from exc
                imported = self._import_manifest(saved, request, request_key, manifest_path)
                self.database.save(imported)
                result = self._summary(imported)
                result["idempotent"] = True
                return result

            reserved_paths = {
                str(item["output_path"])
                for job in self.database.list()
                if job.get("status") in SLICE_ACTIVE_STATES | {"interrupted"}
                for item in job.get("items", [])
                if item.get("output_path")
            }
            job = self.slicer.prepare(
                request,
                reserved_paths=reserved_paths,
                job_id=uuid.uuid4().hex,
                write_manifest=False,
            )
            self.database.save(job)
            try:
                _atomic_json(Path(job["manifest_path"]), job)
            except OSError as exc:
                job["status"] = "failed"
                job["ok"] = False
                job["finished_at"] = _now()
                job["manifest_sync_error"] = str(exc)
                self.database.save(job)
                raise
            cancel_event = threading.Event()
            self.cancel_events[job["id"]] = cancel_event
            self.pending.put(job["id"])
            return self._summary(job)

    @staticmethod
    def _import_manifest(
        saved: dict[str, Any],
        request: TimelineSliceRequest,
        request_key: str,
        manifest_path: Path,
    ) -> dict[str, Any]:
        imported = copy.deepcopy(saved)
        job_id = str(imported.get("id") or uuid.uuid4().hex)
        imported.update(
            id=job_id,
            short_id=str(imported.get("short_id") or job_id[:8]),
            job_type="timeline_slice",
            request_key=request_key,
            client_request_id=request.client_request_id,
            manifest_path=str(manifest_path),
        )
        if imported.get("status") in SLICE_ACTIVE_STATES:
            imported["status"] = "interrupted"
            imported["finished_at"] = _now()
        imported.setdefault(
            "progress",
            1.0 if imported.get("status") in SLICE_TERMINAL_STATES else 0.0,
        )
        imported.setdefault("cancelled_count", 0)
        imported.setdefault("manifest_sync_error", None)
        imported.setdefault("finished_at", None)
        for item in imported.get("items", []):
            terminal_item = item.get("status") in {"succeeded", "failed", "skipped"}
            item.setdefault("phase", "done" if terminal_item else "waiting")
            item.setdefault("progress", 1.0 if terminal_item else 0.0)
            item.setdefault(
                "attempts",
                1 if item.get("status") in {"succeeded", "failed"} else 0,
            )
            item.setdefault("started_at", None)
            item.setdefault("finished_at", None)
            item.setdefault("temporary_path", None)
            item.setdefault("ffmpeg_exit_code", None)
            item.setdefault("ffprobe_result", None)
        return imported

    def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        jobs = self.database.list()
        if status == "active":
            jobs = [job for job in jobs if job.get("status") in SLICE_ACTIVE_STATES]
        elif status:
            jobs = [job for job in jobs if job.get("status") == status]
        return [self._summary(job) for job in jobs]

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.get_job(job_id)
            if job["status"] in SLICE_TERMINAL_STATES:
                return job
            event = self.cancel_events.setdefault(job_id, threading.Event())
            event.set()
            if job["status"] == "queued":
                for item in job["items"]:
                    if item["status"] == "pending":
                        item.update(
                            status="cancelled",
                            phase="done",
                            progress=1.0,
                            error="任务已取消",
                            finished_at=_now(),
                        )
                job["cancelled_count"] = sum(
                    item["status"] == "cancelled" for item in job["items"]
                )
                job["status"] = "cancelled"
                job["ok"] = False
                job["progress"] = 1.0
                job["finished_at"] = _now()
            else:
                job["status"] = "cancelling"
                process = self.processes.get(job_id)
                if process is not None and process.poll() is None:
                    process.terminate()
                    threading.Thread(
                        target=self._kill_if_running,
                        args=(process,),
                        daemon=True,
                    ).start()
            self._persist(job)
            return job

    def delete(self, job_id: str) -> None:
        with self.lock:
            job = self.get_job(job_id)
            if job["status"] not in SLICE_TERMINAL_STATES:
                raise ValueError("任务仍在处理中，请先取消并等待任务结束")
            if not self.database.delete(job_id):
                raise KeyError(job_id)
            self.cancel_events.pop(job_id, None)
            self.processes.pop(job_id, None)

    def retry(self, job_id: str, *, interrupted_only: bool = False) -> dict[str, Any]:
        with self.lock:
            job = self.get_job(job_id)
            allowed = {"interrupted"} if interrupted_only else {
                "partial_failed",
                "failed",
                "cancelled",
                "interrupted",
            }
            if job["status"] not in allowed:
                raise ValueError("当前任务状态不能继续或重试")
            self._validate_retry(job)
            reset_count = 0
            for item in job["items"]:
                if item["status"] in {"succeeded", "skipped"}:
                    continue
                item.update(
                    status="pending",
                    phase="waiting",
                    progress=0.0,
                    started_at=None,
                    finished_at=None,
                    temporary_path=None,
                    ffmpeg_exit_code=None,
                    ffprobe_result=None,
                    error=None,
                )
                reset_count += 1
            if not reset_count:
                raise ValueError("没有可继续执行的切片条目")
            job["status"] = "queued"
            job["ok"] = True
            job["failure_count"] = 0
            job["cancelled_count"] = 0
            job["finished_at"] = None
            job["error"] = None
            job["manifest_sync_error"] = None
            self.slicer._update_batch_progress(job)
            cancel_event = threading.Event()
            self.cancel_events[job_id] = cancel_event
            self._persist(job)
            self.pending.put(job_id)
            return self._summary(job)

    def _validate_retry(self, job: dict[str, Any]) -> None:
        source = Path(str(job["source"]["path"]))
        if not source.is_file():
            raise ValueError("原视频不存在，无法继续切片")
        source_stat = source.stat()
        if (
            source_stat.st_size != int(job["source"]["size_bytes"])
            or source_stat.st_mtime_ns != int(job["source"]["modified_at_ns"])
        ):
            raise ValueError("原视频已变更，请重新分析后提交")
        inspected = self.slicer.library_service.inspect_by_config(job["config_id"])
        if not inspected.get("managed") or inspected.get("health") != "healthy":
            raise ValueError("目标视频库当前不可用")
        if inspected.get("library_id") != job.get("library_id"):
            raise ValueError("配置已指向其他视频库，不能继续原任务")
        for item in job["items"]:
            if item["status"] == "succeeded":
                output = Path(str(item["output_path"]))
                if not output.is_file() or output.stat().st_size <= 0:
                    raise ValueError("已成功的切片文件缺失，请重新提交整批任务")
                continue
            if item["status"] == "skipped":
                continue
            output = Path(str(item["output_path"]))
            if output.exists():
                raise ValueError(f"待执行输出路径已被占用: {output}")
            target = Path(str(item["target_directory"]))
            if not target.is_dir():
                raise ValueError(f"切片目标目录不存在: {target}")
            current_target = self.slicer.library_service.resolve_slice_target(
                job["config_id"], item["category"]
            )
            if current_target.resolve() != target.resolve():
                raise ValueError("切片类别的入库目录已变更，请重新提交")

    def _worker_loop(self) -> None:
        while True:
            job_id = self.pending.get()
            try:
                self._run(job_id)
            finally:
                self.pending.task_done()

    def _run(self, job_id: str) -> None:
        try:
            job = self.get_job(job_id)
        except KeyError:
            return
        if job["status"] != "queued":
            return
        cancel_event = self.cancel_events.setdefault(job_id, threading.Event())
        try:
            self.slicer.execute(
                job,
                on_update=self._persist,
                cancel_event=cancel_event,
                process_callback=lambda process: self._track_process(job_id, process),
                low_priority=True,
            )
        except Exception as exc:
            try:
                failed_job = self.get_job(job_id)
            except Exception:
                failed_job = job
            if failed_job.get("status") not in SLICE_TERMINAL_STATES:
                failure_status = "cancelled" if cancel_event.is_set() else "failed"
                finished_at = _now()
                for item in failed_job.get("items", []):
                    if item.get("status") not in {"pending", "running"}:
                        continue
                    item.update(
                        status=failure_status,
                        phase="done",
                        progress=1.0,
                        error=str(exc),
                        finished_at=finished_at,
                    )
                failed_job["status"] = failure_status
                failed_job["ok"] = False
                failed_job["finished_at"] = finished_at
                failed_job["error"] = str(exc)
                self.slicer._update_batch_progress(failed_job)
                try:
                    self._persist(failed_job)
                except Exception:
                    try:
                        _atomic_json(Path(failed_job["manifest_path"]), failed_job)
                    except OSError:
                        pass
        finally:
            with self.lock:
                self.cancel_events.pop(job_id, None)
                self.processes.pop(job_id, None)

    def _persist(self, job: dict[str, Any]) -> None:
        with self.lock:
            current = self.database.get(str(job["id"]))
            if (
                current is not None
                and current.get("status") == "cancelling"
                and job.get("status") == "running"
            ):
                job["status"] = "cancelling"
            self.database.save(job)
            try:
                _atomic_json(Path(job["manifest_path"]), job)
            except OSError as exc:
                job["manifest_sync_error"] = str(exc)
                self.database.save(job)
            else:
                if job.get("manifest_sync_error"):
                    job["manifest_sync_error"] = None
                    self.database.save(job)
                    _atomic_json(Path(job["manifest_path"]), job)

    def _track_process(
        self, job_id: str, process: subprocess.Popen[str] | None
    ) -> None:
        with self.lock:
            if process is None:
                self.processes.pop(job_id, None)
            else:
                self.processes[job_id] = process

    @staticmethod
    def _kill_if_running(process: subprocess.Popen[str]) -> None:
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()

    @staticmethod
    def _summary(job: dict[str, Any]) -> dict[str, Any]:
        summary = copy.deepcopy(job)
        summary.pop("items", None)
        return summary
