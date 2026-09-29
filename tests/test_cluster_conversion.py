from __future__ import annotations

import shutil
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import authenticated_client
from smartstitch.api import create_app
from smartstitch import cluster, cluster_conversion
from smartstitch.landscape_to_portrait import LandscapeToPortraitManager, inspect


def wait(manager, job_id):
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        job = manager.get(job_id)
        if job['status'] in {'completed', 'failed', 'partial_failed', 'cancelled', 'interrupted'}:
            return job
        time.sleep(.05)
    pytest.fail('转换任务未结束')


@pytest.fixture
def setup(tmp_path, monkeypatch):
    shared = tmp_path / 'nas'
    shared.mkdir()
    for title in ('横改竖', '竖改横'):
        for folder in ('原素材', '已处理'):
            (shared / title / folder).mkdir(parents=True)
    apps = [create_app(tmp_path, config_directory=shared, data_directory=tmp_path / f'data{i}') for i in range(3)]
    peers = {f'http://127.0.0.1:{9800+i}': TestClient(apps[i].state.cluster_worker._app()) for i in (1, 2)}
    submitted = []

    def request(url, token, *, method='GET', payload=None):
        host, path = url.split('/', 3)[2:]
        if path == 'conversion-attempts':
            submitted.append((host, payload))
        response = peers[f'http://{host}'].request(method, '/' + path, json=payload)
        response.raise_for_status()
        return response.json()

    monkeypatch.setattr(cluster, '_request', request)
    monkeypatch.setattr(cluster_conversion, '_request', request)
    master = apps[0].state.cluster_master
    for host in peers:
        master.add_node(host, '')
    try:
        yield apps, peers, shared, submitted
    finally:
        for app in apps:
            app.state.portrait_manager.shutdown()
            app.state.landscape_manager.shutdown()
            app.state.cluster_master.shutdown()
            app.state.instance_lock.release()


@pytest.mark.parametrize('direction,size,dimensions', [
    ('landscape-to-portrait', '160x90', (720, 1280)),
    ('portrait-to-landscape', '90x160', (1280, 720)),
])
@pytest.mark.parametrize('software_only', [False, True])
def test_two_workers_real_conversion_and_isolated_records(setup, tmp_path, monkeypatch, direction, size, dimensions, software_only):
    apps, peers, shared, submitted = setup
    title = '横改竖' if direction == 'landscape-to-portrait' else '竖改横'
    inputs = shared / title / '原素材'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', f'color=c=blue:s={size}:r=20:d=0.2',
                    '-f', 'lavfi', '-i', 'sine=duration=0.2', '-shortest', '-c:v', 'libx264', '-c:a', 'aac', str(inputs / 'one.mp4')], check=True)
    shutil.copy2(inputs / 'one.mp4', inputs / 'one.mov')
    existing = shared / title / '已处理' / f'one_{title}.mp4'
    existing.write_bytes(b'keep-existing-result')
    barrier = threading.Barrier(2)
    render = LandscapeToPortraitManager._render_and_verify
    visited = set()
    lock = threading.Lock()

    def simultaneous(self, job, item, temporary):
        with lock:
            first = job['id'] not in visited
            visited.add(job['id'])
        if first:
            barrier.wait(timeout=8)
        return render(self, job, item, temporary)

    monkeypatch.setattr(LandscapeToPortraitManager, '_render_and_verify', simultaneous)
    client = authenticated_client(apps[0])
    assert client.put('/api/v1/settings/codecs', json={'software_codec_enabled': software_only}).status_code == 200
    preview = client.post(f'/api/v1/tools/{direction}/preview', json={'mode': 'cluster'}).json()
    response = client.post(f'/api/v1/tools/{direction}', json={'preview_id': preview['id'], 'mode': 'cluster'})
    assert response.status_code == 200, response.text
    manager = apps[0].state.portrait_manager if direction == 'landscape-to-portrait' else apps[0].state.landscape_manager
    job = wait(manager, response.json()['id'])
    assert job['status'] == 'completed', job
    assert job['succeeded'] == 2
    assert existing.read_bytes() == b'keep-existing-result'
    assert len({item['output_path'] for item in job['items']}) == 2
    assert len({item['node_id'] for item in job['items']}) == 2
    for item in job['items']:
        info = inspect(Path(item['output_path']))
        assert (info['width'], info['height']) == dimensions
        assert info['has_audio']
        if software_only:
            assert item['actual_video_encoder'] == 'libx264'
            assert item['hardware_decode_requested'] is False
    assert not (shared / title / '.暂存' / job['id']).exists()
    assert job['source_directory'] == str(inputs)
    assert job['output_directory'] == str(shared / title / '已处理')
    assert all(payload['source_relative'].startswith(title + '/原素材/') for _, payload in submitted)
    assert all(payload['software_codec_enabled'] is software_only for _, payload in submitted)
    for app in apps[1:]:
        assert app.state.portrait_manager.list() == []
        assert app.state.landscape_manager.list() == []
    host, payload = submitted[0]
    peer = peers['http://' + host]
    assert peer.post('/conversion-attempts', json=payload).status_code == 200
    changed = {**payload, 'resolution': '1080p'}
    assert peer.post('/conversion-attempts', json=changed).status_code == 422


def test_reject_paths_offline_selection_and_old_workers(setup, tmp_path, monkeypatch):
    apps, peers, shared, _ = setup
    manager = apps[0].state.portrait_manager
    source = shared / '横改竖' / '原素材'
    (source / 'video.mp4').write_bytes(b'not yet inspected')
    preview = manager.preview(mode='cluster')
    with pytest.raises(ValueError, match='不存在'):
        manager.create(preview['id'], 'cluster', ['missing'])
    monkeypatch.setattr(manager.cluster, 'node_statuses', lambda: [{'node_id': 'old', 'online': True}])
    with pytest.raises(ValueError, match='支持横竖转换'):
        manager.create(preview['id'], 'cluster')
    payload = {'attempt_id': 'a' * 32, 'canonical_root': str(apps[1].state.config_store.canonical_directory),
               'source_relative': '../outside.mp4', 'source_sha256': 'x', 'direction': 'landscape-to-portrait', 'resolution': '720p', 'stage_relative': '横改竖/.暂存/' + 'b' * 32 + '/0/' + 'a' * 32 + '.mp4'}
    peer = peers['http://127.0.0.1:9801']
    assert peer.post('/conversion-attempts', json=payload).status_code == 422
    outside = tmp_path / 'outside'
    outside.mkdir()
    (source / 'escape.mp4').symlink_to(outside / 'outside.mp4')
    payload['source_relative'] = '横改竖/原素材/escape.mp4'
    assert peer.post('/conversion-attempts', json=payload).status_code == 422


def test_cancel_workers_and_no_publication(setup, tmp_path, monkeypatch):
    apps, peers, shared, submitted = setup
    entered = threading.Event()

    def blocked(self, job, item, temporary):
        entered.set()
        self.cancelled.wait(8)
        return False

    monkeypatch.setattr(LandscapeToPortraitManager, '_render_and_verify', blocked)
    source = shared / '横改竖' / '原素材'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=160x90:r=20:d=0.2',
                    '-c:v', 'libx264', str(source / 'one.mp4')], check=True)
    manager = apps[0].state.portrait_manager
    job = manager.create(manager.preview(mode='cluster')['id'], 'cluster')
    assert entered.wait(8)
    manager.cancel(job['id'])
    job = wait(manager, job['id'])
    assert job['status'] == 'cancelled'
    assert job['succeeded'] == 0
    assert not list((shared / '横改竖' / '已处理').glob('*.mp4'))
    host, payload = submitted[0]
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        state = peers['http://' + host].get('/attempts/' + payload['attempt_id']).json()
        if state['status'] == 'cancelled':
            break
        time.sleep(.05)
    assert state['status'] == 'cancelled'


def test_transient_dispatch_retry_and_skipped_invalid(setup, tmp_path, monkeypatch):
    apps, peers, shared, _ = setup
    source = shared / '横改竖' / '原素材'
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=160x90:r=20:d=0.2',
                    '-c:v', 'libx264', str(source / 'one.mp4')], check=True)
    subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'color=s=90x160:r=20:d=0.2',
                    '-c:v', 'libx264', str(source / 'skip.mp4')], check=True)
    (source / 'broken.mp4').write_bytes(b'invalid-media')
    original = cluster_conversion._request
    failed = False

    def transient(url, token, **options):
        nonlocal failed
        if url.endswith('/conversion-attempts') and not failed:
            failed = True
            raise OSError('simulated connection reset')
        return original(url, token, **options)

    monkeypatch.setattr(cluster_conversion, '_request', transient)
    manager = apps[0].state.portrait_manager
    node = apps[1].state.cluster_worker.settings['node_id']
    job = manager.create(manager.preview(mode='cluster')['id'], 'cluster', [node])
    job = wait(manager, job['id'])
    assert failed
    assert job['status'] == 'completed', job
    assert (job['succeeded'], job['skipped_count'], job['invalid_count']) == (1, 1, 1)
    assert {item['node_id'] for item in job['items']} == {node}
    assert max(item['attempts'] for item in job['items']) == 2


@pytest.mark.parametrize('direction,title', [('landscape-to-portrait', '横改竖'), ('portrait-to-landscape', '竖改横')])
def test_nas_preview_ignores_custom_paths_and_binds_mode(setup, tmp_path, monkeypatch, direction, title):
    apps, _, shared, _ = setup
    inputs = shared / title / '原素材'
    (inputs / '待处理.mp4').write_bytes(b'filenames-only')
    client = authenticated_client(apps[0])
    monkeypatch.setattr('smartstitch.landscape_to_portrait.probe', lambda *args: pytest.fail('读取清单不应探测视频'))
    response = client.post(f'/api/v1/tools/{direction}/preview', json={
        'mode': 'cluster', 'source_directory': str(tmp_path / 'custom'), 'output_directory': str(tmp_path / 'outside'),
    })
    assert response.status_code == 200, response.text
    preview = response.json()
    assert preview['source_directory'] == str(inputs)
    assert preview['output_directory'] == str(shared / title / '已处理')
    assert preview['pending_count'] == 1
    assert preview['items'][0]['name'] == '待处理.mp4'
    response = client.post(f'/api/v1/tools/{direction}', json={'preview_id': preview['id']})
    assert response.status_code == 422
    assert '执行方式' in response.text
    local_source = tmp_path / 'local'
    local_source.mkdir()
    (local_source / 'local.mp4').write_bytes(b'local')
    local = client.post(f'/api/v1/tools/{direction}/preview', json={'source_directory': str(local_source)}).json()
    response = client.post(f'/api/v1/tools/{direction}', json={'preview_id': local['id'], 'mode': 'cluster'})
    assert response.status_code == 422
    assert '执行方式' in response.text


def test_missing_nas_folders_and_source_link_rejected(setup, tmp_path):
    apps, _, shared, _ = setup
    manager = apps[0].state.portrait_manager
    output = shared / '横改竖' / '已处理'
    output.rmdir()
    with pytest.raises(ValueError, match='原素材.*已处理'):
        manager.preview(mode='cluster')
    assert not output.exists()
    output.mkdir()
    outside = tmp_path / 'outside.mp4'
    outside.write_bytes(b'outside')
    (shared / '横改竖' / '原素材' / 'link.mp4').symlink_to(outside)
    with pytest.raises(ValueError, match='目录外'):
        manager.preview(mode='cluster')
