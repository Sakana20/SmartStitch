from __future__ import annotations

import copy
import csv
import json
import shutil
import subprocess
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigStore
from .database import SQLiteStore
from .models import AppConfig, JobCreateRequest
from .planner import build_plan
from .renderer import render_item
from .scanner import scan_config

TERMINAL_STATES = {"completed", "partial_failed", "failed", "cancelled", "interrupted"}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class JobDatabase:
    def __init__(self, database: Path | SQLiteStore):
        self.store = database if isinstance(database, SQLiteStore) else SQLiteStore(database)
        self.path = self.store.path
        self.lock = self.store.lock
        self.store.ensure_job_table("jobs")
        self.store.mark_active_interrupted("jobs")

    def save(self, job: dict[str, Any]) -> None:
        self.store.save("jobs", job)

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.store.get("jobs", job_id)

    def list(self) -> list[dict[str, Any]]:
        return self.store.list("jobs")

    def delete(self, job_id: str) -> bool:
        return self.store.delete("jobs", job_id)

    def delete_all(self) -> int:
        return self.store.delete_all("jobs")


class JobManager:
    def __init__(
        self,
        config_store: ConfigStore,
        data_directory: Path,
        database_store: SQLiteStore | None = None,
    ):
        self.config_store = config_store
        self.data_directory = data_directory
        self.jobs_directory = data_directory / "jobs"
        self.jobs_directory.mkdir(parents=True, exist_ok=True)
        self.database = JobDatabase(
            database_store or SQLiteStore(data_directory / "smartstitch.db")
        )
        self.cancel_events: dict[str, threading.Event] = {}
        self.processes: dict[tuple[str, int], subprocess.Popen[str]] = {}
        self.lock = threading.RLock()

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self._summary(job) for job in self.database.list()]

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def create(self, request: JobCreateRequest) -> dict[str, Any]:
        config = self.config_store.load(request.config_id)
        if not config.enabled:
            raise ValueError("配置已停用")
        if request.output_directory:
            config.output.directory = request.output_directory
        scan = scan_config(config)
        job_id = uuid.uuid4().hex
        short_id = job_id[:8]
        plan = build_plan(config, scan, request.count, request.seed, short_id)
        batch_directory = self._batch_directory(config, short_id)
        batch_directory.parent.mkdir(parents=True, exist_ok=True)
        free_gb = shutil.disk_usage(batch_directory.parent).free / (1024**3)
        if free_gb < config.batch.minimum_free_space_gb:
            raise OSError(
                f"输出磁盘剩余空间 {free_gb:.2f} GB，低于配置要求的 "
                f"{config.batch.minimum_free_space_gb:.2f} GB"
            )
        batch_directory.mkdir(parents=True, exist_ok=False)
        snapshot_path = batch_directory / "config.snapshot.yaml"
        snapshot_path.write_text(
            yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        items = []
        for planned in plan.items:
            items.append(
                {
                    "index": planned.index,
                    "status": "pending",
                    "progress": 0.0,
                    "output_name": planned.output_name,
                    "output_path": str(batch_directory / planned.output_name),
                    "estimated_duration": planned.estimated_duration,
                    "actual_duration": None,
                    "attempts": 0,
                    "error": None,
                    "selections": {
                        category: asset.model_dump(mode="json") if asset else None
                        for category, asset in planned.selections.items()
                    },
                    "overlay": planned.overlay.model_dump(mode="json") if planned.overlay else None,
                    "naming": planned.naming.model_dump(mode="json") if planned.naming else None,
                }
            )
        job = {
            "id": job_id,
            "short_id": short_id,
            "config_id": config.id,
            "config_name": config.name,
            "schema_version": config.schema_version,
            "workflow_type": config.workflow_type,
            "timeline": list(config.timeline),
            "pool_labels": {
                category: config.sources[category].label or category
                for category in config.timeline
                if config.workflow_type == "generic"
            },
            "status": "draft",
            "count": plan.count,
            "seed": plan.seed,
            "algorithm": plan.algorithm,
            "algorithm_version": 2,
            "distribution": plan.distribution,
            "warnings": plan.warnings,
            "output_directory": str(batch_directory),
            "config_snapshot_path": str(snapshot_path),
            "concurrency": request.concurrency or config.batch.concurrency,
            "retry_count": config.batch.retry_count,
            "success_count": 0,
            "failure_count": 0,
            "created_at": _now(),
            "started_at": None,
            "finished_at": None,
            "items": items,
        }
        self.database.save(job)
        self._write_manifest(job)
        if request.auto_start:
            self.start(job_id)
        return self.get_job(job_id)

    def start(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.get_job(job_id)
            if job["status"] != "draft":
                raise ValueError(f"只有 draft 任务可以启动，当前为 {job['status']}")
            job["status"] = "queued"
            self.database.save(job)
            cancel_event = threading.Event()
            self.cancel_events[job_id] = cancel_event
            thread = threading.Thread(target=self._run, args=(job_id,), daemon=True)
            thread.start()
        return self.get_job(job_id)

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.get_job(job_id)
            if job["status"] in TERMINAL_STATES:
                return job
            job["status"] = "cancelling"
            self.database.save(job)
            event = self.cancel_events.setdefault(job_id, threading.Event())
            event.set()
            for (running_job, _), process in list(self.processes.items()):
                if running_job == job_id and process.poll() is None:
                    process.terminate()
        return self.get_job(job_id)

    def delete(self, job_id: str) -> None:
        with self.lock:
            job = self.get_job(job_id)
            if job["status"] not in TERMINAL_STATES | {"draft"}:
                raise ValueError("任务仍在处理中，请先取消并等待任务结束")
            if not self.database.delete(job_id):
                raise KeyError(job_id)
            self.cancel_events.pop(job_id, None)

    def delete_all(self) -> int:
        with self.lock:
            jobs = self.database.list()
            active = [job for job in jobs if job["status"] not in TERMINAL_STATES | {"draft"}]
            if active:
                raise ValueError(f"还有 {len(active)} 个任务正在处理中，请先取消并等待任务结束")
            deleted_count = self.database.delete_all()
            self.cancel_events.clear()
            return deleted_count

    def _run(self, job_id: str) -> None:
        job = self.get_job(job_id)
        config = AppConfig.model_validate(yaml.safe_load(Path(job["config_snapshot_path"]).read_text("utf-8")))
        job["status"] = "running"
        job["started_at"] = _now()
        self.database.save(job)
        cancel_event = self.cancel_events[job_id]
        concurrency = max(1, int(job["concurrency"]))
        with ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix=f"job-{job_id[:6]}") as pool:
            futures = {
                pool.submit(self._run_item, job_id, config, item["index"], cancel_event): item["index"]
                for item in job["items"]
            }
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception:
                    pass
                if cancel_event.is_set():
                    for pending in futures:
                        pending.cancel()

        job = self.get_job(job_id)
        for item in job["items"]:
            if item["status"] == "pending":
                item["status"] = "cancelled" if cancel_event.is_set() else "failed"
                item["error"] = "任务已取消" if cancel_event.is_set() else "未执行"
        job["success_count"] = sum(item["status"] == "succeeded" for item in job["items"])
        job["failure_count"] = sum(item["status"] == "failed" for item in job["items"])
        if cancel_event.is_set():
            job["status"] = "cancelled"
        elif job["success_count"] == job["count"]:
            job["status"] = "completed"
        elif job["success_count"]:
            job["status"] = "partial_failed"
        else:
            job["status"] = "failed"
        job["finished_at"] = _now()
        self.database.save(job)
        self._write_manifest(job)
        self._write_csv(job)
        with self.lock:
            self.cancel_events.pop(job_id, None)

    def _run_item(
        self,
        job_id: str,
        config: AppConfig,
        item_index: int,
        cancel_event: threading.Event,
    ) -> None:
        if cancel_event.is_set():
            return
        self._mutate_item(job_id, item_index, status="running")
        job = self.get_job(job_id)
        item_data = next(item for item in job["items"] if item["index"] == item_index)
        from .models import Asset, PlanItem

        planned = PlanItem(
            index=item_data["index"],
            selections={
                category: Asset.model_validate(asset) if asset else None
                for category, asset in item_data["selections"].items()
            },
            overlay=Asset.model_validate(item_data["overlay"]) if item_data["overlay"] else None,
            output_name=item_data["output_name"],
            estimated_duration=item_data["estimated_duration"],
        )
        error: str | None = None
        for attempt in range(int(job["retry_count"]) + 1):
            if cancel_event.is_set():
                break
            self._mutate_item(job_id, item_index, attempts=attempt + 1, error=None)
            try:
                result = render_item(
                    config,
                    planned,
                    Path(item_data["output_path"]),
                    cancel_event,
                    on_progress=lambda progress: self._mutate_item(job_id, item_index, progress=progress),
                    process_callback=lambda process: self._track_process(job_id, item_index, process),
                )
                self._mutate_item(
                    job_id,
                    item_index,
                    status="succeeded",
                    progress=1.0,
                    actual_duration=result["actual_duration"],
                    error=None,
                )
                return
            except Exception as exc:
                error = str(exc)
        self._mutate_item(
            job_id,
            item_index,
            status="cancelled" if cancel_event.is_set() else "failed",
            error=error or "任务已取消",
        )

    def _mutate_item(self, job_id: str, item_index: int, **updates: Any) -> None:
        with self.lock:
            job = self.get_job(job_id)
            item = next(item for item in job["items"] if item["index"] == item_index)
            item.update(updates)
            job["success_count"] = sum(entry["status"] == "succeeded" for entry in job["items"])
            job["failure_count"] = sum(entry["status"] == "failed" for entry in job["items"])
            self.database.save(job)

    def _track_process(
        self, job_id: str, item_index: int, process: subprocess.Popen[str] | None
    ) -> None:
        with self.lock:
            key = (job_id, item_index)
            if process is None:
                self.processes.pop(key, None)
            else:
                self.processes[key] = process

    def _batch_directory(self, config: AppConfig, short_id: str) -> Path:
        safe_name = config.name.replace("/", "-").replace(":", "-")
        directory = Path(config.output.directory).expanduser()
        if not directory.is_absolute():
            directory = Path.cwd() / directory
        name = f"{datetime.now():%Y%m%d_%H%M%S}_{safe_name}_{short_id}"
        return directory / name

    def _summary(self, job: dict[str, Any]) -> dict[str, Any]:
        summary = copy.deepcopy(job)
        summary.pop("items", None)
        return summary

    def _write_manifest(self, job: dict[str, Any]) -> None:
        path = Path(job["output_directory"]) / "manifest.json"
        path.write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")

    def _write_csv(self, job: dict[str, Any]) -> None:
        path = Path(job["output_directory"]) / "manifest.csv"
        has_naming = any(item.get("naming") for item in job["items"])
        naming_columns = (
            ["product", "benefit", "talents", "restriction_date"]
            if has_naming
            else []
        )
        available = {key for item in job["items"] for key in item["selections"]}
        categories = [category for category in job.get("timeline", []) if category in available]
        categories.extend(sorted(available - set(categories)))
        pool_labels = job.get("pool_labels", {})
        category_columns = {
            category: (
                f"{index:02d}_{category}_{pool_labels.get(category, category)}"
                if job.get("workflow_type") == "generic"
                else category
            )
            for index, category in enumerate(categories, start=1)
        }
        with path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "index",
                    "status",
                    *naming_columns,
                    *category_columns.values(),
                    "benefit_overlay",
                    "output_path",
                    "error",
                ],
            )
            writer.writeheader()
            for item in job["items"]:
                row = {
                    "index": item["index"],
                    "status": item["status"],
                    "benefit_overlay": item["overlay"]["path"] if item["overlay"] else "",
                    "output_path": item["output_path"],
                    "error": item["error"] or "",
                }
                naming = item.get("naming")
                if naming:
                    row.update(
                        {
                            "product": naming["product"],
                            "benefit": naming["benefit"],
                            "talents": "+".join(naming["talents"]),
                            "restriction_date": naming["restriction_date"],
                        }
                    )
                for category in categories:
                    asset = item["selections"].get(category)
                    row[category_columns[category]] = asset["path"] if asset else ""
                writer.writerow(row)
