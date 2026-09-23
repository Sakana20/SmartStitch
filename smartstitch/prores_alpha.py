"""Batch conversion of black-background footage to ProRes 4444 with alpha."""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .visual_borders import VisualBorderLibrary, VisualBorderLibraryError


VIDEO_SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv"}
ALPHA_FILTER = (
    "format=rgba,"
    "geq=r='if(gt(max(max(r(X,Y),g(X,Y)),b(X,Y)),3),"
    "255*r(X,Y)/max(max(r(X,Y),g(X,Y)),b(X,Y)),0)':"
    "g='if(gt(max(max(r(X,Y),g(X,Y)),b(X,Y)),3),"
    "255*g(X,Y)/max(max(r(X,Y),g(X,Y)),b(X,Y)),0)':"
    "b='if(gt(max(max(r(X,Y),g(X,Y)),b(X,Y)),3),"
    "255*b(X,Y)/max(max(r(X,Y),g(X,Y)),b(X,Y)),0)':"
    "a='max(max(r(X,Y),g(X,Y)),b(X,Y))',"
    "format=yuva444p10le"
)
def _now() -> str:
    return datetime.now(UTC).isoformat()


def find_videos(directory: Path) -> list[Path]:
    return sorted(
        (path for path in directory.iterdir() if path.is_file() and path.suffix.lower() in VIDEO_SUFFIXES),
        key=lambda path: path.name.casefold(),
    )


def build_command(source: Path, output: Path, ffmpeg: str) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-y", "-i", str(source), "-vf", ALPHA_FILTER,
        "-c:v", "prores_ks", "-profile:v", "4", "-qscale:v", "64",
        "-pix_fmt", "yuva444p10le", "-an", str(output),
    ]


class ProResAlphaManager:
    def __init__(self, visual_border_library: VisualBorderLibrary | None = None) -> None:
        self.lock = threading.RLock()
        self.visual_border_library = visual_border_library
        self.jobs: dict[str, dict[str, Any]] = {}
        self.active_id: str | None = None
        self.latest_id: str | None = None
        self.process: subprocess.Popen[str] | None = None
        self.stopping = False
        self.worker: threading.Thread | None = None

    def preview(self, source_directory: str) -> dict[str, Any]:
        source, files = self._source_files(source_directory)
        return {"source_directory": str(source), "files": [path.name for path in files], "count": len(files)}

    @staticmethod
    def _source_files(source_directory: str) -> tuple[Path, list[Path]]:
        source = Path(source_directory).expanduser().resolve()
        if not source.is_dir():
            raise ValueError("源视频文件夹不存在")
        files = find_videos(source)
        if not files:
            raise ValueError("文件夹内没有 MP4、MOV、M4V 或 MKV 视频")
        names = [path.stem.casefold() for path in files]
        if len(names) != len(set(names)):
            raise ValueError("存在同名但扩展名不同的视频，输出文件名会冲突；请先重命名")
        return source, files

    def create(self, source_directory: str, destinations: dict[str, str] | None = None) -> dict[str, Any]:
        source, files = self._source_files(source_directory)
        if destinations is not None and set(destinations) != {path.name for path in files}:
            raise ValueError("待转换视频已变化，请重新读取视频并选择保存位置")
        destinations = destinations or {}
        output_directories: dict[str, Path] = {"alpha_output": source / "Alpha输出"}
        for destination in set(destinations.values()):
            if destination == "alpha_output":
                continue
            if destination not in {"effect_1", "effect_2"} or self.visual_border_library is None:
                raise ValueError("保存位置无效")
            try:
                output_directories[destination] = self.visual_border_library.effect_library_directory(destination)
            except VisualBorderLibraryError as exc:
                raise ValueError(f"保存位置 {destination} 不可用：{exc}") from exc
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise ValueError("找不到 FFmpeg")
        with self.lock:
            if self.stopping:
                raise ValueError("SmartStitch 正在退出，不能创建新任务")
            if self.active_id is not None:
                raise ValueError("已有 ProRes 4444 任务正在运行，请等待完成")
            selected_destinations = {destinations.get(path.name, "alpha_output") for path in files}
            for key in selected_destinations:
                directory = output_directories[key]
                directory.mkdir(parents=True, exist_ok=True)
            job_id = uuid.uuid4().hex
            records = []
            reserved: set[Path] = set()
            for path in files:
                destination = destinations.get(path.name, "alpha_output")
                directory = output_directories[destination]
                output_name = f"{path.stem}_Alpha.mov"
                if destination != "alpha_output":
                    number = 2
                    while (directory / output_name).exists() or directory / output_name in reserved:
                        output_name = f"{path.stem}_Alpha_{number}.mov"
                        number += 1
                reserved.add(directory / output_name)
                records.append({
                    "source": path.name,
                    "output": output_name,
                    "destination": destination,
                    "output_directory": str(directory),
                    "status": "queued",
                })
            job = {
                "id": job_id,
                "status": "queued",
                "source_directory": str(source),
                "output_directory": str(output_directories["alpha_output"]),
                "total": len(files),
                "completed": 0,
                "succeeded": 0,
                "failed": 0,
                "current_file": None,
                "files": records,
                "created_at": _now(),
                "finished_at": None,
            }
            self.jobs[job_id] = job
            self.active_id = job_id
            self.latest_id = job_id
            self.worker = threading.Thread(target=self._run, args=(job_id, files, ffmpeg), daemon=True)
            self.worker.start()
            return copy.deepcopy(job)

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self.jobs[job_id])

    def latest(self) -> dict[str, Any] | None:
        with self.lock:
            return copy.deepcopy(self.jobs[self.latest_id]) if self.latest_id else None

    def active_count(self) -> int:
        with self.lock:
            return int(self.active_id is not None)

    def shutdown(self, timeout: float = 8.0) -> None:
        with self.lock:
            self.stopping = True
            if self.process is not None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
            worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(max(0.0, timeout))

    def _run(self, job_id: str, files: list[Path], ffmpeg: str) -> None:
        try:
            for index, source in enumerate(files):
                with self.lock:
                    job = self.jobs[job_id]
                    if self.stopping:
                        job["status"] = "interrupted"
                        break
                    job["status"] = "running"
                    job["current_file"] = source.name
                    job["files"][index]["status"] = "running"
                    target = Path(job["files"][index]["output_directory"]) / job["files"][index]["output"]
                temporary = target.with_name(f".{target.stem}.{job_id}.tmp.mov")
                try:
                    with self.lock:
                        if self.stopping:
                            job["status"] = "interrupted"
                            break
                        self.process = subprocess.Popen(
                            build_command(source, temporary, ffmpeg),
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                            text=True, errors="replace",
                        )
                    _, stderr = self.process.communicate()
                    returncode = self.process.returncode
                    with self.lock:
                        self.process = None
                    if self.stopping:
                        job["status"] = "interrupted"
                        break
                    if returncode:
                        raise RuntimeError((stderr or "FFmpeg 转换失败")[-1200:])
                    os.replace(temporary, target)
                    with self.lock:
                        job["files"][index]["status"] = "completed"
                        job["files"][index]["size_bytes"] = target.stat().st_size
                        job["succeeded"] += 1
                except (OSError, RuntimeError) as exc:
                    with self.lock:
                        job["files"][index]["status"] = "failed"
                        job["files"][index]["error"] = str(exc)
                        job["failed"] += 1
                finally:
                    temporary.unlink(missing_ok=True)
                    with self.lock:
                        if job["files"][index]["status"] in {"completed", "failed"}:
                            job["completed"] += 1
            with self.lock:
                if job["status"] != "interrupted":
                    job["status"] = "completed" if not job["failed"] else ("partial_failed" if job["succeeded"] else "failed")
                job["current_file"] = None
                job["finished_at"] = _now()
        except Exception as exc:
            with self.lock:
                job = self.jobs[job_id]
                job["status"] = "failed"
                job["current_file"] = None
                job["error"] = str(exc)
                job["finished_at"] = _now()
        finally:
            with self.lock:
                self.process = None
                self.active_id = None
