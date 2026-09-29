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
