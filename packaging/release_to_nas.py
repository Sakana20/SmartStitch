#!/usr/bin/env python3
"""Build and publish the next SmartStitch patch release to a mounted NAS."""

from __future__ import annotations

import argparse
import subprocess
import sys
import tomllib
from pathlib import Path

from bump_version import next_patch_version


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_NAS_ROOT = Path("/Volumes/home/Smartstitch")


def choose_version(current: str, latest: str | None, current_tag_exists: bool) -> str:
    current_key = tuple(map(int, current.split(".")))
    if latest is not None:
        latest_key = tuple(map(int, latest.split(".")))
        if latest_key > current_key:
            raise RuntimeError(
                f"NAS 已发布 v{latest}，但源码仍是 v{current}；请先同步源码"
            )
        if latest_key == current_key:
            return next_patch_version(current)
    if current_tag_exists:
        return next_patch_version(current)
    return current


def run_release(nas_root: Path, notes: str) -> str:
    if not nas_root.is_dir():
        raise RuntimeError(f"未连接 NAS：{nas_root}")
    if sys.platform != "darwin":
        raise RuntimeError("只能在 macOS 上构建和发布 DMG")
    venv = PROJECT_ROOT / ".venv" / "bin"
    python = venv / "python"
    if not python.is_file():
        raise RuntimeError("找不到 .venv/bin/python，请先安装项目的发布依赖")
    if not (PROJECT_ROOT / "build/ffmpeg-arm64/bin/ffmpeg").is_file():
        raise RuntimeError("找不到内置 FFmpeg，请先构建 build/ffmpeg-arm64")

    current = tomllib.loads(
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    package_version = (
        (PROJECT_ROOT / "smartstitch/__init__.py")
        .read_text(encoding="utf-8")
        .split('__version__ = "', 1)[1]
        .split('"', 1)[0]
    )
    if package_version != current:
        raise RuntimeError("pyproject.toml 与 smartstitch/__init__.py 版本不一致")

    sys.path.insert(0, str(PROJECT_ROOT))
    from smartstitch.updater import read_manifest

    latest_manifest = read_manifest(nas_root / "updates")
    latest = latest_manifest["version"] if latest_manifest else None
    tag = subprocess.run(
        ["git", "rev-parse", "-q", "--verify", f"refs/tags/v{current}"],
        cwd=PROJECT_ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode == 0
    target = choose_version(current, latest, tag)
    if target != current:
        print(f"自动递增版本：v{current} → v{target}", flush=True)
        subprocess.run(
            [str(python), "packaging/bump_version.py", target],
            cwd=PROJECT_ROOT,
            check=True,
        )
    else:
        print(f"继续未发布的版本 v{current}", flush=True)

    import os

    environment = os.environ.copy()
    environment["PATH"] = f"{venv}:{environment.get('PATH', '')}"
    version_tag = f"v{target}"
    subprocess.run(
        ["bash", "packaging/build_app.sh", version_tag],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
    )
    subprocess.run(
        ["bash", "packaging/create_dmg.sh", version_tag],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
    )
    dmg = PROJECT_ROOT / "dist" / f"SmartStitch-{version_tag}-macOS-arm64.dmg"
    subprocess.run(
        [
            str(python), "packaging/publish_nas_update.py", str(dmg),
            "--nas-root", str(nas_root), "--notes", notes,
        ],
        cwd=PROJECT_ROOT,
        env=environment,
        check=True,
    )
    return target


def main() -> None:
    parser = argparse.ArgumentParser(
        description="自动递增版本、构建 App/DMG 并发布到 NAS"
    )
    parser.add_argument("--nas-root", type=Path, default=DEFAULT_NAS_ROOT)
    parser.add_argument("--notes", default="", help="显示给客户端的更新摘要")
    arguments = parser.parse_args()
    version = run_release(arguments.nas_root, arguments.notes)
    print(f"NAS 更新发布完成：v{version}")


if __name__ == "__main__":
    main()
