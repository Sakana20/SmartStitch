from __future__ import annotations

import os
import queue
import subprocess
import threading
from pathlib import Path
from typing import Callable

from .models import AppConfig, Asset, PlanItem


class RenderError(RuntimeError):
    pass


ProgressCallback = Callable[[float], None]


def _escape_filter_value(value: str) -> str:
    return value.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")


def _video_filter(index: int, config: AppConfig) -> str:
    output = config.output
    if output.resize_mode == "fit_pad":
        resize = (
            f"scale={output.width}:{output.height}:force_original_aspect_ratio=decrease,"
            f"pad={output.width}:{output.height}:(ow-iw)/2:(oh-ih)/2:color={output.background_color}"
        )
    elif output.resize_mode == "fill_crop":
        resize = (
            f"scale={output.width}:{output.height}:force_original_aspect_ratio=increase,"
            f"crop={output.width}:{output.height}"
        )
    else:
        resize = f"scale={output.width}:{output.height}"
    return (
        f"[{index}:v]{resize},fps={output.fps},setsar=1,"
        f"format={output.pixel_format},setpts=PTS-STARTPTS[v{index}]"
    )


def _audio_filter(index: int, asset: Asset, config: AppConfig) -> str:
    output = config.output
    duration = asset.probe.duration if asset.probe else 0
    layout = "mono" if output.audio_channels == 1 else "stereo"
    if asset.probe and asset.probe.has_audio:
        return (
            f"[{index}:a]aresample={output.audio_sample_rate},"
            f"aformat=channel_layouts={layout},asetpts=PTS-STARTPTS[a{index}]"
        )
    return (
        f"anullsrc=r={output.audio_sample_rate}:cl={layout},"
        f"atrim=duration={duration:.6f},asetpts=PTS-STARTPTS[a{index}]"
    )


def _overlay_range(config: AppConfig, timeline: list[tuple[str, Asset]]) -> tuple[float, float]:
    timing = config.benefit_overlays.timing
    total = sum(asset.probe.duration for _, asset in timeline if asset.probe)
    if timing.scope == "full":
        return 0, total
    if timing.scope == "custom":
        return timing.start_seconds, timing.end_seconds if timing.end_seconds is not None else total

    elapsed = 0.0
    ranges: dict[str, tuple[float, float]] = {}
    for category, asset in timeline:
        duration = asset.probe.duration if asset.probe else 0
        ranges[category] = (elapsed, elapsed + duration)
        elapsed += duration
    if timing.scope == "benefit_video":
        return ranges.get("benefit_video", (0, total))

    main_categories = [item for item in timeline if item[0] not in {"pre_roll", "end_card"}]
    if not main_categories:
        return 0, total
    return ranges[main_categories[0][0]][0], ranges[main_categories[-1][0]][1]


def build_ffmpeg_command(
    config: AppConfig,
    item: PlanItem,
    output_path: Path,
) -> tuple[list[str], float]:
    timeline: list[tuple[str, Asset]] = []
    for category in config.timeline:
        asset = item.selections.get(category)
        if asset is not None:
            timeline.append((category, asset))
    if not timeline:
        raise RenderError("时间线为空")

    command = ["ffmpeg", "-hide_banner", "-y"]
    for category, asset in timeline:
        if asset.media_type == "image":
            duration = asset.probe.duration if asset.probe else config.sources[category].image_duration_seconds
            command.extend(["-loop", "1", "-t", f"{duration:.6f}", "-i", asset.path])
        else:
            command.extend(["-i", asset.path])

    overlay_index: int | None = None
    if item.overlay is not None:
        overlay_index = len(timeline)
        command.extend(["-loop", "1", "-i", item.overlay.path])

    filters: list[str] = []
    for index, (_, asset) in enumerate(timeline):
        filters.append(_video_filter(index, config))
        filters.append(_audio_filter(index, asset, config))
    concat_inputs = "".join(f"[v{index}][a{index}]" for index in range(len(timeline)))
    filters.append(f"{concat_inputs}concat=n={len(timeline)}:v=1:a=1[basev][outa]")

    video_map = "[basev]"
    if overlay_index is not None:
        placement = config.benefit_overlays.placement
        if placement.scale_mode == "stretch":
            overlay_scale = f"scale={config.output.width}:{config.output.height}"
        elif placement.scale_mode == "fit":
            overlay_scale = (
                f"scale={config.output.width}:{config.output.height}:"
                "force_original_aspect_ratio=decrease"
            )
        elif placement.shrink_if_oversized:
            overlay_scale = (
                f"scale='min(iw,{config.output.width})':'min(ih,{config.output.height})':"
                "force_original_aspect_ratio=decrease"
            )
        else:
            overlay_scale = "null"
        opacity = placement.opacity
        filters.append(
            f"[{overlay_index}:v]{overlay_scale},format=rgba,colorchannelmixer=aa={opacity:.6f}[overlay]"
        )
        start, end = _overlay_range(config, timeline)
        x = _escape_filter_value(placement.x)
        y = _escape_filter_value(placement.y)
        filters.append(
            f"[basev][overlay]overlay=x='{x}':y='{y}':"
            f"enable='between(t,{start:.6f},{end:.6f})':eof_action=pass[outv]"
        )
        video_map = "[outv]"

    command.extend(
        [
            "-filter_complex",
            ";".join(filters),
            "-map",
            video_map,
            "-map",
            "[outa]",
            "-c:v",
            config.output.video_codec,
            "-preset",
            config.output.video_preset,
            "-crf",
            str(config.output.crf),
            "-pix_fmt",
            config.output.pixel_format,
            "-fps_mode",
            "cfr",
            "-r",
            str(config.output.fps),
            "-c:a",
            config.output.audio_codec,
            "-b:a",
            config.output.audio_bitrate,
            "-ar",
            str(config.output.audio_sample_rate),
            "-ac",
            str(config.output.audio_channels),
        ]
    )
    if config.output.faststart:
        command.extend(["-movflags", "+faststart"])
    command.extend(["-progress", "pipe:1", "-nostats", str(output_path)])
    return command, item.estimated_duration


def _stderr_reader(stream: object, sink: queue.Queue[str]) -> None:
    try:
        for line in iter(stream.readline, ""):
            sink.put(line.rstrip())
    finally:
        stream.close()


def render_item(
    config: AppConfig,
    item: PlanItem,
    output_path: Path,
    cancel_event: threading.Event,
    on_progress: ProgressCallback | None = None,
    process_callback: Callable[[subprocess.Popen[str] | None], None] | None = None,
) -> dict[str, object]:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = output_path.with_name(f".{output_path.stem}.{os.getpid()}.part.mp4")
    command, duration = build_ffmpeg_command(config, item, temp_path)
    stderr_lines: queue.Queue[str] = queue.Queue()
    process = subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    if process_callback:
        process_callback(process)
    stderr_thread = threading.Thread(
        target=_stderr_reader,
        args=(process.stderr, stderr_lines),
        daemon=True,
    )
    stderr_thread.start()
    cancelled = False
    try:
        assert process.stdout is not None
        for raw_line in process.stdout:
            if cancel_event.is_set() and process.poll() is None:
                cancelled = True
                process.terminate()
            key, _, value = raw_line.strip().partition("=")
            if key in {"out_time_us", "out_time_ms"} and duration > 0:
                try:
                    microseconds = int(value)
                    progress = min(0.99, microseconds / 1_000_000 / duration)
                    if on_progress:
                        on_progress(progress)
                except ValueError:
                    pass
        try:
            return_code = process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            return_code = process.wait()
    finally:
        if process_callback:
            process_callback(None)
        stderr_thread.join(timeout=2)

    captured: list[str] = []
    while not stderr_lines.empty():
        captured.append(stderr_lines.get())
    stderr_tail = "\n".join(captured[-40:])
    if cancelled or cancel_event.is_set():
        temp_path.unlink(missing_ok=True)
        raise RenderError("任务已取消")
    if return_code != 0:
        temp_path.unlink(missing_ok=True)
        raise RenderError(stderr_tail or f"FFmpeg 退出码 {return_code}")

    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(temp_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        temp_path.unlink(missing_ok=True)
        raise RenderError(probe.stderr.strip() or "输出文件校验失败")
    actual_duration = float(probe.stdout.strip())
    if actual_duration <= 0 or abs(actual_duration - duration) > max(1.0, duration * 0.03):
        temp_path.unlink(missing_ok=True)
        raise RenderError(f"输出时长异常: 预计 {duration:.3f}s，实际 {actual_duration:.3f}s")
    if output_path.exists():
        output_path.unlink()
    temp_path.replace(output_path)
    if on_progress:
        on_progress(1.0)
    return {
        "output_path": str(output_path),
        "actual_duration": actual_duration,
        "ffmpeg_command": command,
    }
