from __future__ import annotations

from types import SimpleNamespace

from smartstitch.desktop import DesktopApplication, active_task_count


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
    app = make_app(FakeManager(2), FakeManager(1), FakeManager())
    server = FakeServer()
    window = FakeWindow(confirmed=False)
    webview = FakeWebview(window)
    desktop = DesktopApplication(app, webview, server=server)

    server.start()
    handler = desktop._confirm_close(window)

    assert active_task_count(app) == 3
    assert handler() is False
    assert "3 个任务" in window.dialog_calls[0][1]

    window.confirmed = True
    assert handler() is None
