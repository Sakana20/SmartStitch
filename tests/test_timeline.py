from __future__ import annotations

import json
import struct

import pytest

import smartstitch.timeline as timeline_module

from smartstitch.timeline import (
    TimelineAnalyzer,
    TimelineError,
    _adaptive_intervals_from_levels,
    _aggregate_waveform,
    _parse_silence_intervals,
    merge_breakpoints,
    speech_pause_candidates,
)


def test_merge_breakpoints_uses_frames_and_combines_nearby_signals():
    points = merge_breakpoints(
        [1.0, 5.0],
        [1.08, 8.0],
        fps=25,
        frame_count=250,
    )
    assert [point["frame_index"] for point in points] == [25, 125, 200]
    assert points[0]["reasons"] == ["scene_change", "speech_pause"]
    assert points[0]["confidence"] == 0.9


def test_silence_intervals_and_speech_pause_candidates_keep_tail_room():
    intervals = _parse_silence_intervals(
        """
        silence_start: 0
        silence_end: 0.4 | silence_duration: 0.4
        silence_start: 5
        silence_end: 5.6 | silence_duration: 0.6
        silence_start: 9
        """,
        media_duration=10,
    )

    assert intervals == [
        {
            "start_seconds": 0.0,
            "end_seconds": 0.4,
            "duration_seconds": 0.4,
            "complete": True,
        },
        {
            "start_seconds": 5.0,
            "end_seconds": 5.6,
            "duration_seconds": pytest.approx(0.6),
            "complete": True,
        },
        {
            "start_seconds": 9.0,
            "end_seconds": 10,
            "duration_seconds": 1.0,
            "complete": False,
        },
    ]

    candidates = speech_pause_candidates(intervals, fps=25, frame_count=250)

    assert [candidate["frame_index"] for candidate in candidates] == [128]
    assert candidates[0]["reason"] == "speech_pause"
    assert candidates[0]["evidence"]["silence_start_seconds"] == 5.0
    assert candidates[0]["evidence"]["silence_end_seconds"] == 5.6


def test_speech_pause_candidate_respects_next_speech_guard_after_rounding():
    candidates = speech_pause_candidates(
        [
            {
                "start_seconds": 1.0,
                "end_seconds": 1.1,
                "duration_seconds": 0.1,
                "complete": True,
            }
        ],
        fps=100,
        frame_count=300,
    )

    assert candidates[0]["frame_index"] == 102
    assert 1.1 - candidates[0]["time_seconds"] >= 0.08 - 1e-9


def test_adaptive_pause_intervals_find_relative_energy_valleys():
    intervals, threshold = _adaptive_intervals_from_levels(
        [-8.0] * 10 + [-18.0] * 4 + [-8.0] + [-18.0] * 5 + [-8.0] * 10,
        0.35,
        media_duration=1.5,
        window_seconds=0.05,
    )

    assert threshold == -12.0
    assert len(intervals) == 1
    assert intervals[0]["start_seconds"] == pytest.approx(0.5)
    assert intervals[0]["end_seconds"] == pytest.approx(1.0)
    assert intervals[0]["detector"] == "adaptive_rms"


def test_adaptive_pause_threshold_uses_quiet_source_distribution():
    _intervals, threshold = _adaptive_intervals_from_levels(
        [-45.0] * 3 + [-30.0] * 7,
        0.1,
        media_duration=0.5,
        window_seconds=0.05,
    )

    assert threshold == pytest.approx(-28.0)


def test_merge_breakpoints_preserves_pause_evidence():
    pause = speech_pause_candidates(
        [
            {
                "start_seconds": 1.0,
                "end_seconds": 1.6,
                "duration_seconds": 0.6,
                "complete": True,
            }
        ],
        fps=25,
        frame_count=100,
    )[0]

    points = merge_breakpoints([1.04], [pause], fps=25, frame_count=100)

    assert points[0]["frame_index"] == 26
    assert points[0]["reasons"] == ["scene_change", "speech_pause"]
    assert points[0]["evidence"]["speech_pauses"][0]["silence_end_seconds"] == 1.6


def test_waveform_aggregation_and_visible_range(tmp_path):
    assert _aggregate_waveform([(-100, 200), (-300, 50)], 1) == [
        [round(-300 / 32768, 5), round(200 / 32768, 5)]
    ]
    analyzer = TimelineAnalyzer(tmp_path)
    analysis_id = "c" * 24
    record = {
        "analysis_id": analysis_id,
        "fps": 10,
        "frame_count": 100,
        "audio": {
            "waveform_status": "ready",
            "buckets_per_second": 400,
            "bucket_count": 4,
        },
    }
    (tmp_path / f"{analysis_id}.json").write_text(json.dumps(record), encoding="utf-8")
    (tmp_path / "waveforms" / f"{analysis_id}.wfm").write_bytes(
        b"".join(
            struct.pack("<hh", minimum, maximum)
            for minimum, maximum in [(-100, 100), (-200, 200), (-300, 300), (-400, 400)]
        )
    )

    result = analyzer.waveform(analysis_id, 0, 10, 2)

    assert result["bucket_count"] == 2
    assert result["peaks"] == [
        [round(-200 / 32768, 5), round(200 / 32768, 5)],
        [round(-400 / 32768, 5), round(400 / 32768, 5)],
    ]


def test_speech_pause_failure_keeps_scene_analysis_available(tmp_path, monkeypatch):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")
    monkeypatch.setattr(
        timeline_module,
        "_probe",
        lambda _path: {
            "duration": 2.0,
            "fps": 25.0,
            "frame_count": 50,
            "width": 320,
            "height": 240,
            "has_audio": True,
            "audio_channels": 1,
            "audio_sample_rate": 48000,
        },
    )
    monkeypatch.setattr(timeline_module, "_scene_times", lambda _path, _threshold: [1.0])
    monkeypatch.setattr(
        timeline_module,
        "_silence_intervals",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimelineError("audio failed")),
    )
    monkeypatch.setattr(
        timeline_module,
        "_generate_waveform",
        lambda _path, target: {
            "waveform_sample_rate": 8000,
            "buckets_per_second": 400,
            "bucket_count": 1,
        },
    )

    result = TimelineAnalyzer(tmp_path / "data").analyze(str(source), 0.3, 0.35)

    assert result["breakpoints"][0]["reasons"] == ["scene_change"]
    assert result["audio"]["speech_pause_status"] == "failed"
    assert result["audio"]["waveform_status"] == "ready"


def test_save_decision_persists_frame_exact_segments(tmp_path):
    analyzer = TimelineAnalyzer(tmp_path)
    analysis_id = "a" * 24
    analysis = {
        "analysis_id": analysis_id,
        "source_path": "/tmp/example.mp4",
        "fps": 25.0,
        "frame_count": 250,
    }
    (tmp_path / f"{analysis_id}.json").write_text(json.dumps(analysis), encoding="utf-8")

    result = analyzer.save_decision(analysis_id, [125, 25, 125])

    assert [point["frame_index"] for point in result["breakpoints"]] == [25, 125]
    assert len(result["review_revision"]) == 64
    assert result["segments"] == [
        {"index": 1, "segment_id": "f000000000-f000000025", "start_frame": 0, "end_frame": 25, "start_seconds": 0.0, "end_seconds": 1.0},
        {"index": 2, "segment_id": "f000000025-f000000125", "start_frame": 25, "end_frame": 125, "start_seconds": 1.0, "end_seconds": 5.0},
        {"index": 3, "segment_id": "f000000125-f000000250", "start_frame": 125, "end_frame": 250, "start_seconds": 5.0, "end_seconds": 10.0},
    ]

    saved = json.loads((tmp_path / f"{analysis_id}.json").read_text(encoding="utf-8"))
    assert saved["review"]["breakpoints"][0]["review_status"] == "human_confirmed"

    repeated = analyzer.save_decision(analysis_id, [25, 125])
    changed = analyzer.save_decision(analysis_id, [25, 150])
    assert repeated["review_revision"] == result["review_revision"]
    assert changed["review_revision"] != result["review_revision"]


def test_save_decision_rejects_first_and_last_frame(tmp_path):
    analyzer = TimelineAnalyzer(tmp_path)
    analysis_id = "b" * 24
    (tmp_path / f"{analysis_id}.json").write_text(
        json.dumps({"fps": 30, "frame_count": 90}), encoding="utf-8"
    )
    with pytest.raises(TimelineError):
        analyzer.save_decision(analysis_id, [0, 45])
