"""Distributed, frame-accurate video upscaling over existing cluster workers."""

from __future__ import annotations

import copy
import os
import shutil
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .cluster import LOST_SECONDS, POLL_SECONDS, _file_sha256, _request
from .video_upscale import SUFFIXES, model_config, mux_audio, probe_video


class ClusterUpscaleManager:
    def __init__(self, cluster: Any, database: Any):
        self.cluster = cluster
        self.database = database
        self.database.ensure_job_table("upscale_jobs")
        self.database.mark_active_interrupted("upscale_jobs")
        self.lock = threading.RLock()
        self.events: dict[str, threading.Event] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.stopping = False

    def nodes(self) -> list[dict[str, Any]]:
        return [{"node_id": row["node_id"], "name": row.get("display_name") or row.get("name"),
                 "online": row.get("online", False), "active": row.get("active"),
                 "video_upscale": row.get("video_upscale", {})}
                for row in self.cluster.node_statuses()]

    def create(self, source: str, output_directory: str | None = None, model_name: str = "x2plus") -> dict[str, Any]:
        info = probe_video(Path(source))
        config = model_config(model_name)
        available = [node for node in self.nodes() if node["online"]
                     and node["video_upscale"].get("models", {}).get(model_name, {}).get("available")
                     and node["video_upscale"]["models"][model_name].get("model_sha256") == config["sha256"]
                     and node["video_upscale"].get("protocol") == 2]
        if not available:
            raise ValueError(f"没有安装相同 {model_name} 模型的在线工作机")
        root = self.cluster.config_store.directory.resolve()
        if not root.is_dir():
            raise ValueError("共享 SmartStitch 目录不可用")
        target_dir = Path(output_directory).expanduser().resolve() if output_directory else Path(info["source"]).parent
        if not target_dir.is_dir():
            raise ValueError("输出文件夹不存在")
        with self.lock:
            if self.stopping or any(thread.is_alive() for thread in self.threads.values()):
                raise ValueError("已有集群超分任务运行")
            job_id = uuid.uuid4().hex
            stem = Path(info["source"]).stem + ("_SR2x_原尺寸" if model_name == "x2plus" else f"_SR_{model_name}_原尺寸")
            target = target_dir / f"{stem}.mp4"
            number = 2
            while target.exists():
                target = target_dir / f"{stem}_{number}.mp4"
                number += 1
            segments = [{"start": start, "end": min(start+48, info["frames"]), "status": "pending",
                         "attempts": 0, "attempt_id": None, "node_id": None, "processed_frames": 0,
                         "stage_relative": None, "error": None}
                        for start in range(0, info["frames"], 48)]
            job = {"id": job_id, "mode": "cluster", "status": "queued", "phase": "staging",
                   "model": model_name, "scale": config["scale"],
                   "width": info["width"], "height": info["height"], "fps": info["fps"],
                   "source": info["source"], "output_path": str(target), "info": info,
                   "processed_frames": 0, "total_frames": info["frames"], "segments": segments,
                   "stage_relative": f".video-upscale/{job_id}", "source_sha256": None,
                   "created_at": time.time(), "finished_at": None, "error": None}
            self.database.save("upscale_jobs", job)
            self._launch(job_id)
            return self._public(job)

    def _launch(self, job_id: str) -> None:
        event = threading.Event()
        self.events[job_id] = event
        thread = threading.Thread(target=self._run, args=(job_id, event), daemon=True,
                                  name=f"cluster-upscale-{job_id[:8]}")
        self.threads[job_id] = thread
        thread.start()

    def resume_interrupted(self) -> None:
        with self.lock:
            for job in self.database.list("upscale_jobs"):
                if job["status"] != "interrupted":
                    continue
                # Existing completed segments remain authoritative; in-flight attempts are reconciled.
                for segment in job["segments"]:
                    if segment["status"] == "running":
                        segment["last_seen"] = time.time()
                job["status"] = "running"
                self.database.save("upscale_jobs", job)
                self._launch(job["id"])

    def _save(self, job: dict[str, Any]) -> None:
        job["processed_frames"] = sum((segment["end"]-segment["start"] if segment["status"] == "succeeded"
                                       else segment.get("processed_frames", 0)) for segment in job["segments"])
        self.database.save("upscale_jobs", job)

    def _public(self, job: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(job)
        result.pop("source_sha256", None)
        result.pop("stage_relative", None)
        return result

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.database.get("upscale_jobs", job_id)
        if job is None:
            raise KeyError(job_id)
        return self._public(job)

    def latest(self) -> dict[str, Any] | None:
        rows = self.database.list("upscale_jobs")
        return self._public(rows[0]) if rows else None

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            event = self.events.get(job_id)
            if event is None:
                raise ValueError("集群超分任务没有运行")
            event.set()
            return self.get(job_id)

    def _stage(self, job: dict[str, Any], cancel: threading.Event) -> Path:
        root = self.cluster.config_store.directory.resolve()
        directory = root / job["stage_relative"]
        if not directory.resolve().is_relative_to(root):
            raise RuntimeError("超分暂存目录越过共享目录")
        directory.mkdir(parents=True, exist_ok=True)
        staged = directory / "source.mp4"
        original = Path(job["source"])
        info = job["info"]
        if original.stat().st_size != info["size_bytes"] or original.stat().st_mtime_ns != info["modified_ns"]:
            raise RuntimeError("源视频在预检后发生变化")
        # Files already under the NAS source folder are visible to every worker.
        shared_source = root / "超分" / "原素材"
        if original.is_relative_to(shared_source) and original.is_file():
            job["source_relative"] = str(original.relative_to(root))
            job["source_sha256"] = _file_sha256(original)
            job["phase"] = "upscaling"
            self._save(job)
            return original
        if not staged.is_file():
            temporary = directory / f".{uuid.uuid4().hex}.upload"
            try:
                with original.open("rb") as input_file, temporary.open("wb") as output_file:
                    while block := input_file.read(4 * 1024 * 1024):
                        if cancel.is_set():
                            raise RuntimeError("任务已取消")
                        output_file.write(block)
                if temporary.stat().st_size != info["size_bytes"]:
                    raise RuntimeError("源视频复制不完整")
                os.replace(temporary, staged)
            finally:
                temporary.unlink(missing_ok=True)
        digest = _file_sha256(staged)
        if _file_sha256(original) != digest:
            raise RuntimeError("源视频与共享副本校验失败")
        job["source_sha256"] = digest
        job["source_relative"] = f"{job['stage_relative']}/source.mp4"
        job["phase"] = "upscaling"
        self._save(job)
        return staged

    def _cancel_remote(self, job: dict[str, Any]) -> None:
        for segment in job["segments"]:
            if segment["status"] != "running":
                continue
            node = next((node for node in self.cluster.nodes if node["node_id"] == segment["node_id"]), None)
            if node:
                try:
                    _request(f"{node['url']}/attempts/{segment['attempt_id']}/cancel", node["token"], method="POST")
                except Exception:
                    pass

    def _schedule(self, job: dict[str, Any], cancel: threading.Event) -> None:
        root = self.cluster.config_store.directory.resolve()
        while not cancel.is_set() and not self.stopping:
            statuses = {row["node_id"]: row for row in self.cluster.node_statuses()}
            for segment in job["segments"]:
                if segment["status"] != "running":
                    continue
                node = next((row for row in self.cluster.nodes if row["node_id"] == segment["node_id"]), None)
                try:
                    if node is None:
                        raise RuntimeError("工作机已移除")
                    response = _request(f"{node['url']}/attempts/{segment['attempt_id']}", node["token"])
                    if response["status"] == "succeeded":
                        path = root / segment["stage_relative"]
                        result = response["result"]
                        if not path.is_file() or path.stat().st_size != result["size_bytes"] or _file_sha256(path) != result["sha256"]:
                            raise RuntimeError("超分片段校验失败")
                        checked = probe_video(path)
                        if (checked["frames"] != segment["end"]-segment["start"] or checked["has_audio"]
                                or checked["fps"] != job["info"]["fps"] or checked["width"] != job["info"]["width"]
                                or checked["height"] != job["info"]["height"]):
                            raise RuntimeError("超分片段帧数或音轨错误")
                        segment.update(status="succeeded", processed_frames=result["frames"], error=None)
                    elif response["status"] in {"failed", "cancelled", "interrupted"}:
                        segment.update(status="pending" if segment["attempts"] < 3 else "failed",
                                       error=response.get("error"), processed_frames=0)
                    else:
                        segment["processed_frames"] = response.get("processed_frames", 0)
                        segment["last_seen"] = time.time()
                except Exception as exc:
                    if time.time()-segment.get("last_seen", time.time()) > LOST_SECONDS:
                        segment.update(status="pending" if segment["attempts"] < 3 else "failed",
                                       error=str(exc), processed_frames=0)
            for segment in job["segments"]:
                if segment["status"] != "pending":
                    continue
                node = next((row for row in self.cluster.nodes if statuses.get(row["node_id"], {}).get("online")
                             and statuses[row["node_id"]].get("video_upscale", {}).get("models", {}).get(job["model"], {}).get("available")
                             and statuses[row["node_id"]]["video_upscale"]["models"][job["model"]].get("model_sha256") == model_config(job["model"])["sha256"]
                             and statuses[row["node_id"]].get("active", 1) < 1), None)
                if node is None:
                    break
                attempt = uuid.uuid4().hex
                relative = f"{job['stage_relative']}/{attempt}.mp4"
                segment.update(status="running", attempt_id=attempt, node_id=node["node_id"],
                               stage_relative=relative, attempts=segment["attempts"]+1,
                               processed_frames=0, last_seen=time.time(), error=None)
                payload = {"kind": "video_upscale", "protocol": 2, "attempt_id": attempt,
                           "job_id": job["id"], "canonical_root": str(self.cluster.config_store.canonical_directory),
                           "source_relative": job["source_relative"], "source_sha256": job["source_sha256"],
                           "stage_relative": relative, "start": segment["start"], "end": segment["end"],
                           "frames": job["info"]["frames"], "width": job["info"]["width"],
                           "height": job["info"]["height"], "fps": job["info"]["fps"],
                           "model": job["model"], "model_sha256": model_config(job["model"])["sha256"]}
                self._save(job)
                try:
                    _request(f"{node['url']}/upscale-attempts", node["token"], method="POST", payload=payload)
                    statuses[node["node_id"]]["active"] += 1
                except Exception as exc:
                    # An HTTP timeout may follow an accepted request; reconcile this attempt first.
                    segment["error"] = f"等待派发确认：{exc}"
            self._save(job)
            if all(row["status"] == "succeeded" for row in job["segments"]):
                return
            if any(row["status"] == "failed" for row in job["segments"]):
                raise RuntimeError("部分超分片段在重试后失败")
            cancel.wait(POLL_SECONDS)
        raise RuntimeError("任务已取消" if cancel.is_set() else "应用正在退出")

    def _publish(self, job: dict[str, Any], cancel: threading.Event) -> None:
        root = self.cluster.config_store.directory.resolve()
        directory = root / job["stage_relative"]
        manifest = directory / "concat.txt"
        segments = [directory / Path(row["stage_relative"]).name for row in job["segments"]]
        manifest.write_text("".join(f"file '{path.name}'\n" for path in segments), encoding="utf-8")
        silent = directory / "joined.mp4"
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise RuntimeError("找不到 FFmpeg")
        result = subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "1",
                                 "-i", str(manifest), "-c", "copy", str(silent)], capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError((result.stderr or "片段拼接失败")[-1000:])
        info = job["info"]
        checked = probe_video(silent)
        if (checked["frames"] != info["frames"] or checked["width"] != info["width"]
                or checked["height"] != info["height"] or checked["fps"] != info["fps"]):
            raise RuntimeError("拼接结果帧数或尺寸错误")
        job["phase"] = "audio"
        self._save(job)
        target = Path(job["output_path"])
        temporary = target.with_name(f".{target.stem}.{job['id']}.tmp.mp4")
        try:
            mux_audio(silent, Path(job["source"]), temporary, info)
            if cancel.is_set():
                raise RuntimeError("任务已取消")
            verified = probe_video(temporary)
            if (verified["frames"] != info["frames"] or verified["has_audio"] != info["has_audio"]
                    or verified["width"] != info["width"] or verified["height"] != info["height"]
                    or verified["fps"] != info["fps"]):
                raise RuntimeError("最终视频帧数或音轨校验失败")
            if target.exists():
                raise RuntimeError("目标文件已存在，拒绝覆盖")
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)

    def _run(self, job_id: str, cancel: threading.Event) -> None:
        job = self.database.get("upscale_jobs", job_id)
        assert job is not None
        try:
            job["status"] = "running"
            self._save(job)
            self._stage(job, cancel)
            self._schedule(job, cancel)
            if cancel.is_set():
                raise RuntimeError("任务已取消")
            job["phase"] = "joining"
            self._save(job)
            self._publish(job, cancel)
            job.update(status="completed", phase="completed", error=None)
        except Exception as exc:
            self._cancel_remote(job)
            job.update(status="interrupted" if self.stopping else
                       "cancelled" if cancel.is_set() else "failed", phase="finished", error=str(exc))
        finally:
            job["finished_at"] = time.time() if job["status"] != "interrupted" else None
            self._save(job)
            if job["status"] != "interrupted":
                shutil.rmtree(self.cluster.config_store.directory / job["stage_relative"], ignore_errors=True)
            with self.lock:
                self.events.pop(job_id, None)
                self.threads.pop(job_id, None)

    def active_count(self) -> int:
        return sum(thread.is_alive() for thread in self.threads.values())

    def shutdown(self) -> None:
        self.stopping = True
        for event in self.events.values():
            event.set()
        for thread in list(self.threads.values()):
            thread.join(timeout=5)


class ClusterUpscaleBatchManager:
    """Process files in NAS 超分/原素材 into 超分/已处理 using the cluster."""

    def __init__(self, single: ClusterUpscaleManager, database: Any):
        self.single = single
        self.database = database
        self.database.ensure_job_table("upscale_batches")
        self.database.mark_active_interrupted("upscale_batches")
        self.lock = threading.RLock()
        self.events: dict[str, threading.Event] = {}
        self.threads: dict[str, threading.Thread] = {}
        self.stopping = False

    def _folders(self) -> tuple[Path, Path]:
        root = self.single.cluster.config_store.directory.resolve()
        source = root / "超分" / "原素材"
        output = root / "超分" / "已处理"
        if not source.is_dir() or not output.is_dir():
            raise ValueError("NAS 超分目录需包含“原素材”和“已处理”两个文件夹")
        if not source.resolve().is_relative_to(root) or not output.resolve().is_relative_to(root):
            raise ValueError("超分文件夹不能指向共享目录以外")
        return source.resolve(), output.resolve()

    def preview(self, model_name: str = "x2plus") -> dict[str, Any]:
        model_config(model_name)
        source, output = self._folders()
        items = []
        for path in sorted(source.iterdir(), key=lambda item: item.name.casefold()):
            if path.name.startswith(".") or path.suffix.lower() not in SUFFIXES or not path.is_file():
                continue
            if not path.resolve().is_relative_to(source):
                raise ValueError(f"原素材中包含指向目录外的视频：{path.name}")
            stem = path.stem + ("_SR2x_原尺寸" if model_name == "x2plus" else f"_SR_{model_name}_原尺寸")
            target = output / f"{stem}.mp4"
            if target.exists():
                items.append({"name": path.name, "source": str(path), "status": "skipped",
                              "output_path": str(target), "frames": 0})
                continue
            try:
                info = probe_video(path)
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                items.append({"name": path.name, "source": str(path), "status": "invalid",
                              "error": str(exc), "frames": 0})
                continue
            items.append({"name": path.name, "source": info["source"], "status": "pending",
                          "output_path": str(target), "frames": info["frames"],
                          "width": info["width"], "height": info["height"], "fps": info["fps"],
                          "size_bytes": info["size_bytes"], "modified_ns": info["modified_ns"]})
        return {"source_directory": str(source), "output_directory": str(output), "model": model_name,
                "pending_count": sum(item["status"] == "pending" for item in items),
                "skipped_count": sum(item["status"] == "skipped" for item in items),
                "invalid_count": sum(item["status"] == "invalid" for item in items), "items": items}

    def create(self, model_name: str = "x2plus") -> dict[str, Any]:
        preview = self.preview(model_name)
        if preview["invalid_count"]:
            raise ValueError("原素材中有无法处理的视频，请查看预检结果并移出或修复")
        if not preview["pending_count"]:
            raise ValueError("原素材中没有待处理视频")
        expected_sha = model_config(model_name)["sha256"]
        if not any(node["online"] and node["video_upscale"].get("protocol") == 2
                   and node["video_upscale"].get("models", {}).get(model_name, {}).get("available")
                   and node["video_upscale"]["models"][model_name].get("model_sha256") == expected_sha
                   for node in self.single.nodes()):
            raise ValueError(f"没有安装相同 {model_name} 模型的在线工作机")
        with self.lock:
            if self.stopping or any(thread.is_alive() for thread in self.threads.values()):
                raise ValueError("已有集群超分批次运行")
            job_id = uuid.uuid4().hex
            items = [{**item, "job_id": None, "processed_frames": 0, "error": None}
                     for item in preview["items"] if item["status"] == "pending"]
            job = {"id": job_id, "mode": "cluster", "kind": "batch", "model": model_name,
                   "scale": model_config(model_name)["scale"],
                   "status": "queued", "phase": "queued", "source": preview["source_directory"],
                   "output_directory": preview["output_directory"], "items": items,
                   "processed_frames": 0, "total_frames": sum(item["frames"] for item in items),
                   "total_files": len(items), "completed_files": 0, "failed_files": 0,
                   "created_at": time.time(), "finished_at": None, "error": None}
            self.database.save("upscale_batches", job)
            self._launch(job_id)
            return copy.deepcopy(job)

    def _launch(self, job_id: str) -> None:
        event = threading.Event()
        self.events[job_id] = event
        thread = threading.Thread(target=self._run, args=(job_id, event), daemon=True,
                                  name=f"cluster-upscale-batch-{job_id[:8]}")
        self.threads[job_id] = thread
        thread.start()

    def resume_interrupted(self) -> None:
        with self.lock:
            for job in self.database.list("upscale_batches"):
                if job["status"] == "interrupted":
                    self._launch(job["id"])

    def _save(self, job: dict[str, Any]) -> None:
        job["processed_frames"] = sum(item["frames"] if item["status"] == "completed"
                                       else item.get("processed_frames", 0) for item in job["items"])
        job["completed_files"] = sum(item["status"] == "completed" for item in job["items"])
        job["failed_files"] = sum(item["status"] == "failed" for item in job["items"])
        self.database.save("upscale_batches", job)

    def get(self, job_id: str) -> dict[str, Any]:
        job = self.database.get("upscale_batches", job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def latest(self) -> dict[str, Any] | None:
        rows = self.database.list("upscale_batches")
        return rows[0] if rows else None

    def cancel(self, job_id: str) -> dict[str, Any]:
        event = self.events.get(job_id)
        if event is None:
            raise ValueError("集群超分批次没有运行")
        event.set()
        return self.get(job_id)

    def _run(self, job_id: str, cancel: threading.Event) -> None:
        job = self.database.get("upscale_batches", job_id)
        assert job is not None
        try:
            job.update(status="running", phase="processing")
            self._save(job)
            for item in job["items"]:
                if cancel.is_set() or self.stopping:
                    break
                if item["status"] == "completed":
                    if Path(item["output_path"]).is_file():
                        continue
                    item.update(status="pending", job_id=None, processed_frames=0)
                source = Path(item["source"])
                if not source.is_file() or source.stat().st_size != item["size_bytes"] or source.stat().st_mtime_ns != item["modified_ns"]:
                    item.update(status="failed", error="源视频在预检后发生变化", processed_frames=0)
                    self._save(job)
                    continue
                if item["job_id"]:
                    try:
                        child = self.single.get(item["job_id"])
                    except KeyError:
                        child = None
                else:
                    child = None
                if child is None or child["status"] in {"failed", "cancelled"}:
                    while self.single.active_count() and not cancel.is_set() and not self.stopping:
                        cancel.wait(0.2)
                    if cancel.is_set() or self.stopping:
                        break
                    child = self.single.create(item["source"], job["output_directory"], job["model"])
                    item.update(job_id=child["id"], status="running", processed_frames=0, error=None)
                    self._save(job)
                while child["status"] not in {"completed", "failed", "cancelled"} and not cancel.is_set() and not self.stopping:
                    item["processed_frames"] = child["processed_frames"]
                    self._save(job)
                    cancel.wait(0.5)
                    child = self.single.get(item["job_id"])
                if cancel.is_set() or self.stopping:
                    if child["status"] in {"queued", "running"}:
                        try:
                            self.single.cancel(child["id"])
                        except ValueError:
                            pass
                    break
                item.update(status="completed" if child["status"] == "completed" else "failed",
                            processed_frames=child["processed_frames"],
                            output_path=child["output_path"], error=child.get("error"))
                self._save(job)
            if self.stopping:
                job.update(status="interrupted", phase="finished", error="应用正在退出")
            elif cancel.is_set():
                for item in job["items"]:
                    if item["status"] in {"pending", "running"}:
                        item.update(status="cancelled", processed_frames=0)
                job.update(status="cancelled", phase="finished", error="批次已取消")
            elif job["failed_files"]:
                job.update(status="partial_failed" if job["completed_files"] else "failed", phase="finished")
            else:
                job.update(status="completed", phase="completed")
        except Exception as exc:
            job.update(status="interrupted" if self.stopping else "failed", phase="finished", error=str(exc))
        finally:
            job["finished_at"] = None if job["status"] == "interrupted" else time.time()
            self._save(job)
            with self.lock:
                self.events.pop(job_id, None)
                self.threads.pop(job_id, None)

    def active_count(self) -> int:
        return sum(thread.is_alive() for thread in self.threads.values())

    def shutdown(self) -> None:
        self.stopping = True
        for event in self.events.values():
            event.set()
        for thread in list(self.threads.values()):
            thread.join(timeout=5)
