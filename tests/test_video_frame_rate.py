import json
import shutil
import subprocess
from fractions import Fraction
from types import SimpleNamespace

import pytest

from smartstitch import video_upscale as upscale


def mock_probe(tmp_path, monkeypatch, *, average="25/1", nominal="30/1", timestamps=None):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    commands = []
    video = {"codec_type": "video", "width": 64, "height": 64, "pix_fmt": "yuv420p",
             "avg_frame_rate": average, "r_frame_rate": nominal, "time_base": "1/90000",
             "nb_frames": "N/A"}

    def run(command, **kwargs):
        commands.append(command)
        if "-show_frames" in command:
            for value in timestamps:
                kwargs["stdout"].write(f"best_effort_timestamp={value}|duration={video.get('_last_duration', 3000)}\n")
            return SimpleNamespace(returncode=0, stderr="")
        assert "-count_frames" not in command, "complete detection must reuse its frame count"
        return SimpleNamespace(returncode=0, stdout=json.dumps({"streams": [video]}), stderr="")

    monkeypatch.setattr(upscale.shutil, "which", lambda _: "ffprobe")
    monkeypatch.setattr(upscale.subprocess, "run", run)
    return source, commands, video


def test_header_disagreement_falls_back_and_accepts_cfr(tmp_path, monkeypatch):
    source, commands, _ = mock_probe(tmp_path, monkeypatch, timestamps=[0, 3000, 6000, 9000])
    result = upscale.probe_video(source)
    assert result["fps"] == "30/1" and result["frames"] == 4
    assert result["frame_rate_check"] == "timestamps"
    assert len(commands) == 2


@pytest.mark.parametrize("average,nominal", [(None, None), ("0/0", "bad"), ("0/1", "0/1")])
def test_missing_or_invalid_rates_are_recovered(tmp_path, monkeypatch, average, nominal):
    source, _, _ = mock_probe(tmp_path, monkeypatch, average=average, nominal=nominal,
                              timestamps=[0, 3000, 6000])
    result = upscale.probe_video(source)
    assert result["fps"] == "30/1" and result["frames"] == 3


@pytest.mark.parametrize("pts,message", [
    ([0, 3000, 3000], "不递增"),
    ([0, "N/A", 6000], "缺失"),
    ([0], "有效帧不足"),
])
def test_unsafe_timestamps_are_rejected(tmp_path, monkeypatch, pts, message):
    source, _, _ = mock_probe(tmp_path, monkeypatch, timestamps=pts)
    with pytest.raises(ValueError, match=message):
        upscale.probe_video(source)


def test_matching_headers_keep_fast_path_and_documented_limit(tmp_path, monkeypatch):
    source, commands, video = mock_probe(tmp_path, monkeypatch, average="30/1", nominal="30/1")
    video["nb_frames"] = "90"
    result = upscale.probe_video(source)
    assert result["frame_rate_check"] == "metadata" and len(commands) == 1


def test_confirmed_vfr_enters_timestamps_path(tmp_path, monkeypatch):
    source, commands, _ = mock_probe(tmp_path, monkeypatch, timestamps=[0, 3000, 9000])
    result = upscale.probe_video(source)
    assert result["frame_rate_mode"] == "vfr" and result["frame_rate_check"] == "timestamps"
    assert result["timing"]["pts"] == [0, 3000, 9000]
    assert result["frames"] == 3 and result["duration"] == pytest.approx(12000 / 90000)
    assert len(commands) == 2


def test_fractional_rate_rounding_and_cumulative_drift(tmp_path, monkeypatch):
    ticks = [round(index * 1001 / 30) for index in range(100)]
    source, _, video = mock_probe(tmp_path, monkeypatch, average="30/1", nominal="30000/1001",
                                  timestamps=ticks)
    video["time_base"] = "1/1000"
    video["_last_duration"] = 33
    result = upscale.probe_video(source)
    assert result["fps"] == "30000/1001" and result["frames"] == 100
    # Intervals differ by just one tick, but changes in sustained frame rate
    # must not be mistaken for timestamp rounding.
    ticks[:] = [index * 33 for index in range(50)] + [49 * 33 + index * 34 for index in range(1, 51)]
    video["_last_duration"] = 34
    assert upscale.probe_video(source)["frame_rate_mode"] == "vfr"


def test_changed_last_frame_duration_also_uses_vfr(tmp_path, monkeypatch):
    source, _, video = mock_probe(tmp_path, monkeypatch, timestamps=[0, 3000])
    video["_last_duration"] = 6000
    result = upscale.probe_video(source)
    assert result["frame_rate_mode"] == "vfr" and result["timing"]["last_duration"] == 6000
    assert result["duration"] == pytest.approx(.1)


@pytest.mark.parametrize("failure", ["timeout", "error", "time_base"])
def test_failed_complete_check_never_falls_through(tmp_path, monkeypatch, failure):
    source, _, video = mock_probe(tmp_path, monkeypatch, timestamps=[0, 3000, 6000])
    if failure == "time_base":
        video["time_base"] = "0/0"
    else:
        run = upscale.subprocess.run

        def fail(command, **kwargs):
            if "-show_frames" in command:
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(command, 120)
                return SimpleNamespace(returncode=1, stderr="decode failed")
            return run(command, **kwargs)

        monkeypatch.setattr(upscale.subprocess, "run", fail)
    with pytest.raises(ValueError):
        upscale.probe_video(source)


@pytest.mark.parametrize("fps,variable", [("30", False), ("30000/1001", False), ("30", True)])
def test_real_decoded_timestamps(tmp_path, fps, variable):
    ffmpeg, ffprobe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        pytest.skip("FFmpeg tools unavailable")
    source = tmp_path / "source.mkv"
    command = [ffmpeg, "-v", "error", "-f", "lavfi", "-i", f"testsrc2=s=64x64:r={fps}:d=2"]
    if variable:
        command += ["-vf", "select='if(lt(n,30),1,not(mod(n,2)))'", "-fps_mode", "vfr"]
    subprocess.run([*command, "-c:v", "libx264", str(source)], check=True)
    data = json.loads(subprocess.check_output([ffprobe, "-v", "error", "-show_streams", "-of", "json", str(source)], text=True))
    video = data["streams"][0]
    if variable:
        with pytest.raises(ValueError, match="可变帧率"):
            upscale._check_frame_timestamps(source, ffprobe, video)
    else:
        actual, frames = upscale._check_frame_timestamps(source, ffprobe, video)
        assert actual == Fraction(fps)
        assert frames == 60
