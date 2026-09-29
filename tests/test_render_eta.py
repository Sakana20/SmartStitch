import asyncio
import json

from smartstitch.api import create_app
from smartstitch.jobs import JobManager


def test_queue_summary_accounts_for_inflight_and_failed_work():
    manager = JobManager.__new__(JobManager)
    job = {"count": 4, "items": [
        {"status": "succeeded", "progress": 1},
        {"status": "failed", "progress": 0.1},
        {"status": "running", "progress": 0.5},
        {"status": "pending", "progress": 0},
    ]}
    summary = manager._summary(job)
    assert summary["progress"] == 0.625
    assert "items" not in summary
    assert job["items"][1]["progress"] == 0.1
    assert manager._summary({"count": 0, "items": []})["progress"] == 0


def test_queue_stream_route_precedes_dynamic_job_route_and_stops(tmp_path):
    app = create_app(tmp_path)
    paths = [getattr(route, "path", None) for route in app.routes]
    assert paths.index("/api/v1/jobs/events") < paths.index("/api/v1/jobs/{job_id}")
    route = next(route for route in app.routes if getattr(route, "path", None) == "/api/v1/jobs/events")

    async def consume():
        response = await route.endpoint()
        stream = response.body_iterator
        first = await anext(stream)
        assert first.startswith("event: jobs_update\ndata: ")
        assert json.loads(first.split("data: ", 1)[1]) == []
        app.state.stream_shutdown_event.set()
        assert [chunk async for chunk in stream] == []

    asyncio.run(consume())


def test_fast_slice_estimate_uses_completed_work_and_excludes_startup():
    from smartstitch.slicer import TimelineSlicer
    from smartstitch.slice_jobs import SliceJobManager

    run_started = "2026-09-29T00:00:00+00:00"
    job = {"status": "running", "eta_run_started_at": run_started, "items": [
        {"status": "running", "progress": 0.011, "total_duration_seconds": 10,
         "phase": "encoding", "started_at": "2026-09-29T00:00:06+00:00"},
        {"status": "pending", "total_duration_seconds": 10},
        {"status": "pending", "total_duration_seconds": 10},
    ]}
    TimelineSlicer._update_remaining_estimate(job)
    assert job["eta"]["remaining_seconds"] is None
    first = job["items"][0]
    first.update(status="succeeded", progress=1,
                 finished_at="2026-09-29T00:00:06.600000+00:00")
    TimelineSlicer._update_remaining_estimate(job)
    assert job["eta"]["sample_count"] == 1
    assert abs(job["eta"]["remaining_seconds"] - 1.2) < 1e-9
    assert SliceJobManager._summary(job)["eta"] == job["eta"]
    second = job["items"][1]
    second.update(status="running", progress=.5, phase="encoding")
    TimelineSlicer._update_remaining_estimate(job)
    assert abs(job["eta"]["remaining_seconds"] - .9) < 1e-9


def test_slice_estimate_discards_old_retry_samples_and_invalid_timestamps():
    from smartstitch.slicer import TimelineSlicer

    job = {"status": "running", "eta_run_started_at": "2026-09-29T00:01:00+00:00", "items": [
        {"status": "succeeded", "total_duration_seconds": 10,
         "started_at": "2026-09-29T00:00:00+00:00", "finished_at": "2026-09-29T00:00:50+00:00"},
        {"status": "succeeded", "total_duration_seconds": 10,
         "started_at": "invalid", "finished_at": None},
        {"status": "failed", "total_duration_seconds": 10},
        {"status": "skipped", "total_duration_seconds": 10},
        {"status": "pending", "total_duration_seconds": 10},
    ]}
    TimelineSlicer._update_remaining_estimate(job)
    assert job["eta"]["sample_count"] == 0
    assert job["eta"]["remaining_seconds"] is None
    job["status"] = "completed"
    TimelineSlicer._update_remaining_estimate(job)
    assert job["eta"]["remaining_seconds"] is None


def test_slice_estimate_recalibrates_after_encoder_fallback():
    from smartstitch.slicer import TimelineSlicer

    job = {"status": "running", "items": [
        {"status": "succeeded", "total_duration_seconds": 10,
         "actual_video_encoder": "h264_videotoolbox",
         "started_at": "2026-09-29T00:00:00+00:00", "finished_at": "2026-09-29T00:00:00.500000+00:00"},
        {"status": "running", "total_duration_seconds": 10, "progress": .98,
         "actual_video_encoder": "libx264", "phase": "encoding_fallback"},
    ]}
    TimelineSlicer._update_remaining_estimate(job)
    assert job["eta"]["remaining_seconds"] is None
    job["items"].insert(1, {
        "status": "succeeded", "total_duration_seconds": 10,
        "actual_video_encoder": "libx264",
        "started_at": "2026-09-29T00:00:01+00:00", "finished_at": "2026-09-29T00:00:03+00:00",
    })
    TimelineSlicer._update_remaining_estimate(job)
    assert job["eta"]["sample_count"] == 1
    assert job["eta"]["remaining_seconds"] == 2
