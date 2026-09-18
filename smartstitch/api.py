from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .audio_preview import AudioPreviewError, create_audio_preview
from .config import ConfigError, ConfigStore
from .database import SQLiteStore
from .jobs import JobManager, TERMINAL_STATES
from .library import LibraryConflictError, LibraryError, LibraryService, pick_directory
from .media import DisconnectSafeFileResponse
from .models import (
    AddBenefitRequest,
    AddPoolRequest,
    CloneConfigRequest,
    ConfigUpdateRequest,
    CreateConfigRequest,
    CreateLibraryRequest,
    DeletePoolRequest,
    JobCreateRequest,
    LibraryPreflightRequest,
    LoudnessPreviewRequest,
    PreviewRequest,
    ReplaceOverlayImageRequest,
    ReorderTimelineRequest,
    StructuredConfigUpdateRequest,
    TimelineAnalyzeRequest,
    TimelineDecisionRequest,
    TimelineSourceDirectoryRequest,
    TimelineSliceRequest,
    UpdatePoolRequest,
    WeightUpdateRequest,
)
from .planner import PlanError, build_plan
from .scanner import probe_config_audio, scan_config
from .slice_jobs import SliceJobManager
from .slicer import SliceConflictError, SliceError, TimelineSlicer
from .timeline import TimelineAnalyzer, TimelineError, list_source_videos


DEFAULT_SHARED_CONFIG_DIRECTORY = Path("/Volumes/home/Smartstitch")
DEFAULT_SHARED_CONFIG_ALTERNATE_PARENT = Path("/Volumes/homes")
CONFIG_DIRECTORY_ENV = "SMARTSTITCH_CONFIG_DIRECTORY"


class SharedConfigUnavailableError(RuntimeError):
    pass


def alternate_shared_config_directories(parent: Path) -> list[Path]:
    if not parent.is_dir():
        return []
    return sorted(
        (
            candidate
            for candidate in parent.glob("*/Smartstitch")
            if candidate.is_dir()
        ),
        key=lambda candidate: str(candidate).casefold(),
    )


def resolve_config_directory(
    application_root: Path,
    *,
    allow_shared_default: bool,
    shared_directory: Path = DEFAULT_SHARED_CONFIG_DIRECTORY,
    alternate_shared_parent: Path = DEFAULT_SHARED_CONFIG_ALTERNATE_PARENT,
) -> Path:
    override = os.environ.get(CONFIG_DIRECTORY_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    if allow_shared_default and shared_directory.is_dir():
        return shared_directory.resolve()
    if allow_shared_default:
        alternatives = alternate_shared_config_directories(
            alternate_shared_parent
        )
        if len(alternatives) == 1:
            return alternatives[0].resolve()
        raise SharedConfigUnavailableError(
            "未连接 NAS：找不到 /Volumes/home/Smartstitch。"
            "请先在 Finder 中连接 NAS 并确认共享目录已挂载，然后重新启动 SmartStitch。"
        )
    return application_root / "config"


def canonical_config_directory(directory: Path) -> Path:
    try:
        relative = directory.relative_to(DEFAULT_SHARED_CONFIG_ALTERNATE_PARENT)
    except ValueError:
        return directory
    if len(relative.parts) == 2 and relative.parts[1] == "Smartstitch":
        return DEFAULT_SHARED_CONFIG_DIRECTORY
    return directory


def create_app(
    base_directory: Path | None = None,
    config_directory: Path | None = None,
) -> FastAPI:
    root = (base_directory or Path(__file__).resolve().parent.parent).resolve()
    resolved_config_directory = (
        config_directory.expanduser().resolve()
        if config_directory is not None
        else resolve_config_directory(
            root,
            allow_shared_default=base_directory is None,
        )
    )
    config_store = ConfigStore(
        resolved_config_directory,
        canonical_directory=canonical_config_directory(
            resolved_config_directory
        ),
    )
    library_service = LibraryService(config_store)
    database_store = SQLiteStore(root / "data" / "smartstitch.db")
    job_manager = JobManager(config_store, root / "data", database_store)
    timeline_analyzer = TimelineAnalyzer(root / "data" / "timelines")
    timeline_slicer = TimelineSlicer(timeline_analyzer, library_service)
    slice_job_manager = SliceJobManager(timeline_slicer, database_store)
    app = FastAPI(title="SmartStitch", version=__version__)
    app.state.root = root
    app.state.config_store = config_store
    app.state.library_service = library_service
    app.state.job_manager = job_manager
    app.state.database_store = database_store
    app.state.timeline_analyzer = timeline_analyzer
    app.state.timeline_slicer = timeline_slicer
    app.state.slice_job_manager = slice_job_manager

    @app.get("/api/v1/system/health")
    def health() -> dict[str, object]:
        return {
            "ok": True,
            "version": __version__,
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
            "config_directory": str(config_store.directory),
            "canonical_config_directory": str(
                config_store.canonical_directory
            ),
            "shared_config": config_store.directory != root / "config",
            "config_path_mapped": config_store.has_path_alias,
        }

    @app.post("/api/v1/system/directory-picker")
    def directory_picker() -> dict[str, object]:
        try:
            return pick_directory()
        except (LibraryError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/libraries/preflight")
    def preflight_library(request: LibraryPreflightRequest) -> dict[str, object]:
        try:
            return library_service.preflight(request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/libraries")
    def create_library(request: CreateLibraryRequest) -> dict[str, object]:
        try:
            return library_service.create(request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/libraries/by-config/{config_id}")
    def get_library(config_id: str) -> dict[str, object]:
        try:
            return library_service.inspect_by_config(config_id)
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/v1/libraries/by-config/{config_id}/slice-targets")
    def get_slice_targets(config_id: str) -> dict[str, object]:
        try:
            return {"targets": library_service.slice_targets(config_id)}
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        except (LibraryError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/sources/{category}/open-directory")
    def open_source_directory(config_id: str, category: str) -> dict[str, object]:
        try:
            return library_service.open_source_directory(config_id, category)
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc
        except (LibraryError, OSError, subprocess.SubprocessError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/benefits")
    def add_benefit(config_id: str, request: AddBenefitRequest) -> dict[str, object]:
        try:
            return library_service.add_benefit(config_id, request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/pools")
    def add_pool(config_id: str, request: AddPoolRequest) -> dict[str, object]:
        try:
            return library_service.add_pool(config_id, request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.patch("/api/v1/configs/{config_id}/pools/{pool_id}")
    def update_pool(
        config_id: str, pool_id: str, request: UpdatePoolRequest
    ) -> dict[str, object]:
        try:
            return library_service.update_pool(config_id, pool_id, request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/v1/configs/{config_id}/pools/{pool_id}")
    def delete_pool(
        config_id: str, pool_id: str, request: DeletePoolRequest
    ) -> dict[str, object]:
        try:
            return library_service.delete_pool(config_id, pool_id, request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/v1/configs/{config_id}/timeline")
    def reorder_timeline(
        config_id: str, request: ReorderTimelineRequest
    ) -> dict[str, object]:
        try:
            return library_service.reorder_timeline(config_id, request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/overlay-image")
    def replace_overlay_image(
        config_id: str, request: ReplaceOverlayImageRequest
    ) -> dict[str, object]:
        try:
            return library_service.replace_overlay_image(config_id, request)
        except LibraryConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (LibraryError, ConfigError, FileNotFoundError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/configs")
    def list_configs() -> list[dict[str, object]]:
        return config_store.list()

    @app.post("/api/v1/configs")
    def create_config(request: CreateConfigRequest) -> dict[str, object]:
        try:
            config = config_store.create(request.new_id, request.new_name.strip())
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config.id),
            }
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/configs/{config_id}")
    def get_config(config_id: str) -> dict[str, object]:
        try:
            config = config_store.load(config_id)
            return {
                "config": config.model_dump(mode="json"),
                "yaml_text": config_store.raw(config_id),
                "content_hash": config_store.content_hash(config_id),
            }
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.put("/api/v1/configs/{config_id}")
    def update_config(config_id: str, request: ConfigUpdateRequest) -> dict[str, object]:
        try:
            config = config_store.save_text(config_id, request)
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config_id),
            }
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/v1/configs/{config_id}/structured")
    def update_structured_config(
        config_id: str, request: StructuredConfigUpdateRequest
    ) -> dict[str, object]:
        try:
            config = config_store.save_config(config_id, request.config)
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config_id),
            }
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/clone")
    def clone_config(config_id: str, request: CloneConfigRequest) -> dict[str, object]:
        try:
            config = config_store.clone(config_id, request.new_id, request.new_name)
            return {
                "ok": True,
                "config": config.model_dump(mode="json"),
                "content_hash": config_store.content_hash(config.id),
            }
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.delete("/api/v1/configs/{config_id}")
    def delete_config(config_id: str) -> dict[str, object]:
        try:
            backup = config_store.delete(config_id)
            return {"ok": True, "backup": str(backup)}
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/weights")
    def update_weights(config_id: str, request: WeightUpdateRequest) -> dict[str, object]:
        try:
            config = config_store.update_weights(config_id, request.items)
            return {"ok": True, "config": config.model_dump(mode="json")}
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/scan")
    def scan(config_id: str) -> dict[str, object]:
        try:
            result = scan_config(config_store.load(config_id))
            return {**result.model_dump(mode="json"), "ok": result.ok}
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/plans/preview")
    def preview(request: PreviewRequest) -> dict[str, object]:
        try:
            config = config_store.load(request.config_id)
            if request.output_directory:
                config.output.directory = request.output_directory
            result = build_plan(config, scan_config(config), request.count, request.seed)
            return result.model_dump(mode="json")
        except (ConfigError, FileNotFoundError, PlanError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    def audio_preview_response(request: LoudnessPreviewRequest) -> FileResponse:
        try:
            config = config_store.load(request.config_id)
            requested_path = Path(request.asset_path).expanduser().resolve()
            probe_config_audio(config, requested_path)
            preview_path = create_audio_preview(
                request, requested_path, root / "data" / "previews"
            )
            return FileResponse(
                preview_path,
                media_type="audio/mp4",
                headers={"Cache-Control": "private, max-age=3600"},
            )
        except (ConfigError, FileNotFoundError, ValueError, AudioPreviewError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/audio/preview")
    def audio_preview(request: LoudnessPreviewRequest) -> FileResponse:
        return audio_preview_response(request)

    @app.get("/api/v1/audio/preview")
    def audio_preview_for_player(
        config_id: str,
        asset_path: str,
        normalized: bool = True,
        target_lufs: float = -14,
        loudness_range_lu: float = 7,
        true_peak_dbtp: float = -1.5,
        duration_seconds: float = 12,
    ) -> FileResponse:
        try:
            request = LoudnessPreviewRequest(
                config_id=config_id,
                asset_path=asset_path,
                normalized=normalized,
                target_lufs=target_lufs,
                loudness_range_lu=loudness_range_lu,
                true_peak_dbtp=true_peak_dbtp,
                duration_seconds=duration_seconds,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        return audio_preview_response(request)

    @app.post("/api/v1/timeline/analyze")
    def analyze_timeline(request: TimelineAnalyzeRequest) -> dict[str, object]:
        try:
            return timeline_analyzer.analyze(
                request.source_path,
                request.scene_threshold,
                request.silence_duration_seconds,
            )
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/timeline/sources")
    def timeline_sources(
        request: TimelineSourceDirectoryRequest,
    ) -> dict[str, object]:
        try:
            return list_source_videos(request.source_directory)
        except (TimelineError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/timeline/media/{media_token}")
    def timeline_media(media_token: str) -> DisconnectSafeFileResponse:
        try:
            return DisconnectSafeFileResponse(timeline_analyzer.media_path(media_token))
        except TimelineError as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/v1/timeline/waveforms/{analysis_id}")
    def timeline_waveform(
        analysis_id: str,
        start_frame: int = Query(ge=0),
        end_frame: int = Query(gt=0),
        width_px: int = Query(ge=1, le=4096),
    ) -> dict[str, object]:
        try:
            return timeline_analyzer.waveform(
                analysis_id, start_frame, end_frame, width_px
            )
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.put("/api/v1/timeline/decisions")
    def save_timeline_decision(request: TimelineDecisionRequest) -> dict[str, object]:
        try:
            return timeline_analyzer.save_decision(request.analysis_id, request.frame_indexes)
        except (TimelineError, OSError, json.JSONDecodeError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/timeline/slices")
    def export_timeline_slices(request: TimelineSliceRequest) -> dict[str, object]:
        try:
            return timeline_slicer.export(request)
        except SliceConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (
            SliceError,
            TimelineError,
            LibraryError,
            ConfigError,
            ValueError,
            OSError,
        ) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/timeline/slice-jobs", status_code=202)
    def create_timeline_slice_job(
        request: TimelineSliceRequest,
    ) -> dict[str, object]:
        try:
            return slice_job_manager.create(request)
        except SliceConflictError as exc:
            raise HTTPException(409, str(exc)) from exc
        except (
            SliceError,
            TimelineError,
            LibraryError,
            ConfigError,
            ValueError,
            OSError,
        ) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/timeline/slice-jobs")
    def list_timeline_slice_jobs(
        limit: int = Query(default=20, ge=1, le=200),
        status: str | None = None,
    ) -> list[dict[str, object]]:
        return slice_job_manager.list_jobs(status=status)[:limit]

    @app.get("/api/v1/timeline/slice-jobs/events")
    async def timeline_slice_job_events() -> StreamingResponse:
        async def stream():
            previous = ""
            while True:
                jobs = slice_job_manager.list_jobs()[:20]
                payload = json.dumps(jobs, ensure_ascii=False)
                if payload != previous:
                    yield f"event: slice_jobs_update\ndata: {payload}\n\n"
                    previous = payload
                await asyncio.sleep(0.7)

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.get("/api/v1/timeline/slice-jobs/{job_id}")
    def get_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            return slice_job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc

    @app.post("/api/v1/timeline/slice-jobs/{job_id}/cancel")
    def cancel_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            return slice_job_manager.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc

    @app.post("/api/v1/timeline/slice-jobs/{job_id}/retry-failed")
    def retry_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            return slice_job_manager.retry(job_id)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc
        except (ValueError, LibraryError, ConfigError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/timeline/slice-jobs/{job_id}/resume")
    def resume_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            return slice_job_manager.retry(job_id, interrupted_only=True)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc
        except (ValueError, LibraryError, ConfigError, OSError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.delete("/api/v1/timeline/slice-jobs/{job_id}")
    def delete_timeline_slice_job(job_id: str) -> dict[str, object]:
        try:
            slice_job_manager.delete(job_id)
            return {"ok": True, "job_id": job_id}
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/timeline/slice-jobs/{job_id}/manifest")
    def timeline_slice_job_manifest(job_id: str) -> FileResponse:
        try:
            job = slice_job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "切片任务不存在") from exc
        path = Path(str(job["manifest_path"]))
        if not path.is_file():
            raise HTTPException(404, "切片清单不存在")
        return FileResponse(path)

    @app.post("/api/v1/jobs")
    def create_job(request: JobCreateRequest) -> dict[str, object]:
        try:
            return job_manager.create(request)
        except (ConfigError, FileNotFoundError, PlanError, ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/jobs")
    def list_jobs() -> list[dict[str, object]]:
        return job_manager.list_jobs()

    @app.delete("/api/v1/jobs")
    def delete_all_jobs() -> dict[str, object]:
        try:
            return {"ok": True, "deleted_count": job_manager.delete_all()}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        try:
            return job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.post("/api/v1/jobs/{job_id}/start")
    def start_job(job_id: str) -> dict[str, object]:
        try:
            return job_manager.start(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> dict[str, object]:
        try:
            return job_manager.cancel(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

    @app.delete("/api/v1/jobs/{job_id}")
    def delete_job(job_id: str) -> dict[str, object]:
        try:
            job_manager.delete(job_id)
            return {"ok": True, "job_id": job_id}
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/v1/jobs/{job_id}/manifest")
    def manifest(job_id: str) -> FileResponse:
        try:
            job = job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc
        return FileResponse(Path(job["output_directory"]) / "manifest.json")

    @app.get("/api/v1/jobs/{job_id}/events")
    async def events(job_id: str) -> StreamingResponse:
        try:
            job_manager.get_job(job_id)
        except KeyError as exc:
            raise HTTPException(404, "任务不存在") from exc

        async def stream():
            previous = ""
            while True:
                job = job_manager.get_job(job_id)
                payload = json.dumps(job, ensure_ascii=False)
                if payload != previous:
                    yield f"event: job_update\ndata: {payload}\n\n"
                    previous = payload
                if job["status"] in TERMINAL_STATES:
                    break
                await asyncio.sleep(0.7)

        return StreamingResponse(stream(), media_type="text/event-stream")

    static_directory = root / "frontend"
    if static_directory.exists():
        app.mount("/", StaticFiles(directory=static_directory, html=True), name="frontend")
    return app


app = create_app()
