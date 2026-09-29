"""VFR-only encoding and validation. CFR keeps its original FFmpeg pipeline."""
from __future__ import annotations

import shutil
import subprocess
import tempfile
from fractions import Fraction
from pathlib import Path


def segment_timing(info, start, end):
    timing = info["timing"]
    pts = timing["pts"]
    origin = pts[start]
    last = pts[end] - pts[end - 1] if end < len(pts) else timing["last_duration"]
    return {"time_base": timing["time_base"], "pts": [value - origin for value in pts[start:end]],
            "last_duration": last, "start_pts": 0, "variable": True}


def segment_duration(info, start, end):
    timing = segment_timing(info, start, end)
    return (timing["pts"][-1] + timing["last_duration"]) * Fraction(timing["time_base"])


def verify_vfr_video(path, info, start, end, *, audio=False, final=False):
    from .video_upscale import probe_video
    actual = probe_video(path, force_timestamps=True)
    if (actual["width"], actual["height"], actual["frames"], actual["has_audio"]) != (
            info["width"], info["height"], end - start, audio):
        raise RuntimeError("VFR 超分尺寸、帧数或音轨校验失败")
    expected = segment_timing(info, start, end)
    timing = actual["timing"]
    source_base, output_base = Fraction(expected["time_base"]), Fraction(timing["time_base"])
    tolerance = max(source_base, output_base)
    expected_start = info["timing"]["start_pts"] * source_base if final else 0
    if abs(timing["start_pts"] * output_base - expected_start) > tolerance:
        raise RuntimeError("VFR 超分起始时间戳校验失败")
    for before, after in zip(expected["pts"], timing["pts"], strict=True):
        if abs(before * source_base - after * output_base) > tolerance:
            raise RuntimeError("VFR 超分帧时间戳校验失败")
    if abs(expected["last_duration"] * source_base - timing["last_duration"] * output_base) > tolerance:
        raise RuntimeError("VFR 超分末帧时长校验失败")


def process_vfr_segment(source, output, info, start, end, data_directory, cancelled,
                        progress, process_callback, model_name):
    import av
    import numpy as np
    from .video_upscale import _load_model, _upscale_frame, _stop_process, model_config

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ValueError("找不到 FFmpeg")
    timing = segment_timing(info, start, end)
    time_base = Fraction(timing["time_base"])
    durations = [b - a for a, b in zip(timing["pts"], timing["pts"][1:])] + [timing["last_duration"]]
    duration_by_pts = dict(zip(timing["pts"], durations, strict=True))
    model, input_key, output_key = _load_model(data_directory, model_name)
    width, height = info["width"], info["height"]
    frame_bytes = width * height * 3
    output.parent.mkdir(parents=True, exist_ok=True)
    decoder = None
    completed = 0
    try:
        with tempfile.TemporaryFile() as log, av.open(str(output), "w", options={
                "movflags": "+faststart", "video_track_timescale": str(time_base.denominator)}) as container:
            stream = container.add_stream("libx264", rate=Fraction(info["fps"]).limit_denominator(1001))
            stream.width, stream.height, stream.pix_fmt = width, height, "yuv420p"
            stream.time_base = stream.codec_context.time_base = time_base
            stream.options = {"crf": "18", "preset": "medium", "bf": "0"}

            def mux(packets):
                for packet in packets:
                    if cancelled.is_set():
                        raise RuntimeError("任务已取消")
                    pts = Fraction(packet.pts) * packet.time_base / time_base
                    if pts.denominator != 1 or int(pts) not in duration_by_pts:
                        raise RuntimeError("VFR 编码时间戳发生变化")
                    packet.duration = int(duration_by_pts[int(pts)] * time_base / packet.time_base)
                    container.mux(packet)

            decoder = subprocess.Popen(
                [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", str(source),
                 "-vf", f"select='gte(n,{start})*lt(n,{end})'", "-vsync", "0",
                 "-frames:v", str(end - start), "-an", "-sn", "-dn",
                 "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
                stdout=subprocess.PIPE, stderr=log)
            if process_callback:
                process_callback(decoder)
            try:
                for pts in timing["pts"]:
                    if cancelled.is_set():
                        raise RuntimeError("任务已取消")
                    chunks = bytearray()
                    while len(chunks) < frame_bytes:
                        part = decoder.stdout.read(frame_bytes - len(chunks))
                        if not part:
                            raise RuntimeError("VFR 解码提前结束")
                        chunks.extend(part)
                    frame = np.frombuffer(chunks, dtype=np.uint8).reshape(height, width, 3)
                    enhanced = _upscale_frame(frame, model, input_key, output_key, cancelled,
                                               model_config(model_name)["scale"])
                    image = av.VideoFrame.from_ndarray(np.frombuffer(enhanced, dtype=np.uint8).reshape(height, width, 3), format="rgb24")
                    image.pts, image.time_base = pts, time_base
                    mux(stream.encode(image))
                    completed += 1
                    if progress:
                        progress(completed)
                mux(stream.encode(None))
                if decoder.wait(timeout=30):
                    log.seek(0)
                    raise RuntimeError(log.read().decode(errors="replace")[-800:] or "VFR 解码失败")
            finally:
                decoder.stdout.close()
                _stop_process(decoder)
        verify_vfr_video(output, info, start, end)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    finally:
        if process_callback:
            process_callback(None)
    return {"frames": completed, "output_path": str(output), "size_bytes": output.stat().st_size}
