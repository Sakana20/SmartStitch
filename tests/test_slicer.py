from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from smartstitch.config import ConfigStore
from smartstitch.library import LibraryService
from smartstitch.models import CreateLibraryRequest, TimelineSliceRequest
from smartstitch.slicer import SliceConflictError, SliceError, TimelineSlicer
from smartstitch.scanner import probe_media
from smartstitch.timeline import TimelineAnalyzer


def setup_slicer(tmp_path, runner):
    store = ConfigStore(tmp_path / "config")
    library_service = LibraryService(store)
    library_service.create(
        CreateLibraryRequest(
            new_id="slice-library",
            new_name="切片库",
            parent_directory=str(tmp_path),
            folder_name="library",
            client_request_id="library-request-1",
        )
    )
    analyzer = TimelineAnalyzer(tmp_path / "timelines")
    source = tmp_path / "原始 视频.mp4"
    source.write_bytes(b"source")
    source_stat = source.stat()
    analysis_id = "a" * 24
    review_revision = "b" * 64
    (analyzer.data_directory / f"{analysis_id}.json").write_text(
        json.dumps(
            {
                "analysis_id": analysis_id,
                "source_path": str(source),
                "source_fingerprint": {
                    "size_bytes": source_stat.st_size,
                    "modified_at_ns": source_stat.st_mtime_ns,
                },
                "fps": 25,
                "frame_count": 75,
                "review": {
                    "review_revision": review_revision,
                    "segments": [
                        {"index": 1, "start_frame": 0, "end_frame": 25},
                        {"index": 2, "start_frame": 25, "end_frame": 50},
                        {"index": 3, "start_frame": 50, "end_frame": 75},
                    ]
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return (
        TimelineSlicer(analyzer, library_service, runner=runner),
        analysis_id,
        review_revision,
        store.content_hash("slice-library"),
    )


def test_timeline_slicer_exports_every_segment_and_manifest(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [{"codec_type": "video"}]}),
            stderr="",
        )

    slicer, analysis_id, review_revision, config_hash = setup_slicer(tmp_path, runner)
    request = TimelineSliceRequest(
        analysis_id=analysis_id,
        config_id="slice-library",
        review_revision=review_revision,
        current_config_hash=config_hash,
        client_request_id="slice-request-1",
        assignments=[
            {"segment_index": 1, "category": "hook"},
            {"segment_index": 2, "category": "benefit_1"},
            {"segment_index": 3, "category": "unclassified"},
        ],
    )

    result = slicer.export(request)

    assert result["ok"] is True
    assert result["success_count"] == 3
    assert result["failure_count"] == 0
    assert all(Path(item["output_path"]).is_file() for item in result["items"])
    assert "切片素材/引子" in result["items"][0]["output_path"]
    assert "切片素材/利益点/1" in result["items"][1]["output_path"]
    assert "切片素材/未归类" in result["items"][2]["output_path"]
    assert Path(result["manifest_path"]).is_file()
    assert len([command for command in commands if command[0] == "ffmpeg"]) == 3

    repeated = slicer.export(request)
    assert repeated["idempotent"] is True
    assert len([command for command in commands if command[0] == "ffmpeg"]) == 3


def test_timeline_slicer_exports_only_confirmed_subset(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [{"codec_type": "video"}]}),
            stderr="",
        )

    slicer, analysis_id, review_revision, config_hash = setup_slicer(tmp_path, runner)
    result = slicer.export(
        TimelineSliceRequest(
            analysis_id=analysis_id,
            config_id="slice-library",
            review_revision=review_revision,
            current_config_hash=config_hash,
            client_request_id="slice-request-subset",
            assignments=[{"segment_index": 2, "category": "hook"}],
        )
    )

    assert result["ok"] is True
    assert result["success_count"] == 1
    assert [item["segment_index"] for item in result["items"]] == [2]
    assert len([command for command in commands if command[0] == "ffmpeg"]) == 1


def test_timeline_slicer_builds_one_composite_output_for_discontinuous_segments(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"composite")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if "-select_streams" in command:
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"streams": [{"codec_type": "audio"}]}),
                stderr="",
            )
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "streams": [
                        {"codec_type": "video"},
                        {"codec_type": "audio"},
                    ],
                    "format": {"duration": "2.000000"},
                }
            ),
            stderr="",
        )

    slicer, analysis_id, review_revision, config_hash = setup_slicer(tmp_path, runner)
    result = slicer.export(
        TimelineSliceRequest(
            analysis_id=analysis_id,
            config_id="slice-library",
            review_revision=review_revision,
            current_config_hash=config_hash,
            client_request_id="slice-composite-request",
            assignments=[
                {
                    "client_unit_id": "unit-composite",
                    "segment_indexes": [3, 1],
                    "category": "benefit_1",
                }
            ],
        )
    )

    assert result["success_count"] == 1
    assert result["output_unit_count"] == 1
    assert result["source_segment_count"] == 2
    item = result["items"][0]
    assert item["composite"] is True
    assert item["segment_indexes"] == [1, 3]
    assert item["excluded_gap_frames"] == 25
    assert item["total_duration_seconds"] == pytest.approx(2)
    assert "__g001_benefit-1_p001-003.mp4" in item["output_path"]
    ffmpeg_command = next(command for command in commands if command[0] == "ffmpeg")
    filter_graph = ffmpeg_command[ffmpeg_command.index("-filter_complex") + 1]
    assert "trim=start_frame=0:end_frame=25" in filter_graph
    assert "trim=start_frame=50:end_frame=75" in filter_graph
    assert "concat=n=2:v=1:a=1" in filter_graph


def test_timeline_slicer_rejects_segment_used_by_multiple_output_units(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    slicer, analysis_id, review_revision, config_hash = setup_slicer(tmp_path, runner)
    request = TimelineSliceRequest(
        analysis_id=analysis_id,
        config_id="slice-library",
        review_revision=review_revision,
        current_config_hash=config_hash,
        client_request_id="duplicate-composite-request",
        assignments=[
            {"segment_indexes": [1, 3], "category": "hook"},
            {"segment_indexes": [2, 3], "category": "ending"},
        ],
    )

    with pytest.raises(SliceError, match="多个输出单元"):
        slicer.export(request)
    assert commands == []


def test_timeline_slicer_continues_after_one_segment_fails(tmp_path):
    ffmpeg_count = 0

    def runner(command, **_kwargs):
        nonlocal ffmpeg_count
        if command[0] == "ffmpeg":
            ffmpeg_count += 1
            if ffmpeg_count == 1:
                return SimpleNamespace(returncode=1, stdout="", stderr="broken input")
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [{"codec_type": "video"}]}),
            stderr="",
        )

    slicer, analysis_id, review_revision, config_hash = setup_slicer(tmp_path, runner)
    result = slicer.export(
        TimelineSliceRequest(
            analysis_id=analysis_id,
            config_id="slice-library",
            review_revision=review_revision,
            current_config_hash=config_hash,
            client_request_id="slice-request-2",
            assignments=[
                {"segment_index": 1, "category": "hook"},
                {"segment_index": 2, "category": "hook"},
                {"segment_index": 3, "category": "hook"},
            ],
        )
    )

    assert result["ok"] is False
    assert result["success_count"] == 2
    assert result["failure_count"] == 1
    assert result["items"][0]["error"] == "broken input"
    assert not list((tmp_path / "library/切片素材/引子").glob("*.part.mp4"))


def test_timeline_slicer_skips_excluded_segments(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"rendered")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"streams": [{"codec_type": "video"}]}),
            stderr="",
        )

    slicer, analysis_id, review_revision, config_hash = setup_slicer(tmp_path, runner)
    result = slicer.export(
        TimelineSliceRequest(
            analysis_id=analysis_id,
            config_id="slice-library",
            review_revision=review_revision,
            current_config_hash=config_hash,
            client_request_id="slice-request-skip",
            assignments=[
                {"segment_index": 1, "category": "hook"},
                {"segment_index": 2, "category": "skip"},
                {"segment_index": 3, "category": "ending"},
            ],
        )
    )

    assert result["success_count"] == 2
    assert result["skipped_count"] == 1
    assert result["failure_count"] == 0
    assert result["items"][1]["status"] == "skipped"
    assert result["items"][1]["output_path"] is None
    assert len([command for command in commands if command[0] == "ffmpeg"]) == 2


def test_timeline_slicer_rejects_stale_review_before_ffmpeg(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    slicer, analysis_id, _review_revision, config_hash = setup_slicer(tmp_path, runner)
    request = TimelineSliceRequest(
        analysis_id=analysis_id,
        config_id="slice-library",
        review_revision="c" * 64,
        current_config_hash=config_hash,
        client_request_id="slice-stale-review",
        assignments=[
            {"segment_index": 1, "category": "hook"},
            {"segment_index": 2, "category": "benefit_1"},
            {"segment_index": 3, "category": "ending"},
        ],
    )

    with pytest.raises(SliceConflictError, match="审核已变更"):
        slicer.export(request)
    assert commands == []


def test_timeline_slicer_rejects_stale_config_before_ffmpeg(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    slicer, analysis_id, review_revision, _config_hash = setup_slicer(tmp_path, runner)
    request = TimelineSliceRequest(
        analysis_id=analysis_id,
        config_id="slice-library",
        review_revision=review_revision,
        current_config_hash="0" * 64,
        client_request_id="slice-stale-config",
        assignments=[
            {"segment_index": 1, "category": "hook"},
            {"segment_index": 2, "category": "benefit_1"},
            {"segment_index": 3, "category": "ending"},
        ],
    )

    with pytest.raises(SliceConflictError, match="配置已变更"):
        slicer.export(request)
    assert commands == []


def test_timeline_slicer_rejects_changed_source_before_ffmpeg(tmp_path):
    commands = []

    def runner(command, **_kwargs):
        commands.append(command)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    slicer, analysis_id, review_revision, config_hash = setup_slicer(tmp_path, runner)
    source = tmp_path / "原始 视频.mp4"
    source.write_bytes(b"source changed")
    request = TimelineSliceRequest(
        analysis_id=analysis_id,
        config_id="slice-library",
        review_revision=review_revision,
        current_config_hash=config_hash,
        client_request_id="slice-changed-source",
        assignments=[
            {"segment_index": 1, "category": "hook"},
            {"segment_index": 2, "category": "benefit_1"},
            {"segment_index": 3, "category": "ending"},
        ],
    )

    with pytest.raises(SliceConflictError, match="原视频已变更"):
        slicer.export(request)
    assert commands == []


def test_timeline_slicer_with_real_ffmpeg(tmp_path):
    slicer, analysis_id, review_revision, config_hash = setup_slicer(
        tmp_path, subprocess.run
    )
    source = tmp_path / "原始 视频.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=180x320:r=25:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-shortest",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    record_path = slicer.timeline_analyzer.data_directory / f"{analysis_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    source_stat = source.stat()
    record["source_fingerprint"] = {
        "size_bytes": source_stat.st_size,
        "modified_at_ns": source_stat.st_mtime_ns,
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    result = slicer.export(
        TimelineSliceRequest(
            analysis_id=analysis_id,
            config_id="slice-library",
            review_revision=review_revision,
            current_config_hash=config_hash,
            client_request_id="real-slice-request",
            assignments=[
                {"segment_index": 1, "category": "hook"},
                {"segment_index": 2, "category": "benefit_1"},
                {"segment_index": 3, "category": "ending"},
            ],
        )
    )

    assert result["success_count"] == 3
    durations = [probe_media(Path(item["output_path"])).duration for item in result["items"]]
    assert all(0.9 <= duration <= 1.1 for duration in durations)


def test_timeline_slicer_composite_with_real_ffmpeg_removes_middle_gap(tmp_path):
    slicer, analysis_id, review_revision, config_hash = setup_slicer(
        tmp_path, subprocess.run
    )
    source = tmp_path / "原始 视频.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=red:s=180x320:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=330:sample_rate=48000:duration=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=180x320:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=1",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=180x320:r=25:d=1",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=550:sample_rate=48000:duration=1",
            "-filter_complex",
            "[0:v][1:a][2:v][3:a][4:v][5:a]concat=n=3:v=1:a=1[v][a]",
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
    )
    record_path = slicer.timeline_analyzer.data_directory / f"{analysis_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    source_stat = source.stat()
    record["source_fingerprint"] = {
        "size_bytes": source_stat.st_size,
        "modified_at_ns": source_stat.st_mtime_ns,
    }
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    result = slicer.export(
        TimelineSliceRequest(
            analysis_id=analysis_id,
            config_id="slice-library",
            review_revision=review_revision,
            current_config_hash=config_hash,
            client_request_id="real-composite-request",
            assignments=[
                {"segment_indexes": [1, 3], "category": "benefit_1"},
            ],
        )
    )

    assert result["success_count"] == 1
    output = Path(result["items"][0]["output_path"])
    media = probe_media(output)
    assert 1.9 <= media.duration <= 2.1
    assert media.has_audio is True

    def sample_rgb(seconds):
        sampled = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                str(seconds),
                "-i",
                str(output),
                "-frames:v",
                "1",
                "-vf",
                "scale=1:1",
                "-pix_fmt",
                "rgb24",
                "-f",
                "rawvideo",
                "-",
            ],
            check=True,
            capture_output=True,
        )
        return tuple(sampled.stdout[:3])

    first = sample_rgb(0.4)
    second = sample_rgb(1.4)
    assert first[0] > first[1] and first[0] > first[2]
    assert second[2] > second[0] and second[2] > second[1]
