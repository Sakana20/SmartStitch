import copy
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from smartstitch import cluster, scheduling
from smartstitch.models import ClusterJobCreateRequest
from smartstitch.database import SQLiteStore
from smartstitch.landscape_to_portrait import LandscapeToPortraitManager
from smartstitch.portrait_to_landscape import PortraitToLandscapeManager
from smartstitch.cluster_upscale import ClusterUpscaleBatchManager


def future():
    return datetime.now(timezone.utc) + timedelta(hours=1)


class Database:
    def __init__(self):
        self.rows = {}

    def save(self, job):
        self.rows[job['id']] = copy.deepcopy(job)

    def get(self, job_id):
        return copy.deepcopy(self.rows[job_id])

    def list(self):
        return list(copy.deepcopy(self.rows).values())


def render_master(tmp_path, monkeypatch):
    root = tmp_path / 'nas'
    root.mkdir()
    database = Database()
    jobs = SimpleNamespace(lock=threading.RLock(), database=database, get_job=database.get)
    def create(request):
        assert request.auto_start is False
        job = dict(id='job', items=[dict(index=1, status='pending')], count=1,
                   success_count=0, failure_count=0, finished_at=None)
        database.save(job)
        return job
    jobs.create = create
    store = SimpleNamespace(directory=root, canonical_directory=root,
                            load=lambda _: SimpleNamespace(output=SimpleNamespace(directory=str(root / 'out'))))
    worker = SimpleNamespace(threads={}, stop=lambda: None)
    master = cluster.ClusterMaster(worker, jobs, store, tmp_path)
    monkeypatch.setattr(cluster, '_planned', lambda _: SimpleNamespace(selections={}, overlay=None, visual_border=None, visual_effects=[]))
    master.node_statuses = lambda: [{'online': True}]
    return master


def test_reservation_does_not_launch_dispatch_before_deadline(tmp_path, monkeypatch):
    master = render_master(tmp_path, monkeypatch)
    launched = []
    master._launch = launched.append
    # Booking can happen while workers are offline.
    master.node_statuses = lambda: pytest.fail('booking should not require online workers')
    job = master.create(ClusterJobCreateRequest(config_id='test', count=1, scheduled_at=future()))
    assert job['status'] == 'scheduled' and job['started_at'] is None
    assert launched == ['job']
    clock = [datetime.fromisoformat(job['scheduled_at']).timestamp() - 2]
    monkeypatch.setattr(cluster.time, 'time', lambda: clock[0])
    calls = []
    class Event:
        def is_set(self):
            return False
        def wait(self, duration):
            assert calls == []
            clock[0] += duration
    master._run = lambda job_id, _: calls.append(master.jobs.get_job(job_id))
    master._await_start('job', Event())
    assert len(calls) == 1
    assert calls[0]['status'] == 'running' and calls[0]['started_at']


def test_cancel_and_restart_recovery_of_render_reservations(tmp_path, monkeypatch):
    master = render_master(tmp_path, monkeypatch)
    master._launch = lambda _: None
    job = master.create(ClusterJobCreateRequest(config_id='test', count=1, scheduled_at=future()))
    master.stopping = True
    master._await_start(job['id'], threading.Event())
    assert master.jobs.get_job(job['id'])['status'] == 'scheduled'
    master.stopping = False
    restored = []
    master._launch = restored.append
    master.resume_interrupted()
    assert restored == [job['id']]
    cancelled = master.cancel(job['id'])
    assert cancelled['status'] == 'cancelled'
    assert cancelled['items'][0]['status'] == 'cancelled'
    restored.clear()
    master.resume_interrupted()
    assert restored == []


@pytest.mark.parametrize('value', ['2000-01-01T20:00:00+08:00', '2099-01-01T20:00:00'])
def test_reject_past_or_timezone_free_appointment(value):
    with pytest.raises(ValidationError):
        ClusterJobCreateRequest(config_id='test', count=1, scheduled_at=value)


def test_scheduled_records_are_not_pruned_or_marked_interrupted(tmp_path):
    store = SQLiteStore(tmp_path / 'data.db')
    store.MAX_RECORDS_PER_TABLE = 1
    store.ensure_job_table('jobs')
    store.save('jobs', {'id': 'appointment', 'status': 'scheduled'})
    store.save('jobs', {'id': 'old', 'status': 'completed'})
    store.mark_active_interrupted('jobs')
    assert store.get('jobs', 'appointment')['status'] == 'scheduled'
    assert store.get('jobs', 'old') is None


@pytest.mark.parametrize('manager_class', [LandscapeToPortraitManager, PortraitToLandscapeManager])
def test_conversion_reservation_survives_shutdown_and_restarts(tmp_path, monkeypatch, manager_class):
    import smartstitch.cluster_conversion as conversion
    root = tmp_path / 'nas'
    source = root / manager_class.title / '原素材'
    source.mkdir(parents=True)
    (source / 'video.mp4').write_bytes(b'video')
    (root / manager_class.title / '已处理').mkdir()
    store = SQLiteStore(tmp_path / 'data.db')
    master = SimpleNamespace(config_store=SimpleNamespace(directory=root))
    manager = manager_class(store)
    manager.cluster = master
    preview = manager.preview(mode='cluster')
    monkeypatch.setattr(conversion, 'available_nodes', lambda *_: pytest.fail('booking probed workers'))
    job = manager.create(preview['id'], mode='cluster', scheduled_at=future())
    assert job['status'] == 'scheduled'
    manager.shutdown()
    assert store.get(manager.table, job['id'])['status'] == 'scheduled'
    reloaded = manager_class(store)
    reloaded.cluster = master
    reloaded.resume_scheduled()
    assert reloaded.get(job['id'])['status'] == 'scheduled'
    reloaded.cancel(job['id'])
    reloaded.worker.join(2)
    assert reloaded.get(job['id'])['status'] == 'cancelled'
    reloaded.shutdown()


def test_upscale_reservation_never_probes_video_before_start(tmp_path, monkeypatch):
    import smartstitch.cluster_upscale as upscale
    root = tmp_path / 'nas'
    source = root / '超分' / '原素材'
    source.mkdir(parents=True)
    (source / 'video.mp4').write_bytes(b'video')
    (root / '超分' / '已处理').mkdir()
    store = SQLiteStore(tmp_path / 'data.db')
    single = SimpleNamespace(cluster=SimpleNamespace(config_store=SimpleNamespace(directory=root)),
                             nodes=lambda: pytest.fail('waiting reservation probed workers'))
    monkeypatch.setattr(upscale, 'probe_video', lambda *_: pytest.fail('waiting reservation probed video'))
    manager = ClusterUpscaleBatchManager(single, store)
    job = manager.create(scheduled_at=future())
    assert job['status'] == 'scheduled'
    manager.shutdown()
    assert store.get('upscale_batches', job['id'])['status'] == 'scheduled'
    reloaded = ClusterUpscaleBatchManager(single, store)
    reloaded.resume_interrupted()
    reloaded.cancel(job['id'])
    for thread in list(reloaded.threads.values()):
        thread.join(2)
    assert reloaded.get(job['id'])['status'] == 'cancelled'
    reloaded.shutdown()


def test_overdue_reservation_starts_on_restart(tmp_path, monkeypatch):
    master = render_master(tmp_path, monkeypatch)
    master._launch = lambda _: None
    job = master.create(ClusterJobCreateRequest(config_id='test', count=1, scheduled_at=future()))
    job['scheduled_at'] = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    master.jobs.database.save(job)
    calls = []
    master._run = lambda job_id, _: calls.append(job_id)
    master._await_start(job['id'], threading.Event())
    assert calls == [job['id']]


def test_local_manager_shutdown_does_not_cancel_cluster_reservation(tmp_path):
    from smartstitch.api import create_app
    app = create_app(base_directory=tmp_path)
    manager = app.state.job_manager
    manager.database.save({'id': 'reservation', 'status': 'scheduled', 'job_type': 'cluster', 'items': []})
    try:
        manager.shutdown()
        assert manager.get_job('reservation')['status'] == 'scheduled'
    finally:
        app.state.cluster_master.shutdown()
        app.state.instance_lock.release()
