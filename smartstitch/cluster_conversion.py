"""Whole-video conversion dispatch using shared staging and existing worker attempts."""
from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .cluster import _file_sha256, _request
from .landscape_to_portrait import now, fingerprint


def nas_folders(manager):
    if manager.cluster is None:
        raise ValueError('集群不可用')
    root = manager.cluster.config_store.directory.resolve()
    tool = root / manager.title
    source, output = tool / '原素材', tool / '已处理'
    if not source.is_dir() or not output.is_dir():
        raise ValueError(f'NAS {manager.title}目录需包含“原素材”和“已处理”两个文件夹')
    if not tool.resolve().is_relative_to(root) or any(not path.resolve().is_relative_to(tool.resolve()) for path in (source, output)):
        raise ValueError('转换文件夹不能指向对应 NAS 工具目录以外')
    if not (tool / '.暂存').resolve().is_relative_to(tool.resolve()):
        raise ValueError('共享暂存目录不可用')
    return source.resolve(), output.resolve()


def available_nodes(cluster, selected=None):
    rows = cluster.node_statuses()
    if selected and set(selected) - {row['node_id'] for row in rows}:
        raise ValueError('所选工作机不存在，请刷新集群')
    nodes = [row for row in rows if row.get('online') and row.get('conversion_protocol') == 2
             and (not selected or row['node_id'] in selected)]
    if not nodes or (selected and len(nodes) != len(set(selected))):
        raise ValueError('请选择在线且支持横竖转换的工作机；旧版工作机需更新')
    return nodes


def submit_conversion(worker, payload):
    required = {'attempt_id', 'canonical_root', 'source_relative', 'source_sha256', 'direction', 'resolution', 'stage_relative'}
    if set(payload) != required:
        raise ValueError('转换请求字段无效')
    attempt = payload['attempt_id']
    if not isinstance(attempt, str) or len(attempt) != 32 or any(c not in '0123456789abcdef' for c in attempt):
        raise ValueError('执行标识无效')
    if payload['direction'] not in {'landscape-to-portrait', 'portrait-to-landscape'} or payload['resolution'] not in {'720p', '1080p'}:
        raise ValueError('转换方向或分辨率无效')
    if payload['canonical_root'] != str(worker.config_store.canonical_directory):
        raise ValueError('工作机与主控未挂载同一个共享目录')
    root = worker.config_store.directory.resolve()
    relative = Path(payload['source_relative'])
    title = '横改竖' if payload['direction'] == 'landscape-to-portrait' else '竖改横'
    tool = root / title
    source_root = (tool / '原素材').resolve()
    source = (root / relative).resolve()
    if (relative.is_absolute() or '..' in relative.parts or len(relative.parts) != 3
            or relative.parts[:2] != (title, '原素材') or not source_root.is_relative_to(tool.resolve())
            or not source.is_relative_to(source_root) or not tool.resolve().is_relative_to(root)):
        raise ValueError('转换素材必须位于对应 NAS 原素材目录')
    stage_relative = Path(payload['stage_relative'])
    stage = (root / stage_relative).resolve()
    if (stage_relative.is_absolute() or '..' in stage_relative.parts or len(stage_relative.parts) != 5
            or stage_relative.parts[:2] != (title, '.暂存') or stage.name != f'{attempt}.mp4'
            or not stage.is_relative_to(tool.resolve()) or not stage_relative.parts[3].isdigit()
            or len(stage_relative.parts[2]) != 32
            or any(c not in '0123456789abcdef' for c in stage_relative.parts[2])):
        raise ValueError('转换暂存路径无效')
    digest = hashlib.sha256(str(sorted(payload.items())).encode()).hexdigest()
    with worker.lock:
        existing = worker.get(attempt)
        if existing:
            if existing.get('request_digest') != digest:
                raise ValueError('执行标识已用于其他请求')
            return existing
        if worker.status()['active']:
            raise ValueError('工作机正在渲染，请稍后派发')
        if not source.is_file():
            raise ValueError('转换素材不存在')
        event = threading.Event()
        worker.events[attempt] = event
        worker._save({'attempt_id': attempt, 'kind': 'video_conversion', 'request_digest': digest,
                      'status': 'queued', 'progress': 0, 'result': None, 'error': None, 'updated_at': now(), 'created_at': now(),
                      'config_name': '横改竖' if payload['direction'] == 'landscape-to-portrait' else '竖改横',
                      'output_name': source.name})
        thread = threading.Thread(target=run_worker, args=(worker, payload, source, event), daemon=True)
        worker.threads[attempt] = thread
        thread.start()
    return {'attempt_id': attempt, 'status': 'queued'}


def run_worker(worker, payload, source, event):
    from .database import SQLiteStore
    from .landscape_to_portrait import LandscapeToPortraitManager
    from .portrait_to_landscape import PortraitToLandscapeManager
    attempt = payload['attempt_id']
    stage = (worker.config_store.directory / payload['stage_relative']).resolve()
    upload = stage.with_suffix('.upload')
    manager = None
    try:
        worker._update(attempt, status='running')
        with tempfile.TemporaryDirectory(prefix='smartstitch-conversion-') as directory:
            local = Path(directory)
            inputs = local / 'inputs'
            inputs.mkdir()
            shutil.copy2(source, inputs / source.name)
            if _file_sha256(inputs / source.name) != payload['source_sha256']:
                raise ValueError('转换素材复制期间发生变化')
            cls = LandscapeToPortraitManager if payload['direction'] == 'landscape-to-portrait' else PortraitToLandscapeManager
            manager = cls(SQLiteStore(local / 'jobs.sqlite'))
            preview = manager.preview(str(inputs), str(local / 'output'), payload['resolution'])
            job = manager.create(preview['id'])
            while manager.active_count():
                if event.wait(.2):
                    manager.cancel(job['id'])
                current = manager.get(job['id'])
                worker._update(attempt, progress=current['current_progress'])
            result = manager.get(job['id'])['items'][0]
            if event.is_set():
                raise RuntimeError('任务已取消')
            if result['status'] == 'failed':
                raise ValueError(result.get('error', '转换失败'))
            if result['status'] == 'completed':
                stage.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(result['output_path'], upload)
                if event.is_set():
                    raise RuntimeError('任务已取消')
                os.replace(upload, stage)
                result.update(size_bytes=stage.stat().st_size, sha256=_file_sha256(stage))
            result.pop('source', None)
            result.pop('output_path', None)
            worker._update(attempt, status='succeeded', progress=1, result=result)
    except Exception as exc:
        worker._update(attempt, status='cancelled' if event.is_set() else 'failed', error=str(exc))
    finally:
        if manager:
            manager.shutdown()
        upload.unlink(missing_ok=True)
        with worker.lock:
            worker.events.pop(attempt, None)
            worker.threads.pop(attempt, None)


def run_batch(manager, job, nodes):
    cluster = manager.cluster
    root = cluster.config_store.directory.resolve()
    staging = root / manager.title / '.暂存' / job['id']
    pending = iter(enumerate(job['items']))

    def save():
        job['completed'] = sum(i['status'] in {'completed', 'failed', 'invalid', 'skipped', 'cancelled'} for i in job['items'])
        job['succeeded'] = sum(i['status'] == 'completed' for i in job['items'])
        job['failed'] = sum(i['status'] == 'failed' for i in job['items'])
        job['invalid_count'] = sum(i['status'] == 'invalid' for i in job['items'])
        job['skipped_count'] = sum(i['status'] == 'skipped' for i in job['items'])
        manager._update_encoding_summary(job)
        manager.store.save(manager.table, job)

    def work(node):
        while not manager.cancelled.is_set():
            with manager.lock:
                pair = next(pending, None)
                if pair is None:
                    return
                index, item = pair
                item.update(status='running', node_id=node['node_id'], progress=0)
                save()
            active_attempt = None
            try:
                source = Path(item['source'])
                folder = staging / str(index)
                folder.mkdir(parents=True, exist_ok=True)
                if not folder.resolve().is_relative_to(root):
                    raise ValueError('暂存目录越过共享目录')
                source_root, _ = nas_folders(manager)
                if not source.resolve().is_relative_to(source_root):
                    raise ValueError('素材指向 NAS 原素材目录以外')
                before = fingerprint(source)
                sha = _file_sha256(source)
                if fingerprint(source) != before:
                    raise ValueError('源文件校验期间发生变化')
                for retry in range(3):
                    if manager.cancelled.is_set():
                        break
                    attempt = uuid.uuid4().hex
                    active_attempt = attempt
                    with manager.lock:
                        item.update(attempt_id=attempt, attempts=retry + 1)
                        save()
                    payload = {'attempt_id': attempt, 'canonical_root': str(cluster.config_store.canonical_directory),
                               'source_relative': str(source.relative_to(root)), 'source_sha256': sha,
                               'stage_relative': str((folder / f'{attempt}.mp4').relative_to(root)),
                               'direction': job['direction'], 'resolution': job['resolution']}
                    try:
                        completed_attempt = False
                        while not manager.cancelled.is_set():
                            hello = _request(node['url'] + '/hello', node.get('token', ''))
                            if not hello.get('active'):
                                break
                            manager.cancelled.wait(.5)
                        if manager.cancelled.is_set():
                            break
                        _request(node['url'] + '/conversion-attempts', node.get('token', ''), method='POST', payload=payload)
                        last_seen = time.monotonic()
                        while not manager.cancelled.wait(.5):
                            try:
                                state = _request(node['url'] + '/attempts/' + attempt, node.get('token', ''))
                                last_seen = time.monotonic()
                            except Exception:
                                if time.monotonic() - last_seen > 30:
                                    raise RuntimeError('工作机失联超过 30 秒')
                                continue
                            with manager.lock:
                                item['progress'] = state.get('progress', 0)
                                job['current_progress'] = sum(i.get('progress', 0) for i in job['items']) / max(1, job['total'])
                                save()
                            if state['status'] == 'succeeded':
                                result = state['result']
                                if result['status'] == 'completed':
                                    stage = folder / f'{attempt}.mp4'
                                    if stage.stat().st_size != result['size_bytes'] or _file_sha256(stage) != result['sha256']:
                                        raise ValueError('工作机结果校验失败')
                                    output = Path(job['output_directory'])
                                    with manager.lock:
                                        while True:
                                            target = manager._target(output, source.stem, set())
                                            try:
                                                fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                                                reserved = os.fstat(fd)
                                                break
                                            except FileExistsError:
                                                continue
                                    temp = output / f'.{attempt}.tmp.mp4'
                                    try:
                                        shutil.copy2(stage, temp)
                                        with manager.lock:
                                            if manager.cancelled.is_set():
                                                raise RuntimeError('任务已取消')
                                            current = target.stat()
                                            if (reserved.st_ino, reserved.st_dev) != (current.st_ino, current.st_dev):
                                                raise ValueError('输出文件被其他进程修改')
                                            os.replace(temp, target)
                                    except Exception:
                                        if target.exists() and target.stat().st_ino == reserved.st_ino:
                                            target.unlink()
                                        raise
                                    finally:
                                        os.close(fd)
                                        temp.unlink(missing_ok=True)
                                    result['output_path'] = str(target)
                                with manager.lock:
                                    item.update(result)
                                    save()
                                completed_attempt = True
                                break
                            if state['status'] in {'failed', 'cancelled', 'interrupted'}:
                                raise RuntimeError(state.get('error') or '工作机转换失败')
                        if manager.cancelled.is_set():
                            break
                        if completed_attempt:
                            break
                    except Exception:
                        try:
                            _request(node['url'] + f'/attempts/{attempt}/cancel', node.get('token', ''), method='POST')
                        except Exception:
                            pass
                        if retry == 2:
                            raise
                        manager.cancelled.wait(1)
            except Exception as exc:
                with manager.lock:
                    item.update(status='failed', error=str(exc))
            finally:
                if manager.cancelled.is_set():
                    if active_attempt:
                        try:
                            _request(node['url'] + f'/attempts/{active_attempt}/cancel', node.get('token', ''), method='POST')
                        except Exception:
                            pass
                    with manager.lock:
                        if item['status'] == 'running':
                            item['status'] = 'cancelled'
                with manager.lock:
                    save()

    try:
        for node in nodes:
            registration = next(n for n in cluster.nodes if n['node_id'] == node['node_id'])
            node['token'] = registration.get('token', '')
        with manager.lock:
            job.update(status='running', current_phase='rendering')
            save()
        with ThreadPoolExecutor(max_workers=len(nodes)) as executor:
            futures = [executor.submit(work, node) for node in nodes]
            for future in futures:
                future.result()
        with manager.lock:
            job['status'] = ('interrupted' if manager.stopping else 'cancelled') if manager.cancelled.is_set() else (
                'partial_failed' if job['failed'] and job['succeeded'] else 'failed' if job['failed'] else 'completed')
    except Exception as exc:
        job.update(status='failed', error=str(exc))
    finally:
        # Keep staging after cancellation/loss: a disconnected worker may still be reading it.
        if job['status'] == 'completed' and all(i.get('attempts', 1) == 1 for i in job['items']):
            shutil.rmtree(staging, ignore_errors=True)
        with manager.lock:
            for item in job['items']:
                if item['status'] in {'queued', 'running'}:
                    item['status'] = 'interrupted' if manager.stopping else 'cancelled'
            job.update(finished_at=now(), current_file=None, current_progress=0, current_phase=None)
            save()
            manager.active = None
