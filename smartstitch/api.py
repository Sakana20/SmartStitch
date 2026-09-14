from __future__ import annotations

import asyncio
import json
import shutil
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import ConfigError, ConfigStore
from .jobs import JobManager, TERMINAL_STATES
from .models import (
    CloneConfigRequest,
    ConfigUpdateRequest,
    JobCreateRequest,
    PreviewRequest,
    WeightUpdateRequest,
)
from .planner import PlanError, build_plan
from .scanner import scan_config


def create_app(base_directory: Path | None = None) -> FastAPI:
    root = (base_directory or Path(__file__).resolve().parent.parent).resolve()
    config_store = ConfigStore(root / "config")
    job_manager = JobManager(config_store, root / "data")
    app = FastAPI(title="SmartStitch", version=__version__)
    app.state.root = root
    app.state.config_store = config_store
    app.state.job_manager = job_manager

    @app.get("/api/v1/system/health")
    def health() -> dict[str, object]:
        return {
            "ok": True,
            "version": __version__,
            "ffmpeg": shutil.which("ffmpeg"),
            "ffprobe": shutil.which("ffprobe"),
        }

    @app.get("/api/v1/configs")
    def list_configs() -> list[dict[str, object]]:
        return config_store.list()

    @app.get("/api/v1/configs/{config_id}")
    def get_config(config_id: str) -> dict[str, object]:
        try:
            config = config_store.load(config_id)
            return {"config": config.model_dump(mode="json"), "yaml_text": config_store.raw(config_id)}
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.put("/api/v1/configs/{config_id}")
    def update_config(config_id: str, request: ConfigUpdateRequest) -> dict[str, object]:
        try:
            config = config_store.save_text(config_id, request)
            return {"ok": True, "config": config.model_dump(mode="json")}
        except ConfigError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.post("/api/v1/configs/{config_id}/clone")
    def clone_config(config_id: str, request: CloneConfigRequest) -> dict[str, object]:
        try:
            config = config_store.clone(config_id, request.new_id, request.new_name)
            return {"ok": True, "config": config.model_dump(mode="json")}
        except (ConfigError, FileNotFoundError) as exc:
            raise HTTPException(422, str(exc)) from exc

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

    @app.post("/api/v1/jobs")
    def create_job(request: JobCreateRequest) -> dict[str, object]:
        try:
            return job_manager.create(request)
        except (ConfigError, FileNotFoundError, PlanError, ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/v1/jobs")
    def list_jobs() -> list[dict[str, object]]:
        return job_manager.list_jobs()

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
