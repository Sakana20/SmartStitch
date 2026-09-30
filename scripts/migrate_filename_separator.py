"""One-time, audited migration of the two shared SmartStitch projects to |!|.

Run without --apply to write a plan. --apply rechecks every input and rolls back
file and config changes if any step fails. Historical jobs are not rewritten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from datetime import datetime
from pathlib import Path

import yaml

from smartstitch.models import AppConfig, naming_filename_tokens


BASE = Path('/Volumes/home/Smartstitch')
CONFIGS = ('hongguoduanju-erjian.yaml', 'taobao-xingguang-erjian-generic.yaml')
RED = re.compile(r'^(\d+)_([^-]+)-(.+)-(\d{4}-\d{2}-\d{2})(__(?:\d{3}|g\d{3})_[A-Za-z0-9-]+_(?:f\d+-\d+|p\d{3}-\d{3})(?:-v\d+)?)?$')
PATTERN = r'^(?P<source_index>\d+)\|!\|(?P<talent>.*?)\|!\|(?P<source_title>.*?)\|!\|(?P<restriction_date>\d{4}-\d{2}-\d{2})$'


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def prepare():
    maps = {}
    configs = {}
    originals = {}
    summary = {}
    for config_name in CONFIGS:
        path = BASE / config_name
        original = path.read_bytes()
        config = yaml.safe_load(original)
        originals[path] = original
        root = Path(config['source_root'])
        red = config_name.startswith('hongguo')
        pools = ('pool_5', 'pool_6', 'pool_8') if red else ('pool_1',)
        local_map = {}
        per_pool = {}
        for pool in pools:
            group = config['sources'][pool]
            directory = root / group['directory']
            files = sorted(directory.glob('*.mp4'))
            for source in files:
                if '|!|' in source.name:
                    raise ValueError(f'already migrated: {source}')
                if red:
                    match = RED.fullmatch(source.stem)
                    if match is None:
                        raise ValueError(f'unrecognized red filename: {source}')
                    parts = list(match.groups()[:4])
                    suffix = match.group(5) or ''
                    target = source.with_name('|!|'.join(parts) + suffix + source.suffix)
                    if len(naming_filename_tokens(target.name)) != 4:
                        raise ValueError(f'bad four-field name: {target}')
                else:
                    parts = source.stem.split('-')
                    if len(parts) not in (3, 4) or any(not p for p in parts):
                        raise ValueError(f'unrecognized flash filename: {source}')
                    target = source.with_name('|!|'.join(parts) + source.suffix)
                if len(target.name.encode('utf-8')) > 255 or target.exists():
                    raise ValueError(f'target too long or exists: {target}')
                if target in local_map.values():
                    raise ValueError(f'duplicate target: {target}')
                local_map[source] = target
            per_pool[pool] = len(files)
        for group in config['sources'].values():
            for item in group.get('items', []):
                old = Path(item['path'])
                if old in local_map:
                    item['path'] = str(local_map[old])
        blocks = config['output']['naming']['builder']['blocks']
        for block in blocks:
            if block['type'] != 'source':
                continue
            for variant in list(block['variants']):
                old_name = variant['sample_name']
                matching = [new.name for old, new in local_map.items() if old.name == old_name]
                if len(matching) != 1:
                    raise ValueError(f'sample file missing or ambiguous: {old_name}')
                variant['sample_name'] = matching[0]
                variant.pop('signature', None)
                previous_index = variant.pop('token_index')
                variant['field_count'] = 4 if red else 3
                variant['field_index'] = ({2: 1, 6: 3} if red else {0: 0, 2: 1})[previous_index]
                if not red:
                    four_field = next(new.name for new in local_map.values() if len(naming_filename_tokens(new.name)) == 4)
                    block['variants'].append({'sample_name': four_field, 'field_count': 4, 'field_index': variant['field_index']})
        config['output']['naming']['source_metadata']['pattern'] = PATTERN
        AppConfig.model_validate(config)
        new_text = yaml.safe_dump(config, allow_unicode=True, sort_keys=False).encode()
        configs[path] = new_text
        maps.update(local_map)
        summary[config_name] = per_pool
    if len(maps) != len(set(maps.values())):
        raise ValueError('duplicate target across configs')
    return maps, originals, configs, summary


def write_plan(path: Path, maps, originals, configs, summary):
    payload = {
        'created_at': datetime.now().astimezone().isoformat(),
        'summary': summary,
        'configs': {str(p): {'old_sha256': sha(originals[p]), 'new_sha256': sha(configs[p])} for p in originals},
        'renames': [{'old': str(old), 'new': str(new)} for old, new in sorted(maps.items())],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + '\n')


def apply(maps, originals, configs, backup_dir: Path):
    lock_root = BASE / '.smartstitch-locks'
    for path in originals:
        config_id = path.stem
        for lock_type in ('edit', 'commit'):
            lock = lock_root / f'{config_id}.{lock_type}.lock'
            if not lock.exists():
                continue
            owner = lock / 'owner.json'
            if owner.exists():
                metadata = json.loads(owner.read_text())
                if time.time() - float(metadata.get('last_heartbeat_epoch', 0)) > float(metadata.get('lease_seconds', 120)):
                    continue
            raise RuntimeError(f'active or unknown config lock: {lock}')
    backup_dir.mkdir(parents=True, exist_ok=False)
    moved = []
    replaced = []
    for path, original in originals.items():
        (backup_dir / path.name).write_bytes(original)
    try:
        for old, new in maps.items():
            if not old.is_file() or new.exists():
                raise RuntimeError(f'source changed during migration: {old}')
            old.rename(new)
            moved.append((old, new))
        for path, data in configs.items():
            if path.read_bytes() != originals[path]:
                raise RuntimeError(f'config changed during migration: {path}')
            temp = path.with_name(path.name + '.separator-migration.tmp')
            temp.write_bytes(data)
            temp.replace(path)
            replaced.append(path)
    except BaseException:
        for path in reversed(replaced):
            path.write_bytes(originals[path])
        for old, new in reversed(moved):
            new.rename(old)
        raise


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--plan', type=Path, required=True)
    parser.add_argument('--backup-dir', type=Path)
    args = parser.parse_args()
    maps, originals, configs, summary = prepare()
    write_plan(args.plan, maps, originals, configs, summary)
    print(json.dumps({'count': len(maps), 'summary': summary, 'plan': str(args.plan)}, ensure_ascii=False))
    if args.apply:
        if args.backup_dir is None:
            parser.error('--backup-dir required with --apply')
        apply(maps, originals, configs, args.backup_dir)
        print(f'Applied migration; original configs backed up in {args.backup_dir}')


if __name__ == '__main__':
    main()
