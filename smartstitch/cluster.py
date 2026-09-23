"""App-to-app render coordination; SQLite files remain local to each Mac."""

from __future__ import annotations

import ipaddress
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, HTTPRedirectHandler, ProxyHandler

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request as APIRequest
from pydantic import BaseModel, field_validator

from . import __version__
from .models import AppConfig, Asset, JobCreateRequest, PlannedVisualEffect, PlanItem
from .renderer import render_item
from .video_upscale import MODEL_CONFIGS, model_config, model_status, probe_video, process_segment


DEFAULT_WORKER_PORT = 8767
POLL_SECONDS = 1.5
LOST_SECONDS = 30
LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _safe_write(path: Path, value: dict[str, Any], *, secret: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temporary, flags, 0o600 if secret else 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        os.replace(temporary, path)
        if secret:
            path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _load_json(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _canonicalize(value: Any, actual_root: Path, canonical_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _canonicalize(part, actual_root, canonical_root) for key, part in value.items()}
    if isinstance(value, list):
        return [_canonicalize(part, actual_root, canonical_root) for part in value]
    if isinstance(value, str) and Path(value).is_absolute():
        try:
            relative = Path(value).resolve().relative_to(actual_root.resolve())
        except ValueError:
            return value
        return str(canonical_root / relative)
    return value


def _localize(value: Any, canonical_root: Path, actual_root: Path) -> Any:
    if isinstance(value, dict):
        return {key: _localize(part, canonical_root, actual_root) for key, part in value.items()}
    if isinstance(value, list):
        return [_localize(part, canonical_root, actual_root) for part in value]
    if isinstance(value, str) and (value == str(canonical_root) or value.startswith(str(canonical_root) + os.sep)):
        return str(actual_root / Path(value).relative_to(canonical_root))
    return value


def _planned(item: dict[str, Any]) -> PlanItem:
    return PlanItem(
        index=item["index"],
        selections={
            category: Asset.model_validate(asset) if asset else None
            for category, asset in item["selections"].items()
        },
        overlay=Asset.model_validate(item["overlay"]) if item.get("overlay") else None,
        visual_border=Asset.model_validate(item["visual_border"])
        if item.get("visual_border") else None,
        visual_effects=[PlannedVisualEffect.model_validate(effect) for effect in item.get("visual_effects", [])],
        output_name=item["output_name"],
        estimated_duration=item["estimated_duration"],
    )


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        raise URLError("不接受节点重定向")


_opener = build_opener(NoRedirect, ProxyHandler({}))


def _node_url(value: str) -> str:
    value = value.strip().rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "http" or not parsed.hostname or not parsed.port or parsed.path or parsed.query:
        raise ValueError("节点地址需为 http://局域网地址:端口")
    try:
        addresses = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, parsed.port, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise ValueError("无法解析节点地址") from exc
    if not addresses or not all(ipaddress.ip_address(address).is_private for address in addresses):
        raise ValueError("节点必须位于私有局域网")
    return value


def _request(url: str, token: str, *, method: str = "GET", payload: dict[str, Any] | None = None) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, method=method, headers=headers)
    with _opener.open(request, timeout=3) as response:
        return json.load(response)


class NodeRegistration(BaseModel):
    url: str
    token: str = ""

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        value = value.strip()
        if value and len(value) < 16:
            raise ValueError("请填写工作机启用后显示的随机令牌（通常为 32 位），不要填写集群控制页面的解锁密码")
        return value


class ClusterWorker:
    def __init__(self, data_directory: Path, config_store: Any, sqlite_store: Any, user_profiles: Any):
        self.data_directory = data_directory
        self.config_store = config_store
        self.store = sqlite_store
        self.user_profiles = user_profiles
        self.local_upscale_manager: Any | None = None
        self.settings_path = data_directory / "cluster-worker.json"
        self.settings = _load_json(self.settings_path, {"enabled": True, "token": uuid.uuid4().hex,
                                                         "token_required": False, "node_id": uuid.uuid4().hex})
        self.settings.setdefault("token_required", False)
        self.startup_error: str | None = None
        self.lock = threading.RLock()
        self.events: dict[str, threading.Event] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.processes: dict[str, subprocess.Popen[str]] = {}
        self.server: uvicorn.Server | None = None
        self.server_thread: threading.Thread | None = None
        self.listener: socket.socket | None = None
        self.advertiser: subprocess.Popen[str] | None = None
        with self.store.lock, self.store.connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS cluster_attempts (id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            rows = connection.execute("SELECT id, payload FROM cluster_attempts").fetchall()
            for attempt_id, raw in rows:
                value = json.loads(raw)
                if value["status"] in {"queued", "running"}:
                    value.update(status="interrupted", error="工作机已退出", updated_at=_now())
                    connection.execute("UPDATE cluster_attempts SET payload=? WHERE id=?", (json.dumps(value, ensure_ascii=False), attempt_id))
        _safe_write(self.settings_path, self.settings, secret=True)

    def _save(self, value: dict[str, Any]) -> None:
        with self.store.lock, self.store.connection() as connection:
            connection.execute(
                "INSERT INTO cluster_attempts(id,payload) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload",
                (value["attempt_id"], json.dumps(value, ensure_ascii=False)),
            )

    def get(self, attempt_id: str) -> dict[str, Any] | None:
        with self.store.lock, self.store.connection() as connection:
            row = connection.execute("SELECT payload FROM cluster_attempts WHERE id=?", (attempt_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_attempts(self, limit: int = 100) -> list[dict[str, Any]]:
        with self.store.lock, self.store.connection() as connection:
            rows = connection.execute(
                "SELECT payload FROM cluster_attempts ORDER BY rowid DESC LIMIT ?", (limit,)
            ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def _update(self, attempt_id: str, **updates: Any) -> None:
        with self.lock:
            value = self.get(attempt_id)
            if value is None:
                return
            value.update(updates, updated_at=_now())
            self._save(value)

    def status(self) -> dict[str, Any]:
        with self.lock:
            active = sum(thread.is_alive() for thread in self.threads.values())
            if self.local_upscale_manager is not None:
                active += self.local_upscale_manager.active_count()
            profile = self.user_profiles.get()["user"]
            return {
                "node_id": self.settings["node_id"], "name": socket.gethostname(),
                "display_name": profile["display_name"] if profile else "",
                "version": __version__, "active": active, "capacity": 1,
                "canonical_root": str(self.config_store.canonical_directory),
                "video_upscale": {**model_status(self.data_directory), "protocol": 2,
                                  "models": {name: model_status(self.data_directory, name) for name in MODEL_CONFIGS}},
            }

    def _app(self) -> FastAPI:
        app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

        def authorize(request: APIRequest) -> None:
            if self.settings["token_required"] and request.headers.get("Authorization", "") != f"Bearer {self.settings['token']}":
                raise HTTPException(401, "工作机令牌无效")

        @app.get("/hello")
        def hello(request: APIRequest) -> dict[str, Any]:
            authorize(request)
            return self.status()

        @app.get("/attempts/{attempt_id}")
        def get_attempt(attempt_id: str, request: APIRequest) -> dict[str, Any]:
            authorize(request)
            value = self.get(attempt_id)
            if value is None:
                raise HTTPException(404, "执行记录不存在")
            return {key: value.get(key) for key in (
                "attempt_id", "status", "progress", "processed_frames", "error", "result", "stage_path", "updated_at"
            )}

        @app.post("/attempts")
        def create_attempt(payload: dict[str, Any], request: APIRequest) -> dict[str, Any]:
            authorize(request)
            try:
                return self.submit(payload)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from exc

        @app.post("/upscale-attempts")
        def create_upscale_attempt(payload: dict[str, Any], request: APIRequest) -> dict[str, Any]:
            authorize(request)
            try:
                return self.submit_upscale(payload)
            except (ValueError, KeyError, OSError) as exc:
                raise HTTPException(422, str(exc)) from exc

        @app.post("/attempts/{attempt_id}/cancel")
        def cancel_attempt(attempt_id: str, request: APIRequest) -> dict[str, Any]:
            authorize(request)
            self.cancel(attempt_id)
            return {"ok": True}

        return app

    def start(self) -> dict[str, Any]:
        with self.lock:
            if self.server_thread and self.server_thread.is_alive():
                return self.connection_info()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("0.0.0.0", int(os.environ.get("SMARTSTITCH_CLUSTER_PORT", DEFAULT_WORKER_PORT))))
            except OSError:
                listener.close()
                raise
            listener.listen(128)
            self.listener = listener
            self.server = uvicorn.Server(uvicorn.Config(self._app(), log_config=None, access_log=False))
            self.server_thread = threading.Thread(target=self.server.run, kwargs={"sockets": [listener]}, daemon=True, name="cluster-worker-http")
            self.server_thread.start()
            self.settings["enabled"] = True
            _safe_write(self.settings_path, self.settings, secret=True)
        deadline = time.monotonic() + 5
        while not self.server.started and time.monotonic() < deadline:
            time.sleep(0.02)
        if not self.server.started:
            self.stop(disable=True)
            raise RuntimeError("工作机局域网接口启动失败")
        self.startup_error = None
        self._advertise()
        return self.connection_info()

    def _advertise(self) -> None:
        try:
            if self.listener and shutil.which("dns-sd"):
                display_name = self.status()["display_name"]
                prefix = display_name.encode("utf-8")[:36].decode("utf-8", "ignore").strip()
                service_name = f"{prefix} · SmartStitch-{self.settings['node_id'][:8]}" if prefix else f"SmartStitch-{self.settings['node_id'][:8]}"
                self.advertiser = subprocess.Popen(
                    ["dns-sd", "-R", service_name,
                     "_smartstitch._tcp", "local.", str(self.listener.getsockname()[1]),
                     f"node_id={self.settings['node_id']}", f"version={__version__}"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True,
                )
        except OSError:
            self.advertiser = None

    def refresh_advertisement(self) -> None:
        with self.lock:
            if not self.listener or not self.server or not self.server.started:
                return
            if self.advertiser:
                self.advertiser.terminate()
                try:
                    self.advertiser.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.advertiser.kill()
                self.advertiser = None
            self._advertise()

    def connection_info(self) -> dict[str, Any]:
        port = self.listener.getsockname()[1] if self.listener else None
        return {**self.status(), "enabled": bool(self.server and self.server.started),
                "port": port, "token": self.settings["token"],
                "token_required": self.settings["token_required"], "startup_error": self.startup_error}

    def set_token_required(self, required: bool) -> None:
        with self.lock:
            self.settings["token_required"] = required
            _safe_write(self.settings_path, self.settings, secret=True)

    def stop(self, *, disable: bool = False) -> None:
        with self.lock:
            if disable:
                self.settings["enabled"] = False
                _safe_write(self.settings_path, self.settings, secret=True)
            for event in self.events.values():
                event.set()
            if self.advertiser:
                self.advertiser.terminate()
                try:
                    self.advertiser.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.advertiser.kill()
                self.advertiser = None
            if self.server:
                self.server.should_exit = True
            for process in self.processes.values():
                if process.poll() is None:
                    process.terminate()
        if self.server_thread:
            self.server_thread.join(timeout=3)
        for thread in list(self.threads.values()):
            thread.join(timeout=5)
        if self.listener:
            self.listener.close()
        self.server = None
        self.server_thread = None
        self.listener = None

    def submit(self, payload: dict[str, Any]) -> dict[str, Any]:
        attempt_id = str(payload.get("attempt_id", ""))
        if not attempt_id or not all(character in "0123456789abcdef" for character in attempt_id):
            raise ValueError("执行标识无效")
        with self.lock:
            existing = self.get(attempt_id)
            if existing:
                return {"attempt_id": attempt_id, "status": existing["status"]}
            if any(thread.is_alive() for thread in self.threads.values()) or (self.local_upscale_manager and self.local_upscale_manager.active_count()):
                raise ValueError("工作机正在渲染，请稍后派发")
            canonical = Path(str(payload["canonical_root"]))
            if canonical != self.config_store.canonical_directory:
                raise ValueError("工作机与主控未挂载同一个 SmartStitch 共享目录")
            relative = Path(str(payload["batch_relative_path"]))
            if relative.is_absolute() or ".." in relative.parts:
                raise ValueError("批次目录无效")
            stage_name = str(payload["stage_name"])
            if Path(stage_name).name != stage_name or not stage_name.endswith(".mp4"):
                raise ValueError("暂存文件名无效")
            stage = self.config_store.directory / relative / stage_name
            localized = _localize(payload["config"], canonical, self.config_store.directory)
            item = _localize(payload["item"], canonical, self.config_store.directory)
            config = AppConfig.model_validate(localized)
            planned = _planned(item)
            for asset in [*planned.selections.values(), planned.overlay, planned.visual_border,
                          *(effect.asset for effect in planned.visual_effects)]:
                if asset:
                    asset_path = Path(asset.path).resolve()
                    try:
                        asset_path.relative_to(self.config_store.directory.resolve())
                    except ValueError as exc:
                        raise ValueError(f"集群素材不在 NAS 共享目录中：{asset.path}") from exc
                    if not asset_path.is_file():
                        raise ValueError(f"素材不可访问：{asset.path}")
                    stat = asset_path.stat()
                    if asset.size_bytes is not None and stat.st_size != asset.size_bytes:
                        raise ValueError(f"素材已在计划后改变大小：{asset.path}")
                    if asset.modified_at is not None and abs(stat.st_mtime - asset.modified_at) > 1:
                        raise ValueError(f"素材已在计划后修改：{asset.path}")
            value = {"attempt_id": attempt_id, "batch_id": payload["batch_id"],
                     "item_index": item["index"], "config_name": config.name,
                     "output_name": planned.output_name, "status": "queued", "progress": 0.0,
                     "error": None, "result": None, "stage_path": str(stage),
                     "created_at": _now(), "updated_at": _now()}
            self._save(value)
            event = threading.Event()
            self.events[attempt_id] = event
            thread = threading.Thread(target=self._run, args=(attempt_id, config, planned, stage, event), daemon=True)
            self.threads[attempt_id] = thread
            thread.start()
            return {"attempt_id": attempt_id, "status": "queued"}

    def submit_upscale(self, payload: dict[str, Any]) -> dict[str, Any]:
        required = {"kind", "protocol", "attempt_id", "job_id", "canonical_root", "source_relative",
                    "source_sha256", "stage_relative", "start", "end", "frames", "width", "height",
                    "fps", "model", "model_sha256"}
        if set(payload) != required or payload["kind"] != "video_upscale" or payload["protocol"] != 2:
            raise ValueError("超分任务协议不匹配")
        attempt_id = payload["attempt_id"]
        if not isinstance(attempt_id, str) or len(attempt_id) != 32 or any(c not in "0123456789abcdef" for c in attempt_id):
            raise ValueError("执行标识无效")
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        with self.lock:
            existing = self.get(attempt_id)
            if existing:
                if existing.get("request_digest") != digest:
                    raise ValueError("执行标识已被不同的请求使用")
                return {"attempt_id": attempt_id, "status": existing["status"]}
            if any(thread.is_alive() for thread in self.threads.values()) or (self.local_upscale_manager and self.local_upscale_manager.active_count()):
                raise ValueError("工作机正在渲染，请稍后派发")
            if payload["canonical_root"] != str(self.config_store.canonical_directory):
                raise ValueError("共享目录不匹配")
            model_name = str(payload["model"])
            if payload["model_sha256"] != model_config(model_name)["sha256"] or not model_status(self.data_directory, model_name)["available"]:
                raise ValueError("工作机超分模型不可用或版本不匹配")
            root = self.config_store.directory.resolve()
            source_relative = Path(str(payload["source_relative"]))
            stage_relative = Path(str(payload["stage_relative"]))
            if (source_relative.is_absolute() or ".." in source_relative.parts
                    or stage_relative.is_absolute() or ".." in stage_relative.parts
                    or len(stage_relative.parts) != 3
                    or stage_relative.parts[:2] != (".video-upscale", str(payload["job_id"]))):
                raise ValueError("超分共享路径无效")
            staged_source = (len(source_relative.parts) == 3
                             and source_relative.parts == (".video-upscale", str(payload["job_id"]), "source.mp4"))
            nas_source = (len(source_relative.parts) >= 3 and source_relative.parts[:2] == ("超分", "原素材"))
            if not staged_source and not nas_source:
                raise ValueError("超分输入必须位于共享原素材目录")
            source = (root / source_relative).resolve()
            stage = (root / stage_relative).resolve()
            if not source.is_relative_to(root) or not stage.is_relative_to(root):
                raise ValueError("超分路径越过共享目录")
            if not source.is_file() or stage.exists() or stage.name != f"{attempt_id}.mp4":
                raise ValueError("超分输入或暂存文件名无效")
            info = probe_video(source)
            if any(info[key] != payload[key] for key in ("frames", "width", "height", "fps")):
                raise ValueError("超分输入视频参数不一致")
            from .video_upscale import _sha256
            if _sha256(source) != payload["source_sha256"]:
                raise ValueError("超分输入校验失败")
            start, end = payload["start"], payload["end"]
            if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start < end <= info["frames"]:
                raise ValueError("超分帧范围无效")
            value = {"attempt_id": attempt_id, "kind": "video_upscale", "request_digest": digest,
                     "batch_id": payload["job_id"], "output_name": source.name,
                     "start_frame": start, "end_frame": end, "status": "queued", "progress": 0.0,
                     "processed_frames": 0, "total_frames": end-start, "error": None, "result": None,
                     "stage_path": str(stage), "created_at": _now(), "updated_at": _now()}
            self._save(value)
            event = threading.Event()
            self.events[attempt_id] = event
            thread = threading.Thread(target=self._run_upscale, args=(attempt_id, source, stage, info, start, end, event, model_name), daemon=True)
            self.threads[attempt_id] = thread
            thread.start()
            return {"attempt_id": attempt_id, "status": "queued"}

    def _run_upscale(self, attempt_id: str, source: Path, stage: Path, info: dict[str, Any],
                     start: int, end: int, event: threading.Event, model_name: str) -> None:
        upload = stage.with_name(stage.name + ".upload")
        try:
            self._update(attempt_id, status="running")
            with tempfile.TemporaryDirectory(prefix="smartstitch-upscale-worker-") as directory:
                local = Path(directory) / "segment.mp4"
                process_segment(source, local, info, start, end, self.data_directory, event,
                                progress=lambda count: self._update(attempt_id, processed_frames=count,
                                                                    progress=count/(end-start)),
                                process_callback=lambda process: self._track_process(attempt_id, process),
                                model_name=model_name)
                if event.is_set():
                    raise RuntimeError("任务已取消")
                verified = probe_video(local)
                if (verified["frames"] != end-start or verified["width"] != info["width"]
                        or verified["height"] != info["height"] or verified["fps"] != info["fps"]
                        or verified["has_audio"]):
                    raise RuntimeError("超分片段校验失败")
                stage.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local, upload)
                if event.is_set() or upload.stat().st_size != local.stat().st_size:
                    raise RuntimeError("超分片段上传未完成")
                os.replace(upload, stage)
            self._update(attempt_id, status="succeeded", progress=1.0, result={"frames": end-start,
                         "size_bytes": stage.stat().st_size, "sha256": _file_sha256(stage)})
        except Exception as exc:
            self._update(attempt_id, status="cancelled" if event.is_set() else "failed", error=str(exc))
        finally:
            upload.unlink(missing_ok=True)
            with self.lock:
                self.events.pop(attempt_id, None)
                self.threads.pop(attempt_id, None)

    def _run(self, attempt_id: str, config: AppConfig, planned: PlanItem, stage: Path, event: threading.Event) -> None:
        upload = stage.with_name(f"{stage.name}.upload")
        try:
            self._update(attempt_id, status="running")
            with tempfile.TemporaryDirectory(prefix="smartstitch-cluster-") as directory:
                local_output = Path(directory) / stage.name
                result = render_item(config, planned, local_output, event,
                                     on_progress=lambda progress: self._update(attempt_id, progress=progress),
                                     process_callback=lambda process: self._track_process(attempt_id, process))
                if event.is_set():
                    raise RuntimeError("任务已取消")
                stage.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(local_output, upload)
                if upload.stat().st_size != local_output.stat().st_size or event.is_set():
                    raise RuntimeError("暂存文件复制未完成")
                os.replace(upload, stage)
                result["output_path"] = str(stage)
            self._update(attempt_id, status="succeeded", progress=1.0, result=result)
        except Exception as exc:
            self._update(attempt_id, status="cancelled" if event.is_set() else "failed", error=str(exc))
        finally:
            upload.unlink(missing_ok=True)
            with self.lock:
                self.events.pop(attempt_id, None)
                self.threads.pop(attempt_id, None)

    def cancel(self, attempt_id: str) -> None:
        event = self.events.get(attempt_id)
        if event:
            event.set()
        process = self.processes.get(attempt_id)
        if process and process.poll() is None:
            process.terminate()

    def _track_process(self, attempt_id: str, process: subprocess.Popen[str] | None) -> None:
        with self.lock:
            if process is None:
                self.processes.pop(attempt_id, None)
            else:
                self.processes[attempt_id] = process


class ClusterMaster:
    def __init__(self, worker: ClusterWorker, job_manager: Any, config_store: Any, data_directory: Path):
        self.worker = worker
        self.jobs = job_manager
        self.config_store = config_store
        self.nodes_path = data_directory / "cluster-nodes.json"
        self.nodes = _load_json(self.nodes_path, {"nodes": []})["nodes"]
        self.lock = threading.RLock()
        self.threads: dict[str, threading.Thread] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.stopping = False

    def discover(self) -> list[dict[str, Any]]:
        if not shutil.which("dns-sd"):
            return []
        browser = subprocess.Popen(["dns-sd", "-B", "_smartstitch._tcp", "local."],
                                   stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            try:
                output, _ = browser.communicate(timeout=2)
            except subprocess.TimeoutExpired as exc:
                output = exc.output or ""
        finally:
            browser.kill()
            browser.communicate()
        if isinstance(output, bytes):
            output = output.decode("utf-8", "replace")
        names = []
        for line in output.splitlines():
            match = re.search(r"\bAdd\b.*?_smartstitch\._tcp\.?\s+(.+)$", line)
            if match:
                names.append(match.group(1).strip())
        found = []
        for name in dict.fromkeys(names):
            resolver = subprocess.Popen(["dns-sd", "-L", name, "_smartstitch._tcp", "local."],
                                        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
            try:
                try:
                    resolved, _ = resolver.communicate(timeout=2)
                except subprocess.TimeoutExpired as exc:
                    resolved = exc.output or ""
            finally:
                resolver.kill()
                resolver.communicate()
            if isinstance(resolved, bytes):
                resolved = resolved.decode("utf-8", "replace")
            match = re.search(r"can be reached at\s+([^:\s]+):([0-9]+)", resolved)
            if match:
                found.append({"name": name, "url": f"http://{match.group(1).rstrip('.')}:{match.group(2)}"})
        return found

    def add_node(self, url: str, token: str) -> dict[str, Any]:
        url = _node_url(url)
        try:
            hello = _request(f"{url}/hello", token)
        except HTTPError as exc:
            if exc.code == 401:
                raise ValueError("该工作机已开启令牌保护，请填写工作机令牌") from exc
            raise
        if hello.get("version") != __version__:
            raise ValueError(f"工作机版本 {hello.get('version', '未知')} 与主控 {__version__} 不一致")
        if hello.get("canonical_root") != str(self.config_store.canonical_directory):
            raise ValueError("节点挂载的共享目录与主控不一致")
        node = {"node_id": hello["node_id"], "name": hello["name"],
                "display_name": hello.get("display_name", ""), "url": url, "token": token}
        with self.lock:
            self.nodes = [entry for entry in self.nodes if entry["node_id"] != node["node_id"]]
            self.nodes.append(node)
            _safe_write(self.nodes_path, {"nodes": self.nodes}, secret=True)
        return {key: value for key, value in node.items() if key != "token"}

    def remove_node(self, node_id: str) -> None:
        with self.lock:
            self.nodes = [node for node in self.nodes if node["node_id"] != node_id]
            _safe_write(self.nodes_path, {"nodes": self.nodes}, secret=True)

    def node_statuses(self) -> list[dict[str, Any]]:
        result = []
        for node in list(self.nodes):
            try:
                status = _request(f"{node['url']}/hello", node["token"])
                result.append({**status, "url": node["url"], "online": True})
            except Exception as exc:
                result.append({"node_id": node["node_id"], "name": node["name"],
                               "display_name": node.get("display_name", ""),
                               "url": node["url"], "online": False, "error": str(exc)})
        return result

    def create(self, request: JobCreateRequest) -> dict[str, Any]:
        if not any(node["online"] for node in self.node_statuses()):
            raise ValueError("请先添加至少一台在线工作机")
        config = self.config_store.load(request.config_id)
        if request.output_directory:
            config.output.directory = request.output_directory
        output = Path(config.output.directory).expanduser().resolve()
        try:
            output.relative_to(self.config_store.directory.resolve())
        except ValueError as exc:
            raise ValueError("集群成片输出必须位于共享 SmartStitch 目录内") from exc
        request = request.model_copy(update={"auto_start": False})
        job = self.jobs.create(request)
        try:
            root = self.config_store.directory.resolve()
            for item in job["items"]:
                planned = _planned(item)
                for asset in [*planned.selections.values(), planned.overlay, planned.visual_border,
                              *(effect.asset for effect in planned.visual_effects)]:
                    if asset:
                        try:
                            Path(asset.path).resolve().relative_to(root)
                        except ValueError as exc:
                            raise ValueError(f"集群素材不在 NAS 共享目录中：{asset.path}") from exc
        except Exception:
            self.jobs.delete(job["id"])
            shutil.rmtree(job["output_directory"], ignore_errors=True)
            raise
        with self.jobs.lock:
            job["job_type"] = "cluster"
            job["status"] = "running"
            job["started_at"] = _now()
            self.jobs.database.save(job)
        self._launch(job["id"])
        return self.jobs.get_job(job["id"])

    def _launch(self, job_id: str) -> None:
        event = threading.Event()
        self.cancel_events[job_id] = event
        thread = threading.Thread(target=self._run, args=(job_id, event), daemon=True,
                                  name=f"cluster-master-{job_id[:8]}")
        self.threads[job_id] = thread
        thread.start()

    def resume_interrupted(self) -> None:
        for job in self.jobs.database.list():
            if job.get("job_type") != "cluster" or job.get("status") != "interrupted":
                continue
            job["status"] = "running"
            job["finished_at"] = None
            self.jobs.database.save(job)
            self._launch(job["id"])

    def _send_assignment(self, job: dict[str, Any], item: dict[str, Any], node: dict[str, Any], attempt_id: str) -> None:
        batch_path = Path(job["output_directory"]).resolve()
        relative = batch_path.relative_to(self.config_store.directory.resolve())
        config = yaml.safe_load(Path(job["config_snapshot_path"]).read_text(encoding="utf-8"))
        actual = self.config_store.directory
        canonical = self.config_store.canonical_directory
        payload = {
            "attempt_id": attempt_id, "batch_id": job["id"],
            "batch_relative_path": str(relative),
            "stage_name": f".{Path(item['output_name']).stem}.{attempt_id}.cluster.mp4",
            "canonical_root": str(canonical),
            "config": _canonicalize(config, actual, canonical),
            "item": _canonicalize(item, actual, canonical),
        }
        _request(f"{node['url']}/attempts", node["token"], method="POST", payload=payload)

    def _update_item(self, job_id: str, index: int, **updates: Any) -> dict[str, Any]:
        with self.jobs.lock:
            job = self.jobs.get_job(job_id)
            item = next(entry for entry in job["items"] if entry["index"] == index)
            item.update(updates)
            job["success_count"] = sum(entry["status"] == "succeeded" for entry in job["items"])
            job["failure_count"] = sum(entry["status"] == "failed" for entry in job["items"])
            self.jobs.database.save(job)
            return job

    def _run(self, job_id: str, cancel: threading.Event) -> None:
        unexpected_error: str | None = None
        try:
            while not self.stopping:
                job = self.jobs.get_job(job_id)
                if cancel.is_set():
                    for item in job["items"]:
                        if item["status"] == "running":
                            node = next((node for node in self.nodes if node["node_id"] == item.get("worker_id")), None)
                            if node:
                                try:
                                    _request(f"{node['url']}/attempts/{item['attempt_id']}/cancel", node["token"], method="POST")
                                except Exception:
                                    pass
                        if item["status"] in {"pending", "running"}:
                            self._update_item(job_id, item["index"], status="cancelled", error="任务已取消")
                    break
                statuses = {entry["node_id"]: entry for entry in self.node_statuses()}
                for item in job["items"]:
                    if item["status"] != "running":
                        continue
                    node = next((node for node in self.nodes if node["node_id"] == item.get("worker_id")), None)
                    try:
                        if node is None:
                            raise URLError("工作机已移除")
                        attempt = _request(f"{node['url']}/attempts/{item['attempt_id']}", node["token"])
                        if attempt["status"] == "succeeded":
                            stage = Path(attempt["stage_path"])
                            # Different NAS aliases require the master to use its own mount path.
                            if stage.is_absolute() and stage.exists():
                                local_stage = stage
                            else:
                                local_stage = Path(job["output_directory"]) / f".{Path(item['output_name']).stem}.{item['attempt_id']}.cluster.mp4"
                            target = Path(item["output_path"])
                            if not target.exists():
                                if not local_stage.is_file():
                                    raise FileNotFoundError("工作机成片暂存文件不存在")
                                local_stage.replace(target)
                            elif not target.is_file() or target.stat().st_size <= 0:
                                raise ValueError("成片文件为空，无法确认已发布结果")
                            self._update_item(job_id, item["index"], status="succeeded", progress=1.0,
                                              actual_duration=attempt["result"]["actual_duration"],
                                              actual_video_encoder=attempt["result"]["actual_video_encoder"],
                                              hardware_acceleration=attempt["result"]["hardware_acceleration"],
                                              encoder_fallback_reason=attempt["result"]["encoder_fallback_reason"], error=None)
                        elif attempt["status"] in {"failed", "cancelled", "interrupted"}:
                            next_status = "failed" if item["attempts"] > job["retry_count"] else "pending"
                            self._update_item(job_id, item["index"], status=next_status, error=attempt.get("error"))
                        else:
                            self._update_item(job_id, item["index"], progress=attempt.get("progress", 0), last_seen=time.time())
                    except (HTTPError, URLError, OSError, KeyError, ValueError) as exc:
                        if time.time() - item.get("last_seen", time.time()) > LOST_SECONDS:
                            next_status = "failed" if item["attempts"] > job["retry_count"] else "pending"
                            self._update_item(job_id, item["index"], status=next_status, error=str(exc))
                job = self.jobs.get_job(job_id)
                for item in job["items"]:
                    if item["status"] != "pending":
                        continue
                    node = next((node for node in self.nodes if statuses.get(node["node_id"], {}).get("online")
                                 and statuses[node["node_id"]].get("active", 1) < statuses[node["node_id"]].get("capacity", 1)), None)
                    if node is None:
                        break
                    attempt_id = uuid.uuid4().hex
                    self._update_item(job_id, item["index"], status="running", worker_id=node["node_id"],
                                      worker_name=node["name"], attempt_id=attempt_id,
                                      attempts=item["attempts"] + 1, last_seen=time.time(), progress=0.0, error=None)
                    try:
                        self._send_assignment(job, item, node, attempt_id)
                        statuses[node["node_id"]]["active"] += 1
                    except Exception as exc:
                        # A timed-out POST may already have been accepted by the worker.
                        # Poll this same attempt before assigning another one.
                        self._update_item(job_id, item["index"], error=f"等待派发确认：{exc}")
                job = self.jobs.get_job(job_id)
                if all(item["status"] in {"succeeded", "failed", "cancelled"} for item in job["items"]):
                    break
                cancel.wait(POLL_SECONDS)
        except Exception as exc:
            unexpected_error = str(exc)
            LOGGER.exception("Cluster batch %s stopped unexpectedly", job_id)
        finally:
            with self.jobs.lock:
                job = self.jobs.get_job(job_id)
                if unexpected_error:
                    job["status"] = "interrupted"
                    job["error"] = unexpected_error
                elif cancel.is_set():
                    job["status"] = "cancelled"
                elif job["success_count"] == job["count"]:
                    job["status"] = "completed"
                elif job["success_count"]:
                    job["status"] = "partial_failed"
                else:
                    job["status"] = "failed"
                job["finished_at"] = _now() if job["status"] != "interrupted" else None
                self.jobs.database.save(job)
                if job["status"] != "interrupted":
                    self.jobs._write_manifest(job)
                    self.jobs._write_csv(job)
                    if self.jobs.output_sync_manager is not None:
                        self.jobs.output_sync_manager.enqueue_if_enabled(job)
            self.threads.pop(job_id, None)
            self.cancel_events.pop(job_id, None)

    def cancel(self, job_id: str) -> dict[str, Any]:
        event = self.cancel_events.get(job_id)
        if event is None:
            raise ValueError("集群任务未运行")
        event.set()
        return self.jobs.get_job(job_id)

    def shutdown(self, timeout: float = 5) -> None:
        self.stopping = True
        for event in self.cancel_events.values():
            event.set()
        for thread in list(self.threads.values()):
            thread.join(timeout=timeout)
        self.worker.stop()

    def active_count(self) -> int:
        return sum(thread.is_alive() for thread in self.threads.values()) + len(self.worker.threads)
