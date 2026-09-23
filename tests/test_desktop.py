from __future__ import annotations

import asyncio
from types import SimpleNamespace

from smartstitch.api import create_app
from smartstitch.desktop import DesktopApplication, LocalApplicationServer, active_task_count


class FakeManager:
    def __init__(self, active: int = 0):
        self.active = active
        self.shutdown_timeouts: list[float] = []

    def active_count(self) -> int:
        return self.active

    def shutdown(self, timeout: float) -> None:
        self.shutdown_timeouts.append(timeout)


class FakeEvent:
    def __init__(self):
        self.handler = None

    def __iadd__(self, handler):
        self.handler = handler
        return self


class FakeWindow:
    def __init__(self, confirmed: bool = True):
        self.events = SimpleNamespace(closing=FakeEvent())
        self.confirmed = confirmed
        self.dialog_calls: list[tuple[str, str]] = []

    def create_confirmation_dialog(self, title: str, message: str) -> bool:
        self.dialog_calls.append((title, message))
        return self.confirmed


class FakeWebview:
    def __init__(self, window: FakeWindow):
        self.window = window
        self.created = None
        self.started = None

    def create_window(self, *args, **kwargs):
        self.created = (args, kwargs)
        return self.window

    def start(self, **kwargs) -> None:
        self.started = kwargs


class FakeServer:
    url = "http://127.0.0.1:54321"

    def __init__(self):
        self.started = False
        self.stop_timeouts: list[float] = []

    def start(self) -> None:
        self.started = True

    def stop(self, timeout: float) -> None:
        self.stop_timeouts.append(timeout)


def make_app(*managers: FakeManager):
    names = ("job_manager", "slice_job_manager", "feishu_sync_manager")
    return SimpleNamespace(
        state=SimpleNamespace(**dict(zip(names, managers, strict=False)))
    )


def test_desktop_window_owns_server_lifecycle() -> None:
    managers = (FakeManager(), FakeManager(), FakeManager())
    app = make_app(*managers)
    server = FakeServer()
    webview = FakeWebview(FakeWindow())

    DesktopApplication(app, webview, server=server).run()

    assert server.started is True
    assert webview.created[0] == ("SmartStitch", server.url)
    assert webview.started == {"gui": "cocoa", "debug": False}
    assert all(manager.shutdown_timeouts for manager in managers)
    assert server.stop_timeouts


def test_running_tasks_require_confirmation_before_close() -> None:
    managers = (FakeManager(2), FakeManager(1), FakeManager())
    app = make_app(*managers)
    server = FakeServer()
    window = FakeWindow(confirmed=False)
    webview = FakeWebview(window)
    desktop = DesktopApplication(app, webview, server=server)

    server.start()
    handler = desktop._confirm_close(window)

    assert active_task_count(app) == 3
    assert handler() is False
    assert "3 个任务" in window.dialog_calls[0][1]
    assert not server.stop_timeouts
    assert all(not manager.shutdown_timeouts for manager in managers)

    window.confirmed = True
    assert handler() is None
    assert len(server.stop_timeouts) == 1
    assert all(len(manager.shutdown_timeouts) == 1 for manager in managers)

    # A second close or the run() finally block must not stop things twice.
    assert handler() is None
    desktop.shutdown()
    assert len(server.stop_timeouts) == 1
    assert all(len(manager.shutdown_timeouts) == 1 for manager in managers)


def test_close_without_tasks_cleans_up_before_native_termination() -> None:
    manager = FakeManager()
    app = make_app(manager)
    server = FakeServer()
    window = FakeWindow()
    desktop = DesktopApplication(app, FakeWebview(window), server=server)

    server.start()
    assert desktop._confirm_close(window)() is None

    assert window.dialog_calls == []
    assert len(manager.shutdown_timeouts) == 1
    assert len(server.stop_timeouts) == 1


def test_server_stops_promptly_with_open_progress_stream(tmp_path) -> None:
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    server = LocalApplicationServer(app)

    async def check_stream() -> None:
        route = next(
            route for route in app.routes
            if getattr(route, "path", None) == "/api/v1/timeline/slice-jobs/events"
        )
        response = await route.endpoint()
        stream = response.body_iterator
        assert (await anext(stream)).startswith("event: slice_jobs_update")

        server.stop(timeout=0)
        assert app.state.stream_shutdown_event.is_set()
        try:
            await asyncio.wait_for(anext(stream), timeout=1)
        except StopAsyncIteration:
            pass
        else:
            raise AssertionError("Progress stream remained open after shutdown")

    asyncio.run(check_stream())
