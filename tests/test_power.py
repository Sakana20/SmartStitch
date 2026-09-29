import asyncio
import subprocess
from types import SimpleNamespace

from smartstitch import power
from smartstitch.api import create_app
from smartstitch.desktop import shutdown_application


class FakeProcess:
    def __init__(self):
        self.code = None
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self.code

    def terminate(self):
        self.terminated += 1
        self.code = 0

    def wait(self, timeout):
        return self.code

    def kill(self):
        self.killed += 1
        self.code = -9


def fake_caffeinate(monkeypatch):
    calls = []
    monkeypatch.setattr(power.sys, "platform", "darwin")

    def spawn(command, **kwargs):
        process = FakeProcess()
        calls.append((command, process))
        return process

    monkeypatch.setattr(power.subprocess, "Popen", spawn)
    return calls


def test_overlapping_reasons_and_exit_cleanup(monkeypatch):
    calls = fake_caffeinate(monkeypatch)
    reasons = []
    guard = power.SleepInhibitor(lambda: list(reasons))
    guard.refresh()
    assert calls == []
    reasons.extend(["render", "worker", "reservation"])
    guard.refresh()
    command, process = calls[0]
    assert command == ["/usr/bin/caffeinate", "-ims", "-w", str(power.os.getpid())]
    assert guard.status()["active"]
    reasons.remove("render")
    guard.refresh()
    assert len(calls) == 1 and process.terminated == 0
    reasons.clear()
    guard.refresh()
    assert process.terminated == 1 and not guard.status()["active"]
    reasons.append("worker")
    guard.refresh()
    guard.shutdown()
    guard.shutdown()
    guard.refresh()
    assert len(calls) == 2 and calls[1][1].terminated == 1


def test_failed_launch_and_dead_process_recover(monkeypatch):
    calls = fake_caffeinate(monkeypatch)
    spawn = power.subprocess.Popen
    guard = power.SleepInhibitor(lambda: ["render"])

    def fail(*args, **kwargs):
        raise OSError("unavailable")

    monkeypatch.setattr(power.subprocess, "Popen", fail)
    guard.refresh()
    assert guard.status()["error"] == "unavailable"
    monkeypatch.setattr(power.subprocess, "Popen", spawn)
    guard.refresh()
    assert guard.status()["error"] is None
    calls[0][1].code = 1
    guard.refresh()
    assert len(calls) == 2 and guard.status()["active"]
    guard.shutdown()


def test_unsupported_platform_does_not_spawn(monkeypatch):
    calls = fake_caffeinate(monkeypatch)
    monkeypatch.setattr(power.sys, "platform", "linux")
    guard = power.SleepInhibitor(lambda: ["render"])
    guard.refresh()
    assert not guard.status()["supported"] and calls == []
    guard.shutdown()


def test_force_release_if_child_does_not_exit(monkeypatch):
    calls = fake_caffeinate(monkeypatch)
    guard = power.SleepInhibitor(lambda: ["render"])
    guard.refresh()
    process = calls[0][1]

    def wait(timeout):
        if not process.killed:
            raise subprocess.TimeoutExpired("caffeinate", timeout)

    process.wait = wait
    guard.shutdown()
    assert process.killed == 1


def test_app_reasons_lifecycle_and_desktop_shutdown(tmp_path, monkeypatch):
    calls = fake_caffeinate(monkeypatch)
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    worker = app.state.cluster_worker
    monkeypatch.setattr(worker, "start", lambda: None)
    guard = app.state.sleep_inhibitor

    async def check():
        async with app.router.lifespan_context(app):
            assert guard.thread.is_alive() and not guard.status()["active"]
            # A listening worker must stay awake even while idle.
            worker.server = SimpleNamespace(started=True, should_exit=False)
            worker.server_thread = SimpleNamespace(is_alive=lambda: True)
            guard.refresh()
            assert guard.status()["reasons"] == ["工作机已上线"]
            worker.server = None
            worker.server_thread = None
            # active_count includes render reservations, keeping the master awake.
            monkeypatch.setattr(app.state.job_manager, "active_count", lambda: 1)
            guard.refresh()
            assert guard.status()["reasons"] == ["渲染或预约任务"]
            assert len(calls) == 1
        assert not guard.status()["active"] and not guard.thread.is_alive()

    asyncio.run(check())
    assert calls[0][1].terminated == 1
    second = power.SleepInhibitor(lambda: ["render"])
    second.refresh()
    shutdown_application(SimpleNamespace(state=SimpleNamespace(sleep_inhibitor=second)))
    assert not second.status()["active"]
