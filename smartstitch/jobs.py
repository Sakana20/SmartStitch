from __future__ import annotations

import copy
import csv
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .config import ConfigStore
from .database import SQLiteStore
from .folder_concat import VIDEO_SUFFIXES, inspect_folders
from .models import AppConfig, BatchDedupRequest, FolderConcatRequest, JobCreateRequest, SourceMode, VisualDedupConfig
from .planner import build_plan
from .probe_cache import MediaProbeCache
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
        visual_border_library: Any | None = None,
        media_probe_cache: MediaProbeCache | None = None,
    ):
        self.config_store = config_store
        self.data_directory = data_directory
        self.jobs_directory = data_directory / "jobs"
        self.batch_dedup_settings_path = data_directory / "batch_dedup_settings.json"
        self.jobs_directory.mkdir(parents=True, exist_ok=True)
        shared_store = database_store or SQLiteStore(data_directory / "smartstitch.db")
        self.database = JobDatabase(shared_store)
        self.cancel_events: dict[str, threading.Event] = {}
        self.processes: dict[tuple[str, int], subprocess.Popen[str]] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.lock = threading.RLock()
        self.stopping = False
        self.output_sync_manager: Any | None = None
        self.visual_border_library = visual_border_library
        self.media_probe_cache = media_probe_cache or MediaProbeCache(shared_store)

    def list_jobs(self) -> list[dict[str, Any]]:
        return [self._summary(job) for job in self.database.list()]

    def get_job(self, job_id: str) -> dict[str, Any]:
        job = self.database.get(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def create(self, request: JobCreateRequest) -> dict[str, Any]:
        return self._create(request)

    def create_batch_dedup(self, request: BatchDedupRequest) -> dict[str, Any]:
        return self._create(
            JobCreateRequest(config_id="batch-dedup", count=1),
            dedup_source=request.source_directory,
            dedup_visual=request.visual_dedup,
        )

    def create_folder_concat(self, request: FolderConcatRequest) -> dict[str, Any]:
        preview = inspect_folders(request)
        if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
            raise ValueError("找不到 FFmpeg 或 ffprobe")
        output_root = (
            Path(request.output_directory).expanduser().resolve()
            if request.output_directory else Path(preview["directory_a"]).parent
        )
        if not output_root.is_dir():
            raise ValueError("输出位置不存在或不是文件夹")
        config = self._folder_concat_config(preview, output_root)
        scan = scan_config(config, probe_cache=self.media_probe_cache)
        assets_by_path = {
            category: {asset.path: asset for asset in scan.assets[category]}
            for category in ("pool_1", "pool_2")
        }
        invalid = [
            asset
            for pair in preview["pairs"]
            for category, path in (("pool_1", pair["a"]), ("pool_2", pair["b"]))
            if not (asset := assets_by_path[category][path]).selectable
        ]
        if invalid:
            details = "；".join(f"{asset.name}: {asset.error or '不可用'}" for asset in invalid[:5])
            raise ValueError(f"文件夹中有不可用视频，请先处理：{details}")
        first = assets_by_path["pool_1"][preview["pairs"][0]["a"]].probe
        if first.width and first.height:
            config.output.width = max(2, first.width // 2 * 2)
            config.output.height = max(2, first.height // 2 * 2)
        if first.fps:
            config.output.fps = first.fps
        job_id = uuid.uuid4().hex
        short_id = job_id[:8]
        batch_directory = self._batch_directory(config, short_id)
        free_gb = shutil.disk_usage(output_root).free / (1024**3)
        if free_gb < config.batch.minimum_free_space_gb:
            raise OSError(f"输出磁盘剩余空间 {free_gb:.2f} GB，低于配置要求")
        batch_directory.mkdir(parents=True, exist_ok=False)
        snapshot_path = batch_directory / "config.snapshot.yaml"
        snapshot_path.write_text(
            yaml.safe_dump(config.model_dump(mode="json"), allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )
        items = []
        used_names: set[str] = set()
        for index, pair in enumerate(preview["pairs"], start=1):
            a = assets_by_path["pool_1"][pair["a"]]
            b = assets_by_path["pool_2"][pair["b"]]
            base = f"{Path(pair['a_name']).stem}_拼接"
            output_name = f"{base}.mp4"
            suffix = 2
            while output_name.casefold() in used_names:
                output_name = f"{base}_{suffix}.mp4"
                suffix += 1
            used_names.add(output_name.casefold())
            items.append({
                "index": index, "status": "pending", "progress": 0.0,
                "output_name": output_name,
                "output_path": str(batch_directory / output_name),
                "estimated_duration": round(a.probe.duration + b.probe.duration, 3),
                "actual_duration": None,
                "planned_video_encoder": config.output.video_codec,
                "actual_video_encoder": None,
                "hardware_acceleration": False,
                "encoder_fallback_reason": None,
                "attempts": 0, "error": None,
                "selections": {
                    "pool_1": a.model_dump(mode="json"),
                    "pool_2": b.model_dump(mode="json"),
                },
                "overlay": None, "visual_border": None,
                "visual_effects": [], "naming": None,
            })
        job = {
            "id": job_id, "job_type": "folder_concat",
            "source_directory": preview["directory_a"],
            "source_directory_b": preview["directory_b"],
            "short_id": short_id, "config_id": config.id,
            "config_name": config.name, "schema_version": config.schema_version,
            "workflow_type": config.workflow_type,
            "timeline": list(config.timeline),
            "pool_labels": {"pool_1": "A 文件夹", "pool_2": "B 文件夹"},
            "status": "draft", "count": len(items), "seed": None,
            "algorithm": "filename_order_pairing", "algorithm_version": 1,
            "distribution": {},
            "warnings": [f"多出的 {preview['unpaired_a']} 条 A 视频和 {preview['unpaired_b']} 条 B 视频未配对"]
            if preview["unpaired_a"] or preview["unpaired_b"] else [],
            "output_directory": str(batch_directory),
            "config_snapshot_path": str(snapshot_path),
            "visual_dedup": config.visual_dedup.model_dump(mode="json"),
            "feishu_base_sync": config.output.feishu_base_sync.model_dump(mode="json"),
            "concurrency": config.batch.concurrency,
            "retry_count": config.batch.retry_count,
            "success_count": 0, "failure_count": 0,
            "created_at": _now(), "started_at": None,
            "finished_at": None, "items": items,
        }
        self.database.save(job)
        self._write_manifest(job)
        self.start(job_id)
        return self.get_job(job_id)

    @staticmethod
    def _folder_concat_config(preview: dict[str, Any], output_root: Path) -> AppConfig:
        return AppConfig.model_validate({
            "schema_version": 3, "workflow_type": "generic",
            "id": "folder-concat", "name": "文件夹拼接",
            "source_root": preview["directory_a"],
            "timeline": ["pool_1", "pool_2"],
            "sources": {
                "pool_1": {"label": "A 文件夹", "mode": "required",
                           "directory": preview["directory_a"], "extensions": sorted(VIDEO_SUFFIXES)},
                "pool_2": {"label": "B 文件夹", "mode": "required",
                           "directory": preview["directory_b"], "extensions": sorted(VIDEO_SUFFIXES)},
            },
            "benefit_overlays": {"mode": "disabled", "file": ""},
            "output": {"directory": str(output_root), "video_codec": "libx264"},
            "batch": {"minimum_free_space_gb": 0.5, "retry_count": 0},
        })

    def get_batch_dedup_settings(self) -> VisualDedupConfig:
        with self.lock:
            if self.batch_dedup_settings_path.exists():
                return VisualDedupConfig.model_validate(
                    json.loads(self.batch_dedup_settings_path.read_text(encoding="utf-8"))
                )
        return VisualDedupConfig(enabled=True)

    def save_batch_dedup_settings(self, settings: VisualDedupConfig) -> VisualDedupConfig:
        path = self.batch_dedup_settings_path
        temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        with self.lock:
            try:
                temporary.write_text(
                    settings.model_dump_json(indent=2), encoding="utf-8"
                )
                os.replace(temporary, path)
            finally:
                temporary.unlink(missing_ok=True)
        return settings

    def _create(
        self, request: JobCreateRequest, *, dedup_source: str | None = None,
        dedup_visual: VisualDedupConfig | None = None,
    ) -> dict[str, Any]:
        if dedup_source is not None:
            source = Path(dedup_source).expanduser().resolve()
            if not source.is_dir():
                raise ValueError("源视频文件夹不存在或不是文件夹")
            if dedup_visual is None or not dedup_visual.enabled:
                raise ValueError("请先开启视觉去重")
            if not any(
                layer.enabled and layer.opacity_percent > 0
                for layer in dedup_visual.effect_layers
            ):
                raise ValueError("请至少启用一个透明度大于 0 的特效层")
            config = self._dedup_config(dedup_visual, source)
        else:
            config = self.config_store.load(request.config_id)
            if not config.enabled:
                raise ValueError("配置已停用")
            if request.output_directory:
                config.output.directory = request.output_directory
        global_borders = (
            self.visual_border_library.assets_for_effect_layers(config)
            if self.visual_border_library is not None
            else None
        )
        scan = scan_config(config, global_borders, self.media_probe_cache)
        dedup_assets = []
        if dedup_source is not None:
            invalid = [
                asset for asset in scan.assets.get("pool_1", [])
                if not asset.selectable
            ]
            if invalid:
                details = "；".join(
                    f"{asset.name}: {asset.error or '不可用'}" for asset in invalid[:5]
                )
                raise ValueError(f"源文件夹中有不可用视频，请先处理：{details}")
            dedup_assets = sorted(
                (asset for asset in scan.assets.get("pool_1", []) if asset.selectable),
                key=lambda asset: (asset.name.casefold(), asset.name),
            )
            if not dedup_assets:
                raise ValueError("所选文件夹没有可用视频")
        job_id = uuid.uuid4().hex
        short_id = job_id[:8]
        plan = build_plan(
            config, scan, len(dedup_assets) if dedup_source is not None else request.count,
            request.seed, short_id,
        )
        if dedup_source is not None:
            used_names: set[str] = set()
            for planned, asset in zip(plan.items, dedup_assets, strict=True):
                planned.selections = {"pool_1": asset}
                planned.estimated_duration = round(asset.probe.duration, 3)
                base_name = f"{Path(asset.name).stem}_去重"
                output_name = f"{base_name}.mp4"
                suffix = 2
                while output_name.casefold() in used_names:
                    output_name = f"{base_name}_{suffix}.mp4"
                    suffix += 1
                used_names.add(output_name.casefold())
                planned.output_name = output_name
            plan.distribution["pool_1"] = {asset.name: 1 for asset in dedup_assets}
            self.save_batch_dedup_settings(dedup_visual)
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
                    "planned_video_encoder": config.output.video_codec,
                    "actual_video_encoder": None,
                    "hardware_acceleration": False,
                    "encoder_fallback_reason": None,
                    "attempts": 0,
                    "error": None,
                    "selections": {
                        category: asset.model_dump(mode="json") if asset else None
                        for category, asset in planned.selections.items()
                    },
                    "overlay": planned.overlay.model_dump(mode="json") if planned.overlay else None,
                    "visual_border": (
                        planned.visual_border.model_dump(mode="json")
                        if planned.visual_border
                        else None
                    ),
                    "visual_effects": [
                        effect.model_dump(mode="json")
                        for effect in planned.visual_effects
                    ],
                    "naming": planned.naming.model_dump(mode="json") if planned.naming else None,
                }
            )
        job = {
            "id": job_id,
            "job_type": "batch_dedup" if dedup_source is not None else "render",
            "source_directory": str(source) if dedup_source is not None else None,
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
            "visual_dedup": config.visual_dedup.model_dump(mode="json"),
            "feishu_base_sync": config.output.feishu_base_sync.model_dump(mode="json"),
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

    @staticmethod
    def _dedup_config(visual: VisualDedupConfig, source: Path) -> AppConfig:
        return AppConfig.model_validate({
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "batch-dedup",
            "name": "批量去重",
            "source_root": str(source),
            "timeline": ["pool_1"],
            "sources": {
                "pool_1": {
                    "label": "源视频",
                    "mode": SourceMode.REQUIRED,
                    "directory": ".",
                    "extensions": [".mp4", ".mov", ".mkv", ".m4v", ".webm"],
                }
            },
            "benefit_overlays": {"mode": SourceMode.DISABLED, "file": ""},
            "visual_dedup": visual.model_dump(mode="json"),
            "output": {"directory": str(source.parent)},
            "batch": {"minimum_free_space_gb": 0.5},
            "randomization": {"duplicate_policy": "strict"},
        })

    def start(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if self.stopping:
                raise RuntimeError("SmartStitch 正在退出，不能启动新任务")
            job = self.get_job(job_id)
            if job["status"] != "draft":
                raise ValueError(f"只有 draft 任务可以启动，当前为 {job['status']}")
            job["status"] = "queued"
            self.database.save(job)
            cancel_event = threading.Event()
            self.cancel_events[job_id] = cancel_event
            thread = threading.Thread(
                target=self._run,
                args=(job_id,),
                daemon=True,
                name=f"stitch-job-{job_id[:6]}",
            )
            self.threads[job_id] = thread
            thread.start()
        return self.get_job(job_id)

    def active_count(self) -> int:
        return sum(
            job.get("status") not in TERMINAL_STATES | {"draft"}
            for job in self.database.list()
        )

    def shutdown(self, timeout: float = 8.0) -> None:
        """Stop accepting work and terminate child encoders before app exit."""

        with self.lock:
            self.stopping = True
            active_ids = [
                str(job["id"])
                for job in self.database.list()
                if job.get("status") not in TERMINAL_STATES | {"draft"}
            ]
        for job_id in active_ids:
            try:
                self.cancel(job_id)
            except KeyError:
                pass

        deadline = time.monotonic() + max(0.0, timeout)
        for thread in list(self.threads.values()):
            thread.join(max(0.0, deadline - time.monotonic()))

        with self.lock:
            for process in list(self.processes.values()):
                if process.poll() is None:
                    process.kill()

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
        if self.output_sync_manager is not None:
            self.output_sync_manager.enqueue_if_enabled(job)
        with self.lock:
            self.cancel_events.pop(job_id, None)
            self.threads.pop(job_id, None)

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
        from .models import Asset, PlannedVisualEffect, PlanItem

        planned = PlanItem(
            index=item_data["index"],
            selections={
                category: Asset.model_validate(asset) if asset else None
                for category, asset in item_data["selections"].items()
            },
            overlay=Asset.model_validate(item_data["overlay"]) if item_data["overlay"] else None,
            visual_border=(
                Asset.model_validate(item_data["visual_border"])
                if item_data.get("visual_border")
                else None
            ),
            visual_effects=[
                PlannedVisualEffect.model_validate(effect)
                for effect in item_data.get("visual_effects", [])
            ],
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
                    planned_video_encoder=result["planned_video_encoder"],
                    actual_video_encoder=result["actual_video_encoder"],
                    hardware_acceleration=result["hardware_acceleration"],
                    encoder_fallback_reason=result["encoder_fallback_reason"],
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
            ["product", "benefit", "talents", "restriction_date", "sequence"]
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
                    "visual_border",
                    "visual_effects",
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
                    "visual_border": (
                        item["visual_border"]["path"]
                        if item.get("visual_border")
                        else ""
                    ),
                    "visual_effects": json.dumps(
                        item.get("visual_effects", []),
                        ensure_ascii=False,
                    ),
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
                            "sequence": naming["sequence"],
                        }
                    )
                for category in categories:
                    asset = item["selections"].get(category)
                    row[category_columns[category]] = asset["path"] if asset else ""
                writer.writerow(row)
