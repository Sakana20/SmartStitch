"""Local, persistent batches of full-frame landscape video over blurred backgrounds."""
from __future__ import annotations

import copy
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import uuid
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any

from .scheduling import wait_for_start
from .database import SQLiteStore
from .video_encoding import (
    SOFTWARE_VIDEO_ENCODER, VIDEOTOOLBOX_VIDEO_ENCODER,
    preferred_encoder_plan, video_encoder_arguments,
)

TABLE = "portrait_jobs"
TERMINAL = {"completed", "partial_failed", "failed", "cancelled", "interrupted"}
SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv"}
RESOLUTIONS = {"720p": (720, 1280), "1080p": (1080, 1920)}
GENERATED = re.compile(r"_横改竖(?:_[0-9]+)?$", re.IGNORECASE)


def now() -> str:
    return datetime.now(UTC).isoformat()


def videos(directory: Path, generated: re.Pattern = GENERATED) -> list[Path]:
    return sorted((p for p in directory.iterdir() if p.is_file()
                   and not p.name.startswith((".", "~")) and p.suffix.lower() in SUFFIXES
                   and not generated.search(p.stem)), key=lambda p: (p.name.casefold(), p.name))


def fingerprint(path: Path) -> list[int]:
    stat = path.stat()
    return [stat.st_size, stat.st_mtime_ns]


def probe(path: Path) -> dict[str, Any]:
    executable = shutil.which("ffprobe")
    if not executable:
        raise ValueError("找不到 ffprobe")
    result = subprocess.run([executable, "-v", "error", "-show_streams", "-show_format",
                             "-of", "json", str(path)], capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise ValueError(f"视频无法读取：{result.stderr[-400:]}")
    return json.loads(result.stdout)


def inspect(path: Path) -> dict[str, Any]:
    data = probe(path)
    streams = data.get("streams", [])
    video = [s for s in streams if s.get("codec_type") == "video"]
    if len(video) != 1 or video[0].get("disposition", {}).get("attached_pic"):
        raise ValueError("需要单视频轨素材")
    stream = video[0]
    if stream.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
        raise ValueError("首版暂不支持 HDR 视频")
    if stream.get("field_order", "unknown") not in {"unknown", "progressive"}:
        raise ValueError("首版暂不支持隔行视频")
    width, height = int(stream["width"]), int(stream["height"])
    sar = stream.get("sample_aspect_ratio", "1:1")
    ratio = Fraction(sar.replace(":", "/")) if sar not in {"N/A", "0:1"} else Fraction(1)
    width = float(width * ratio)
    rotation = float(stream.get("tags", {}).get("rotate", 0))
    for side in stream.get("side_data_list", []):
        if "rotation" in side:
            rotation = float(side["rotation"])
    if abs(rotation / 90 - round(rotation / 90)) > .001:
        raise ValueError("无法处理此旋转角度")
    if round(rotation / 90) % 2:
        width, height = height, width
    if not math.isfinite(width) or not math.isfinite(height) or width <= 0 or height <= 0:
        raise ValueError("无法确定有效显示尺寸")
    fps = Fraction(stream.get("avg_frame_rate", "0/1"))
    if fps <= 0 or fps > 240:
        raise ValueError("无法确定有效帧率")
    duration = float(stream.get("duration") or data.get("format", {}).get("duration") or 0)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("无法确定视频时长")
    warnings = []
    audio = [s for s in streams if s.get("codec_type") == "audio"]
    if len(audio) > 1:
        warnings.append("仅保留第一条音轨")
    if any(s.get("codec_type") not in {"video", "audio"} for s in streams):
        warnings.append("字幕与附加数据轨不保留")
    if stream.get("pix_fmt", "").startswith(("yuva", "rgba", "bgra", "argb", "abgr", "gbrap", "ya")):
        warnings.append("透明通道不保留")
    return {"width": width, "height": height, "fps": str(fps), "duration": duration,
            "has_audio": bool(audio), "warnings": warnings,
            "nominal_fps": stream.get("r_frame_rate", "0/1"),
            "frame_count": int(stream["nb_frames"]) if str(stream.get("nb_frames", "")).isdigit() else None}


def foreground_size(width: float, height: float, output_width: int = 720, output_height: int = 1280) -> tuple[int, int]:
    scale = min(output_width / width, output_height / height)
    return max(2, round(width * scale / 2) * 2), max(2, round(height * scale / 2) * 2)


def command(source: Path, target: Path, item: dict[str, Any]) -> list[str]:
    output_width, output_height = item.get("output_width", 720), item.get("output_height", 1280)
    width, height = foreground_size(item["width"], item["height"], output_width, output_height)
    background_scale = max(output_width / item["width"], output_height / item["height"])
    bg_width = math.ceil(item["width"] * background_scale / 2) * 2
    bg_height = math.ceil(item["height"] * background_scale / 2) * 2
    # FFmpeg autorotates the decoded source before this graph.
    graph = ("[0:v:0]split=2[bs][fs];"
             f"[bs]scale={bg_width}:{bg_height},setsar=1,"
             f"crop={output_width}:{output_height},gblur=sigma={20 * min(output_width, output_height) / 720:g}:steps=2[bg];"
             f"[fs]scale={width}:{height},setsar=1[fg];"
             "[bg][fg]overlay=(W-w)/2:(H-h)/2:format=auto,format=yuv420p[v]")
    return [str(shutil.which("ffmpeg")), "-hide_banner", "-v", "error", "-nostdin", "-y",
            "-i", str(source), "-filter_complex", graph, "-map", "[v]", "-map", "0:a:0?",
            *video_encoder_arguments(item.get("actual_video_encoder", SOFTWARE_VIDEO_ENCODER), software_preset="medium"),
            *(["-allow_sw", "0"] if item.get("actual_video_encoder") == VIDEOTOOLBOX_VIDEO_ENCODER else []),
            "-fps_mode", "passthrough",
            "-c:a", "aac", "-b:a", "192k", "-map_metadata", "-1", "-metadata:s:v:0", "rotate=0",
            "-movflags", "+faststart", "-progress", "pipe:1", str(target)]


class LandscapeToPortraitManager:
    table = TABLE
    title = "横改竖"
    resolutions = RESOLUTIONS
    generated = GENERATED
    source_orientation = "横屏"

    def accepts_orientation(self, width: float, height: float) -> bool:
        return width > height

    def __init__(self, store: SQLiteStore):
        self.store = store
        store.ensure_job_table(self.table)
        store.mark_active_interrupted(self.table)
        for job in store.list(self.table):
            if job["status"] == "interrupted":
                for item in job["items"]:
                    if item["status"] in {"queued", "running"}:
                        item["status"] = "interrupted"
                job.update(current_file=None, current_progress=0)
                store.save(self.table, job)
        self.lock = threading.RLock()
        self.previews: dict[str, dict[str, Any]] = {}
        self.active: dict[str, Any] | None = None
        self.process: subprocess.Popen[str] | None = None
        self.worker: threading.Thread | None = None
        self.stopping = False
        self.cluster = None
        self.cancelled = threading.Event()

    def preview(self, source_directory: str = "", output_directory: str | None = None, resolution: str = "720p", mode: str = "local") -> dict[str, Any]:
        if mode not in {"local", "cluster"}:
            raise ValueError("执行方式无效")
        if mode == "cluster":
            from .cluster_conversion import nas_folders
            nas_source, nas_output = nas_folders(self)
            source_directory, output_directory = str(nas_source), str(nas_output)
        if resolution not in self.resolutions:
            raise ValueError("输出分辨率仅支持 720p 或 1080p")
        output_width, output_height = self.resolutions[resolution]
        if not source_directory.strip():
            raise ValueError("请选择源视频文件夹")
        source = Path(source_directory).expanduser().resolve()
        if not source.is_dir():
            raise ValueError("源视频文件夹不存在")
        output = Path(output_directory).expanduser().resolve() if output_directory else source / self.title
        items = []
        for path in videos(source, self.generated):
            if mode == "cluster" and not path.resolve().is_relative_to(source):
                raise ValueError(f"原素材中包含指向目录外的视频：{path.name}")
            items.append({"name": path.name, "source": str(path), "status": "pending",
                          "output_width": output_width, "output_height": output_height})
        preview = {"id": uuid.uuid4().hex, "mode": mode, "source_directory": str(source), "output_directory": str(output),
                   "resolution": resolution, "output_width": output_width, "output_height": output_height,
                   "items": items, "pending_count": sum(i["status"] == "pending" for i in items),
                   "skipped_count": sum(i["status"] == "skipped" for i in items),
                   "invalid_count": sum(i["status"] == "invalid" for i in items)}
        with self.lock:
            # Bound preflight memory; old browser sessions must reread after eviction.
            if len(self.previews) >= 20:
                self.previews.pop(next(iter(self.previews)))
            self.previews[preview["id"]] = preview
        return copy.deepcopy(preview)

    def _target(self, directory: Path, stem: str, reserved: set[Path]) -> Path:
        target = directory / f"{stem}_{self.title}.mp4"
        number = 2
        while target.exists() or target.is_symlink() or target in reserved:
            target = directory / f"{stem}_{self.title}_{number}.mp4"
            number += 1
        return target

    def create(self, preview_id: str, mode: str = "local", node_ids: list[str] | None = None, *, scheduled_at=None) -> dict[str, Any]:
        if mode not in {"local", "cluster"}:
            raise ValueError("执行方式无效")
        nodes = None
        if mode == "cluster":
            from .cluster_conversion import available_nodes
            if self.cluster is None:
                raise ValueError("集群不可用")
            if scheduled_at is None:
                nodes = available_nodes(self.cluster, node_ids)
            from .cluster_conversion import nas_folders
            nas_source, nas_output = nas_folders(self)
        with self.lock:
            if self.stopping or self.active:
                raise ValueError(f"已有{self.title}任务正在运行或应用正在退出")
            preview = self.previews.get(preview_id)
            if not preview or not preview["pending_count"]:
                raise ValueError("请重新读取视频文件列表")
            if preview.get("mode", "local") != mode:
                raise ValueError("执行方式已变化，请重新读取")
            source = Path(preview["source_directory"])
            if mode == "cluster":
                if source != nas_source or Path(preview["output_directory"]) != nas_output:
                    raise ValueError("NAS 目录已变化，请重新读取")
                if any(not Path(i["source"]).resolve().is_relative_to(nas_source) for i in preview["items"]):
                    raise ValueError("素材指向 NAS 原素材目录以外")
            if [p.name for p in videos(source, self.generated)] != [i["name"] for i in preview["items"]]:
                raise ValueError("文件夹内容已变化，请重新读取")
            if not shutil.which("ffmpeg"):
                raise ValueError("找不到 FFmpeg")
            output = Path(preview["output_directory"])
            output.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=output):
                pass
            if shutil.disk_usage(output).free < 100 * 1024 * 1024:
                raise ValueError("输出目录可用空间不足 100MB")
            job = copy.deepcopy(preview)
            job.update(id=uuid.uuid4().hex, mode=mode, direction="portrait-to-landscape" if self.table == "landscape_jobs" else "landscape-to-portrait", node_ids=[node["node_id"] for node in nodes] if nodes else (node_ids or []),
                       status="scheduled" if scheduled_at else "queued",
                       scheduled_at=scheduled_at.isoformat() if scheduled_at else None, created_at=now(), finished_at=None,
                       total=preview["pending_count"], completed=0, succeeded=0, failed=0,
                       current_file=None, current_progress=0)
            job["encoding"] = {**preferred_encoder_plan(), "actual_video_encoders": [],
                               "fallback_count": 0, "fallback_reason": None}
            for item in job["items"]:
                if item["status"] == "pending":
                    item["status"] = "queued"
                    item.update(planned_video_encoder=job["encoding"]["planned_video_encoder"],
                                actual_video_encoder=None, encoder_fallback_reason=None, encoder_attempts=[])
            self.store.save(self.table, job)
            self.previews.pop(preview_id)
            self.active = job
            self.cancelled.clear()
            self.worker = threading.Thread(target=self._await_start, args=(job, nodes), daemon=True)
            self.worker.start()
            return copy.deepcopy(job)

    def _await_start(self, job, nodes=None):
        delegated = False
        try:
            ready = wait_for_start(job.get("scheduled_at"), self.cancelled, lambda: self.stopping)
            if self.stopping:
                return
            if not ready:
                return
            if job["mode"] == "cluster":
                from .cluster_conversion import available_nodes, run_batch
                with self.lock:
                    job.update(status="queued", current_phase="waiting_workers")
                    self.store.save(self.table, job)
                while nodes is None and not self.cancelled.is_set() and not self.stopping:
                    try:
                        nodes = available_nodes(self.cluster, job.get("node_ids") or None)
                    except ValueError:
                        self.cancelled.wait(1.0)
                if self.stopping or self.cancelled.is_set():
                    return
                delegated = True
                run_batch(self, job, nodes)
            else:
                delegated = True
                self._run(job)
        finally:
            if not delegated:
                with self.lock:
                    if self.stopping and job["status"] not in {"scheduled", "cancelled"}:
                        job.update(status="interrupted", finished_at=None)
                    elif not self.stopping:
                        job.update(status="cancelled", finished_at=now())
                        for item in job["items"]:
                            if item["status"] == "queued":
                                item["status"] = "cancelled"
                    self.store.save(self.table, job)
                    self.active = None

    def resume_scheduled(self):
        with self.lock:
            if self.stopping or self.active:
                return
            for job in self.store.list(self.table):
                if job.get("mode") != "cluster" or not job.get("scheduled_at") or job["status"] not in {"scheduled", "interrupted"}:
                    continue
                for item in job["items"]:
                    if item["status"] == "interrupted":
                        item["status"] = "queued"
                job.update(status="scheduled", finished_at=None)
                self.store.save(self.table, job)
                self.active = job
                self.cancelled.clear()
                self.worker = threading.Thread(target=self._await_start, args=(job,), daemon=True)
                self.worker.start()
                break

    def list(self) -> list[dict[str, Any]]:
        with self.lock:
            jobs = self.store.list(self.table)
            return [copy.deepcopy(self.active) if self.active and j["id"] == self.active["id"] else j for j in jobs]

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if self.active and self.active["id"] == job_id:
                return copy.deepcopy(self.active)
            job = self.store.get(self.table, job_id)
            if job is None:
                raise KeyError(job_id)
            return job

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            job = self.get(job_id)
            if job["status"] in TERMINAL:
                return job
            self.cancelled.set()
            assert self.active is not None
            self.active["status"] = "cancelled" if job["status"] == "scheduled" else "cancelling"
            if job["status"] == "scheduled":
                self.active["finished_at"] = now()
                for item in self.active["items"]:
                    if item["status"] == "queued":
                        item["status"] = "cancelled"
            self.store.save(self.table, self.active)
            if self.process and self.process.poll() is None:
                self.process.terminate()
                process = self.process
                def force_stop() -> None:
                    if process.poll() is None:
                        try:
                            process.kill()
                        except ProcessLookupError:
                            pass
                timer = threading.Timer(2, force_stop)
                timer.daemon = True
                timer.start()
            return copy.deepcopy(self.active)

    def delete(self, job_id: str) -> None:
        with self.lock:
            if self.get(job_id)["status"] not in TERMINAL:
                raise ValueError("运行中的任务不能删除")
            if self.active and self.active["id"] == job_id:
                raise ValueError("任务正在结束，请稍后删除")
            self.store.delete(self.table, job_id)

    def active_count(self) -> int:
        with self.lock:
            return int(self.active is not None)

    def shutdown(self, timeout: float = 8) -> None:
        with self.lock:
            self.stopping = True
            if self.active:
                if self.active["status"] == "scheduled":
                    self.cancelled.set()
                else:
                    self.cancel(self.active["id"])
            worker = self.worker
        if worker:
            worker.join(max(0, timeout / 2))
            with self.lock:
                if self.process and self.process.poll() is None:
                    self.process.kill()
            worker.join(max(0, timeout / 2))

    def _render_and_verify(self, job: dict[str, Any], item: dict[str, Any], temporary: Path) -> bool:
        with tempfile.TemporaryFile() as errors:
            with self.lock:
                if self.cancelled.is_set():
                    return False
                process = subprocess.Popen(command(Path(item["source"]), temporary, item),
                                           stdout=subprocess.PIPE, stderr=errors, text=True)
                self.process = process
            assert process.stdout is not None
            for line in process.stdout:
                if line.startswith("out_time_us="):
                    try:
                        progress = min(.99, max(0, int(line.split("=", 1)[1]) / 1e6 / item["duration"]))
                    except ValueError:
                        continue
                    with self.lock:
                        job["current_progress"] = progress
            process.wait()
            process.stdout.close()
            if self.cancelled.is_set():
                return False
            if process.returncode:
                errors.seek(0)
                raise ValueError(errors.read().decode(errors="replace")[-1200:] or "FFmpeg 转换失败")
        result = inspect(temporary)
        if (result["width"], result["height"]) != (job["output_width"], job["output_height"]) or result["has_audio"] != item["has_audio"]:
            raise ValueError("输出尺寸或音轨校验失败")
        if abs(result["duration"] - item["duration"]) > max(.1, 2 / float(Fraction(item["fps"]))):
            raise ValueError("输出时长校验失败")
        # VFR container rates/counts may differ despite identical decoded timestamps.
        # Only compare these header fields for sources whose rates suggest CFR.
        if Fraction(item.get("nominal_fps", "0/1")) == Fraction(item["fps"]):
            if Fraction(result["fps"]) != Fraction(item["fps"]):
                raise ValueError("输出帧率校验失败")
            if item.get("frame_count") is not None and result.get("frame_count") is not None and result["frame_count"] != item["frame_count"]:
                raise ValueError("输出帧数校验失败")
        return True

    @staticmethod
    def _update_encoding_summary(job: dict[str, Any]) -> None:
        encoding = job["encoding"]
        encoding["actual_video_encoders"] = sorted({item["actual_video_encoder"] for item in job["items"] if item.get("actual_video_encoder")})
        encoding["fallback_count"] = sum(bool(item.get("encoder_fallback_reason")) for item in job["items"])
        encoding["hardware_acceleration_used"] = VIDEOTOOLBOX_VIDEO_ENCODER in encoding["actual_video_encoders"]

    def _run(self, job: dict[str, Any]) -> None:
        hardware_disabled_reason = None
        try:
            for item in job["items"]:
                if item["status"] != "queued":
                    continue
                with self.lock:
                    if self.cancelled.is_set():
                        break
                    job.update(status="running", current_file=item["name"], current_progress=0, current_phase="inspecting")
                    item["status"] = "running"
                    self.store.save(self.table, job)
                temporary = Path(job["output_directory"]) / f".{uuid.uuid4().hex}.tmp.mp4"
                prepared = False
                try:
                    source = Path(item["source"])
                    source_fingerprint = fingerprint(source)
                    info = inspect(source)
                    with self.lock:
                        item.update(info)
                        if self.cancelled.is_set():
                            break
                        if not self.accepts_orientation(item["width"], item["height"]):
                            item.update(status="skipped", error=f"不是{self.source_orientation}视频")
                            job["skipped_count"] += 1
                            continue
                    prepared = True
                    with self.lock:
                        self.process = None
                        if self.cancelled.is_set():
                            break
                        if fingerprint(source) != source_fingerprint:
                            raise ValueError("源文件已变化，请重新读取")
                        item["fingerprint"] = source_fingerprint
                        target = self._target(Path(job["output_directory"]), source.stem, set())
                        item["output_path"] = str(target)
                        job["current_phase"] = "rendering"
                    planned = item["planned_video_encoder"]
                    encoders = [planned]
                    if planned == VIDEOTOOLBOX_VIDEO_ENCODER:
                        if hardware_disabled_reason:
                            encoders = [SOFTWARE_VIDEO_ENCODER]
                            item["encoder_fallback_reason"] = hardware_disabled_reason
                        else:
                            encoders.append(SOFTWARE_VIDEO_ENCODER)
                    for encoder in encoders:
                        with self.lock:
                            if self.cancelled.is_set():
                                break
                            job["current_progress"] = 0
                            item["actual_video_encoder"] = encoder
                            attempt = {"encoder": encoder, "status": "running", "started_at": now()}
                            item["encoder_attempts"].append(attempt)
                            self._update_encoding_summary(job)
                            self.store.save(self.table, job)
                        try:
                            rendered = self._render_and_verify(job, item, temporary)
                        except (ValueError, OSError, subprocess.TimeoutExpired, KeyError) as exc:
                            with self.lock:
                                attempt.update(status="cancelled" if self.cancelled.is_set() else "failed",
                                               finished_at=now(), error=str(exc))
                            if self.cancelled.is_set():
                                break
                            if encoder != VIDEOTOOLBOX_VIDEO_ENCODER:
                                raise
                            temporary.unlink(missing_ok=True)
                            with self.lock:
                                hardware_disabled_reason = str(exc)
                                item["encoder_fallback_reason"] = str(exc)
                                job["encoding"]["fallback_reason"] = str(exc)
                                self._update_encoding_summary(job)
                                self.store.save(self.table, job)
                            continue
                        with self.lock:
                            attempt.update(status="succeeded" if rendered else "cancelled", finished_at=now())
                        break
                    if self.cancelled.is_set():
                        break
                    # Reserve a name exclusively, including on SMB volumes without hard links.
                    with self.lock:
                        if self.cancelled.is_set():
                            break
                        while True:
                            try:
                                descriptor = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                                break
                            except FileExistsError:
                                target = self._target(target.parent, Path(item["source"]).stem, set())
                        try:
                            reserved_stat = os.fstat(descriptor)
                            current_stat = target.stat()
                            if (reserved_stat.st_ino, reserved_stat.st_dev) != (current_stat.st_ino, current_stat.st_dev):
                                raise ValueError("输出文件被其他进程修改")
                            os.replace(temporary, target)
                        except Exception:
                            if target.exists() and target.stat().st_ino == os.fstat(descriptor).st_ino:
                                target.unlink()
                            raise
                        finally:
                            os.close(descriptor)
                        item.update(status="completed", output_path=str(target), progress=1)
                        job["succeeded"] += 1
                except (ValueError, OSError, subprocess.TimeoutExpired, KeyError, ZeroDivisionError) as exc:
                    with self.lock:
                        if not self.cancelled.is_set():
                            item.update(status="failed" if prepared else "invalid", error=str(exc))
                            job["failed" if prepared else "invalid_count"] += 1
                finally:
                    temporary.unlink(missing_ok=True)
                    with self.lock:
                        self.process = None
                        if item["status"] in {"completed", "failed", "skipped", "invalid"}:
                            job["completed"] += 1
                            job["current_progress"] = 0
                        self.store.save(self.table, job)
            with self.lock:
                job["status"] = ("interrupted" if self.stopping else "cancelled") if self.cancelled.is_set() else (
                    "partial_failed" if job["failed"] and job["succeeded"] else "failed" if job["failed"] else "completed")
        except Exception as exc:
            with self.lock:
                job.update(status="failed", error=str(exc))
        finally:
            with self.lock:
                for item in job["items"]:
                    if item["status"] in {"queued", "running"}:
                        item["status"] = "interrupted" if job["status"] == "interrupted" else "cancelled"
                job.update(current_file=None, current_progress=0, current_phase=None, finished_at=now())
                self.store.save(self.table, job)
                self.active = None
