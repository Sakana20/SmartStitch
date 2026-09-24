from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import uvicorn


LOGGER = logging.getLogger(__name__)
DEFAULT_SHUTDOWN_TIMEOUT = 10.0


class LocalApplicationServer:
    """Run Uvicorn on a reserved loopback socket owned by this process."""

    def __init__(self, app: Any, host: str = "127.0.0.1", port: int = 0):
        self.app = app
        self.host = host
        self.requested_port = port
        self.socket: socket.socket | None = None
        self.server: uvicorn.Server | None = None
        self.thread: threading.Thread | None = None
        self.error: BaseException | None = None

    @property
    def port(self) -> int:
        if self.socket is None:
            raise RuntimeError("本地服务尚未启动")
        return int(self.socket.getsockname()[1])

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"

    def start(self, timeout: float = 10.0) -> None:
        if self.thread is not None:
            raise RuntimeError("本地服务已经启动")
        server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_socket.bind((self.host, self.requested_port))
        server_socket.listen(2048)
        self.socket = server_socket
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=2,
        )
        self.server = uvicorn.Server(config)
        self.thread = threading.Thread(
            target=self._serve,
            name="smartstitch-local-server",
            daemon=False,
        )
        self.thread.start()

        deadline = time.monotonic() + timeout
        while not self.server.started:
            if self.error is not None:
                raise RuntimeError("SmartStitch 本地服务启动失败") from self.error
            if not self.thread.is_alive():
                raise RuntimeError("SmartStitch 本地服务启动失败")
            if time.monotonic() >= deadline:
                self.stop(timeout=1.0)
                raise TimeoutError("等待 SmartStitch 本地服务启动超时")
            time.sleep(0.02)

    def _serve(self) -> None:
        try:
            assert self.server is not None
            assert self.socket is not None
            self.server.run(sockets=[self.socket])
        except BaseException as exc:
            self.error = exc
            LOGGER.exception("SmartStitch local server stopped unexpectedly")

    def stop(self, timeout: float = DEFAULT_SHUTDOWN_TIMEOUT) -> None:
        stream_shutdown_event = getattr(self.app.state, "stream_shutdown_event", None)
        if stream_shutdown_event is not None:
            stream_shutdown_event.set()
        if self.server is not None:
            self.server.should_exit = True
        if self.thread is not None and self.thread is not threading.current_thread():
            self.thread.join(max(0.0, timeout))
            if self.thread.is_alive() and self.server is not None:
                self.server.force_exit = True
                self.thread.join(1.0)
        if self.socket is not None:
            try:
                self.socket.close()
            except OSError:
                pass


def _application_managers(app: Any) -> list[Any]:
    state = app.state
    return [
        manager
        for manager in (
            getattr(state, "job_manager", None),
            getattr(state, "slice_job_manager", None),
            getattr(state, "prores_alpha_manager", None),
            getattr(state, "video_upscale_manager", None),
            getattr(state, "cluster_upscale_manager", None),
            getattr(state, "cluster_upscale_batch_manager", None),
            getattr(state, "feishu_sync_manager", None),
            getattr(state, "cluster_master", None),
        )
        if manager is not None
    ]


def active_task_count(app: Any) -> int:
    count = 0
    for manager in _application_managers(app):
        active_count = getattr(manager, "active_count", None)
        if active_count is not None:
            count += int(active_count())
    return count


def shutdown_application(app: Any, timeout: float = 8.0) -> None:
    deadline = time.monotonic() + max(0.0, timeout)
    for manager in _application_managers(app):
        shutdown = getattr(manager, "shutdown", None)
        if shutdown is None:
            continue
        remaining = max(0.0, deadline - time.monotonic())
        try:
            shutdown(timeout=remaining)
        except Exception:
            LOGGER.exception("Failed to stop %s", type(manager).__name__)


class DesktopApplication:
    """Own the local server and the native WebKit window as one lifecycle."""

    def __init__(
        self,
        app: Any,
        webview: Any,
        *,
        server: LocalApplicationServer | None = None,
        shutdown_timeout: float = DEFAULT_SHUTDOWN_TIMEOUT,
    ):
        self.app = app
        self.webview = webview
        self.server = server or LocalApplicationServer(app)
        self.shutdown_timeout = shutdown_timeout
        self._shutdown_lock = threading.Lock()
        self._stopped = False

    def run(self) -> None:
        self.server.start()
        try:
            window = self.webview.create_window(
                "SmartStitch",
                self.server.url,
                width=1280,
                height=820,
                min_size=(960, 640),
                text_select=True,
                confirm_close=False,
            )
            window.events.closing += self._confirm_close(window)
            self.webview.start(gui="cocoa", debug=False)
        finally:
            self.shutdown()

    def _confirm_close(self, window: Any) -> Callable[[], bool | None]:
        def confirm() -> bool | None:
            count = active_task_count(self.app)
            if count:
                confirmed = window.create_confirmation_dialog(
                    "退出 SmartStitch",
                    f"当前有 {count} 个任务仍在运行。退出会停止这些任务，确定退出吗？",
                )
                if not confirmed:
                    return False

            # Cocoa may terminate the process directly after a Dock/Menu Quit,
            # without returning from webview.start() to run its finally block.
            # Clean up before allowing either native close path to finish.
            self.shutdown()
            return None

        return confirm

    def shutdown(self) -> None:
        with self._shutdown_lock:
            if self._stopped:
                return
            self._stopped = True
        started = time.monotonic()
        shutdown_application(self.app, timeout=max(0.0, self.shutdown_timeout - 1.0))
        manager_seconds = time.monotonic() - started
        remaining = max(0.0, self.shutdown_timeout - (time.monotonic() - started))
        self.server.stop(timeout=remaining)
        LOGGER.info(
            "Desktop shutdown completed: managers=%.2fs server=%.2fs",
            manager_seconds,
            time.monotonic() - started - manager_seconds,
        )


def run_desktop_application(app: Any, *, port: int = 0) -> None:
    # Import lazily so source/development mode does not require macOS GUI packages.
    import webview

    DesktopApplication(
        app,
        webview,
        server=LocalApplicationServer(app, port=port),
    ).run()
