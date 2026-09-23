"""Frame-accurate Real-ESRGAN CoreML x2 upscaling for local and cluster jobs."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import platform
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import zipfile
from datetime import UTC, datetime
from fractions import Fraction
from pathlib import Path
from typing import Any, Callable
from urllib.request import urlopen


MODEL_CONFIGS = {
    "x2plus": {"scale": 2, "sha256": "4acf0afa2828e82b88e1d712b4b7604977698c99aa1505276c943922be6b3938"},
    "animevideo": {"scale": 4, "sha256": "0e83446f03b4c0a60c970da02d70d10af14949c169714f9ad8e8ea3e479b145b"},
}
MODEL_ZIP_SHA256 = MODEL_CONFIGS["x2plus"]["sha256"]
MODEL_RELEASE_URL = "https://github.com/hanxiao/real-esrgan-coreml/releases/download/v1.0.0"
TILE = 512
OVERLAP = 32
PRE_PAD = 10
SUFFIXES = {".mp4", ".mov", ".m4v", ".mkv"}


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def model_config(model_name: str) -> dict[str, Any]:
    if model_name not in MODEL_CONFIGS:
        raise ValueError(f"不支持的超分模型：{model_name}")
    return MODEL_CONFIGS[model_name]


def model_asset_name(model_name: str) -> str:
    model_config(model_name)
    return f"RealESRGAN_{model_name}_522_fp16"


def model_directory(data_directory: Path, model_name: str = "x2plus") -> Path:
    override = os.environ.get("SMARTSTITCH_UPSCALE_MODEL", "").strip()
    return Path(override).expanduser().resolve() if override and model_name == "x2plus" else data_directory / "models" / f"{model_asset_name(model_name)}.mlpackage"


def model_status(data_directory: Path, model_name: str = "x2plus") -> dict[str, Any]:
    config = model_config(model_name)
    path = model_directory(data_directory, model_name)
    available = (path / "Manifest.json").is_file() and (
        path / "Data/com.apple.CoreML/model.mlmodel"
    ).is_file()
    marker = path.parent / f"{model_asset_name(model_name)}.sha256"
    if not (os.environ.get("SMARTSTITCH_UPSCALE_MODEL") and model_name == "x2plus"):
        available = available and marker.is_file() and marker.read_text().strip() == config["sha256"]
    return {"available": available, "model": model_name, "scale": config["scale"], "model_sha256": config["sha256"],
            "platform_supported": platform.system() == "Darwin" and platform.machine() == "arm64"}


def prepare_model(data_directory: Path, zip_source: Path | None = None, *, model_name: str = "x2plus") -> dict[str, Any]:
    """Install a verified release asset in writable app data; never run torch conversion."""
    config = model_config(model_name)
    asset_name = model_asset_name(model_name)
    if not model_status(data_directory, model_name)["platform_supported"]:
        raise ValueError("超分模型仅支持 Apple Silicon Mac")
    if model_status(data_directory, model_name)["available"]:
        return model_status(data_directory, model_name)
    target = model_directory(data_directory, model_name)
    if os.environ.get("SMARTSTITCH_UPSCALE_MODEL") and model_name == "x2plus":
        raise ValueError("指定的超分模型目录不存在或不完整")
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smartstitch-model-", dir=target.parent) as directory:
        temporary = Path(directory)
        archive = temporary / "model.zip"
        if zip_source is None:
            with urlopen(f"{MODEL_RELEASE_URL}/{asset_name}.zip", timeout=30) as response, archive.open("wb") as output:
                shutil.copyfileobj(response, output)
        else:
            shutil.copy2(zip_source, archive)
        if _sha256(archive) != config["sha256"]:
            raise ValueError("模型 SHA-256 不匹配，已拒绝安装")
        with zipfile.ZipFile(archive) as package:
            members = package.infolist()
            expected = asset_name + ".mlpackage/"
            if not members or any(
                not member.filename.startswith(expected)
                or ".." in Path(member.filename).parts
                or (not member.is_dir() and (member.external_attr >> 16) & 0o170000 == 0o120000)
                for member in members
            ):
                raise ValueError("模型压缩包包含无效路径")
            package.extractall(temporary / "unpacked")
        unpacked = temporary / "unpacked" / f"{asset_name}.mlpackage"
        if not (unpacked / "Manifest.json").is_file():
            raise ValueError("模型压缩包缺少 Manifest.json")
        if target.exists():
            shutil.rmtree(target)
        os.replace(unpacked, target)
        (target.parent / f"{asset_name}.sha256").write_text(config["sha256"], encoding="utf-8")
    return model_status(data_directory, model_name)


def probe_video(source: Path) -> dict[str, Any]:
    source = source.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in SUFFIXES:
        raise ValueError("请选择 MP4、MOV、M4V 或 MKV 视频文件")
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise ValueError("找不到 FFprobe")
    command = [ffprobe, "-v", "error", "-count_frames", "-show_entries",
               "format=duration:stream=index,codec_type,codec_name,width,height,pix_fmt,"
               "r_frame_rate,avg_frame_rate,nb_read_frames,nb_frames,color_transfer:"
               "stream_side_data=rotation", "-of", "json", str(source)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, check=False)
    if result.returncode:
        raise ValueError((result.stderr or "无法读取视频")[-800:])
    payload = json.loads(result.stdout)
    videos = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "video"]
    audios = [stream for stream in payload.get("streams", []) if stream.get("codec_type") == "audio"]
    if len(videos) != 1:
        raise ValueError("首版超分只支持单视频轨")
    video = videos[0]
    width, height = int(video["width"]), int(video["height"])
    if width % 2 or height % 2:
        raise ValueError("视频宽高需为偶数，才能保持原尺寸输出 H.264 MP4")
    if video.get("pix_fmt") != "yuv420p":
        raise ValueError("首版仅支持普通 8-bit yuv420p 视频")
    if video.get("color_transfer") in {"smpte2084", "arib-std-b67"}:
        raise ValueError("首版不支持 HDR 视频")
    if any(int(side.get("rotation", 0)) for side in video.get("side_data_list", [])):
        raise ValueError("首版暂不支持旋转元数据视频")
    fps = Fraction(video.get("avg_frame_rate", "0/1"))
    if fps <= 0 or fps != Fraction(video.get("r_frame_rate", "0/1")):
        raise ValueError("首版只支持恒定帧率视频")
    frames = int(video.get("nb_read_frames") or video.get("nb_frames") or 0)
    if frames <= 0:
        raise ValueError("无法确定视频帧数")
    return {"source": str(source), "width": width, "height": height,
            "fps": f"{fps.numerator}/{fps.denominator}", "frames": frames,
            "duration": frames / float(fps), "has_audio": bool(audios),
            "audio_codec": audios[0].get("codec_name") if audios else None,
            "size_bytes": source.stat().st_size, "modified_ns": source.stat().st_mtime_ns}


def _starts(length: int) -> list[int]:
    if length <= TILE:
        return [0]
    result = []
    position = 0
    while position < length:
        if position + TILE >= length:
            result.append(length - TILE)
            break
        result.append(position)
        position += TILE - OVERLAP
    return result


def _load_model(data_directory: Path, model_name: str = "x2plus"):
    try:
        import coremltools as ct
    except ImportError as exc:
        raise ValueError("未安装 coremltools，无法运行超分") from exc
    if not model_status(data_directory, model_name)["available"]:
        raise ValueError("超分模型尚未准备，请先下载模型")
    model = ct.models.MLModel(str(model_directory(data_directory, model_name)), compute_units=ct.ComputeUnit.ALL)
    spec = model.get_spec()
    scale = model_config(model_name)["scale"]
    if list(spec.description.input[0].type.multiArrayType.shape) != [1, 3, 522, 522] or list(
        spec.description.output[0].type.multiArrayType.shape
    ) != [1, 3, 522 * scale, 522 * scale]:
        raise ValueError(f"超分模型输入输出形状与 {model_name} 版本不符")
    return model, spec.description.input[0].name, spec.description.output[0].name


def _upscale_frame(frame, model, input_key: str, output_key: str, cancelled: threading.Event, scale: int = 2):
    import numpy as np
    from PIL import Image

    height, width = frame.shape[:2]
    rgb = frame.astype(np.float32) / 255.0
    # 4x models are reduced per tile so a 1080p frame does not need a giant 4x float buffer.
    accumulation_scale = 2 if scale == 2 else 1
    output = np.zeros((height * accumulation_scale, width * accumulation_scale, 3), dtype=np.float32)
    weights = np.zeros((height * accumulation_scale, width * accumulation_scale, 1), dtype=np.float32)
    for y in _starts(height):
        for x in _starts(width):
            if cancelled.is_set():
                raise RuntimeError("任务已取消")
            tile = rgb[y:min(y + TILE, height), x:min(x + TILE, width)]
            th, tw = tile.shape[:2]
            tile = np.pad(tile, ((0, TILE + PRE_PAD - th), (0, TILE + PRE_PAD - tw), (0, 0)), mode="reflect")
            tensor = tile.transpose(2, 0, 1)[None].astype(np.float32)
            predicted = model.predict({input_key: tensor})[output_key][0]
            enlarged = np.clip(predicted.transpose(1, 2, 0)[:th * scale, :tw * scale], 0, 1)
            if scale == 4:
                tile_image = Image.fromarray((enlarged * 255).astype(np.uint8), "RGB")
                enlarged = np.asarray(tile_image.resize((tw, th), Image.Resampling.LANCZOS), dtype=np.float32) / 255
            ah, aw = th * accumulation_scale, tw * accumulation_scale
            blend = np.ones((ah, aw, 1), dtype=np.float32)
            ramp_size = min(OVERLAP * accumulation_scale, ah, aw)
            ramp = np.linspace(0, 1, ramp_size, dtype=np.float32)
            if y > 0:
                blend[:ramp_size] *= ramp[:, None, None]
            if y + th < height:
                blend[-ramp_size:] *= ramp[::-1, None, None]
            if x > 0:
                blend[:, :ramp_size] *= ramp[None, :, None]
            if x + tw < width:
                blend[:, -ramp_size:] *= ramp[None, ::-1, None]
            oy, ox = y * accumulation_scale, x * accumulation_scale
            output[oy:oy + ah, ox:ox + aw] += enlarged * blend
            weights[oy:oy + ah, ox:ox + aw] += blend
    output = np.clip(output / np.maximum(weights, 1e-8) * 255, 0, 255).astype(np.uint8)
    image = Image.fromarray(output, "RGB")
    return (image.resize((width, height), Image.Resampling.LANCZOS) if scale == 2 else image).tobytes()


def _terminate(process: subprocess.Popen[bytes] | None) -> None:
    if process and process.poll() is None:
        process.terminate()


def _stop_process(process: subprocess.Popen[bytes]) -> None:
    _terminate(process)
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def process_segment(
    source: Path, output: Path, info: dict[str, Any], start: int, end: int,
    data_directory: Path, cancelled: threading.Event,
    progress: Callable[[int], None] | None = None,
    process_callback: Callable[[subprocess.Popen[bytes] | None], None] | None = None,
    model_name: str = "x2plus",
) -> dict[str, Any]:
    """Write one silent, exact frame range with fixed video encoding parameters."""
    import numpy as np

    if not (0 <= start < end <= info["frames"]):
        raise ValueError("超分帧范围无效")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("找不到 FFmpeg")
    scale = model_config(model_name)["scale"]
    model, input_key, output_key = _load_model(data_directory, model_name)
    width, height = info["width"], info["height"]
    frame_bytes = width * height * 3
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="smartstitch-sr-logs-") as logdir:
        with (Path(logdir) / "decoder.log").open("wb") as decode_log, (
            Path(logdir) / "encoder.log"
        ).open("wb") as encode_log:
            decoder = subprocess.Popen(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
                 "-vf", f"select='gte(n,{start})*lt(n,{end})'", "-vsync", "0",
                 "-frames:v", str(end - start), "-an", "-sn", "-dn",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                stdout=subprocess.PIPE, stderr=decode_log,
            )
            encoder = subprocess.Popen(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "rawvideo",
                 "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-r", info["fps"],
                 "-i", "pipe:0", "-an", "-c:v", "libx264", "-crf", "18",
                 "-preset", "medium", "-pix_fmt", "yuv420p", "-bf", "0",
                 "-movflags", "+faststart", str(output)],
                stdin=subprocess.PIPE, stderr=encode_log,
            )
            if process_callback:
                process_callback(decoder)
            completed = 0
            try:
                assert decoder.stdout and encoder.stdin
                for _ in range(start, end):
                    if cancelled.is_set():
                        raise RuntimeError("任务已取消")
                    chunks = bytearray()
                    while len(chunks) < frame_bytes:
                        part = decoder.stdout.read(frame_bytes - len(chunks))
                        if not part:
                            raise RuntimeError(f"解码提前结束，仅处理 {completed}/{end-start} 帧")
                        chunks.extend(part)
                    frame = np.frombuffer(chunks, dtype=np.uint8).reshape(height, width, 3)
                    encoder.stdin.write(_upscale_frame(frame, model, input_key, output_key, cancelled, scale))
                    completed += 1
                    if progress:
                        progress(completed)
                encoder.stdin.close()
                decoder.stdout.close()
                if decoder.wait(timeout=30) or encoder.wait(timeout=120):
                    raise RuntimeError("FFmpeg 解码或编码失败")
            except Exception:
                _terminate(decoder)
                _terminate(encoder)
                if decoder.stdout:
                    decoder.stdout.close()
                if encoder.stdin and not encoder.stdin.closed:
                    encoder.stdin.close()
                _stop_process(decoder)
                _stop_process(encoder)
                output.unlink(missing_ok=True)
                raise
            finally:
                if process_callback:
                    process_callback(None)
            if not output.is_file() or output.stat().st_size == 0:
                raise RuntimeError("超分片段未生成")
    return {"frames": completed, "output_path": str(output), "size_bytes": output.stat().st_size}


def mux_audio(silent: Path, source: Path, target: Path, info: dict[str, Any]) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("找不到 FFmpeg")
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-i", str(silent),
               "-i", str(source), "-map", "0:v:0", "-map", "1:a:0?", "-c:v", "copy",
               "-c:a", "aac", "-b:a", "192k", "-t", f"{info['duration']:.9f}",
               "-movflags", "+faststart", str(target)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
    if result.returncode:
        raise RuntimeError((result.stderr or "合并音频失败")[-1200:])


class VideoUpscaleManager:
    def __init__(self, data_directory: Path) -> None:
        self.data_directory = data_directory
        self.lock = threading.RLock()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.latest_id: str | None = None
        self.active_id: str | None = None
        self.worker: threading.Thread | None = None
        self.cancel_event = threading.Event()
        self.process: subprocess.Popen[bytes] | None = None
        self.stopping = False

    def preview(self, source: str, model_name: str = "x2plus") -> dict[str, Any]:
        return {**probe_video(Path(source)), "model": model_status(self.data_directory, model_name)}

    def prepare_model(self, zip_source: Path | None = None, *, model_name: str = "x2plus") -> dict[str, Any]:
        return prepare_model(self.data_directory, zip_source, model_name=model_name)

    def create(self, source: str, output_directory: str | None = None, model_name: str = "x2plus") -> dict[str, Any]:
        info = probe_video(Path(source))
        config = model_config(model_name)
        if not model_status(self.data_directory, model_name)["available"]:
            raise ValueError(f"{model_name} 模型未准备，请先在面板下载模型")
        directory = Path(output_directory).expanduser().resolve() if output_directory else Path(info["source"]).parent
        if not directory.is_dir():
            raise ValueError("输出文件夹不存在")
        with self.lock:
            if self.stopping or self.active_id:
                raise ValueError("已有超分任务运行，或应用正在退出")
            stem = Path(info["source"]).stem + ("_SR2x_原尺寸" if model_name == "x2plus" else f"_SR_{model_name}_原尺寸")
            target = directory / f"{stem}.mp4"
            number = 2
            while target.exists():
                target = directory / f"{stem}_{number}.mp4"
                number += 1
            job_id = uuid.uuid4().hex
            job = {"id": job_id, "status": "queued", "phase": "queued", "source": info["source"],
                   "model": model_name, "scale": config["scale"],
                   "width": info["width"], "height": info["height"], "fps": info["fps"],
                   "output_path": str(target), "processed_frames": 0, "total_frames": info["frames"],
                   "created_at": _now(), "finished_at": None, "error": None}
            self.jobs[job_id] = job
            self.latest_id = self.active_id = job_id
            self.cancel_event = threading.Event()
            self.worker = threading.Thread(target=self._run, args=(job_id, info, target, self.cancel_event, model_name), daemon=True)
            self.worker.start()
            return copy.deepcopy(job)

    def _run(self, job_id: str, info: dict[str, Any], target: Path, cancelled: threading.Event, model_name: str) -> None:
        temporary = target.with_name(f".{target.stem}.{job_id}.tmp.mp4")
        try:
            source = Path(info["source"])
            if source.stat().st_size != info["size_bytes"] or source.stat().st_mtime_ns != info["modified_ns"]:
                raise RuntimeError("源视频在预检后发生变化")
            with tempfile.TemporaryDirectory(prefix="smartstitch-sr-", dir=self.data_directory) as directory:
                silent = Path(directory) / "silent.mp4"
                self._update(job_id, status="running", phase="upscaling")
                process_segment(source, silent, info, 0, info["frames"], self.data_directory,
                                cancelled, progress=lambda count: self._update(job_id, processed_frames=count),
                                process_callback=lambda process: self._set_process(process), model_name=model_name)
                if cancelled.is_set():
                    raise RuntimeError("任务已取消")
                self._update(job_id, phase="audio")
                mux_audio(silent, source, temporary, info)
                if cancelled.is_set():
                    raise RuntimeError("任务已取消")
                output_info = probe_video(temporary)
                if (output_info["width"], output_info["height"], output_info["frames"], output_info["fps"]) != (
                    info["width"], info["height"], info["frames"], info["fps"]
                ) or output_info["has_audio"] != info["has_audio"]:
                    raise RuntimeError("超分结果的尺寸、帧数或音轨校验失败")
                if target.exists():
                    raise RuntimeError("输出文件已由其他任务创建，请重新提交")
                os.replace(temporary, target)
            self._update(job_id, status="completed", phase="completed", finished_at=_now())
        except Exception as exc:
            self._update(job_id, status="cancelled" if cancelled.is_set() else "failed",
                         phase="finished", error=str(exc), finished_at=_now())
        finally:
            temporary.unlink(missing_ok=True)
            with self.lock:
                self.process = None
                self.active_id = None

    def _set_process(self, process: subprocess.Popen[bytes] | None) -> None:
        with self.lock:
            self.process = process

    def _update(self, job_id: str, **updates: Any) -> None:
        with self.lock:
            self.jobs[job_id].update(updates)

    def get(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id not in self.jobs:
                raise KeyError(job_id)
            return copy.deepcopy(self.jobs[job_id])

    def latest(self) -> dict[str, Any] | None:
        with self.lock:
            return copy.deepcopy(self.jobs[self.latest_id]) if self.latest_id else None

    def cancel(self, job_id: str) -> dict[str, Any]:
        with self.lock:
            if job_id != self.active_id:
                raise ValueError("超分任务没有运行")
            self.cancel_event.set()
            _terminate(self.process)
            self.jobs[job_id]["phase"] = "cancelling"
            return copy.deepcopy(self.jobs[job_id])

    def active_count(self) -> int:
        with self.lock:
            return int(self.active_id is not None)

    def shutdown(self, timeout: float = 8.0) -> None:
        with self.lock:
            self.stopping = True
            self.cancel_event.set()
            _terminate(self.process)
            worker = self.worker
        if worker and worker is not threading.current_thread():
            worker.join(timeout)
