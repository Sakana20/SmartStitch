from __future__ import annotations

import json

import pytest

from smartstitch.timeline import TimelineAnalyzer, TimelineError, merge_breakpoints


def test_merge_breakpoints_uses_frames_and_combines_nearby_signals():
    points = merge_breakpoints(
        [1.0, 5.0],
        [1.08, 8.0],
        fps=25,
        frame_count=250,
    )
    assert [point["frame_index"] for point in points] == [25, 125, 200]
    assert points[0]["reasons"] == ["scene_change", "silence_end"]
    assert points[0]["confidence"] == 0.9


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
