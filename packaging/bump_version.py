#!/usr/bin/env python3
"""Update every source-controlled SmartStitch version in one command."""

from __future__ import annotations

import argparse
from pathlib import Path
import re
import shutil
import subprocess
import sys


VERSION_PATTERN = re.compile(r"^(?:v)?(\d+)\.(\d+)\.(\d+)$")
PROJECT_VERSION_PATTERN = re.compile(r'(?m)^version = "([^"]+)"$')
PACKAGE_VERSION_PATTERN = re.compile(r'(?m)^__version__ = "([^"]+)"$')


def normalize_version(value: str) -> str:
    match = VERSION_PATTERN.fullmatch(value.strip())
    if match is None:
        raise ValueError("版本必须使用 X.Y.Z 格式，例如 0.1.4 或 v0.1.4")
    return ".".join(match.groups())


def next_patch_version(value: str) -> str:
    major, minor, patch = map(int, normalize_version(value).split("."))
    return f"{major}.{minor}.{patch + 1}"


def replace_version(text: str, pattern: re.Pattern[str], version: str) -> str:
    updated, count = pattern.subn(
        lambda match: match.group(0).replace(match.group(1), version),
        text,
        count=1,
    )
    if count != 1:
        raise RuntimeError("无法唯一定位版本字段")
    return updated


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "自动递增补丁版本，并同步更新 pyproject.toml、"
            "smartstitch/__init__.py 和 uv.lock"
        ),
    )
    parser.add_argument(
        "version",
        nargs="?",
        help="可选的指定版本，例如 0.2.0 或 v1.0.0；省略时自动递增补丁版本",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    pyproject_path = project_root / "pyproject.toml"
    package_path = project_root / "smartstitch" / "__init__.py"
    lock_path = project_root / "uv.lock"

    original = {
        pyproject_path: pyproject_path.read_text(encoding="utf-8"),
        package_path: package_path.read_text(encoding="utf-8"),
        lock_path: lock_path.read_text(encoding="utf-8"),
    }
    project_match = PROJECT_VERSION_PATTERN.search(original[pyproject_path])
    package_match = PACKAGE_VERSION_PATTERN.search(original[package_path])
    if project_match is None or package_match is None:
        raise RuntimeError("无法读取当前 SmartStitch 版本")

    project_version = project_match.group(1)
    package_version = package_match.group(1)
    if project_version != package_version:
        raise RuntimeError(
            "当前版本不一致："
            f"pyproject.toml={project_version}，smartstitch/__init__.py={package_version}"
        )
    try:
        new_version = (
            normalize_version(args.version)
            if args.version is not None
            else next_patch_version(project_version)
        )
    except ValueError as error:
        parser.error(str(error))
    if tuple(map(int, new_version.split("."))) <= tuple(
        map(int, project_version.split("."))
    ):
        raise RuntimeError(
            f"新版本 {new_version} 必须高于当前版本 {project_version}"
        )
    if shutil.which("uv") is None:
        raise RuntimeError("未找到 uv，请先安装 uv 后再更新版本")

    try:
        pyproject_path.write_text(
            replace_version(
                original[pyproject_path], PROJECT_VERSION_PATTERN, new_version
            ),
            encoding="utf-8",
        )
        package_path.write_text(
            replace_version(
                original[package_path], PACKAGE_VERSION_PATTERN, new_version
            ),
            encoding="utf-8",
        )
        subprocess.run(["uv", "lock"], cwd=project_root, check=True)
    except BaseException:
        for path, contents in original.items():
            path.write_text(contents, encoding="utf-8")
        raise

    locked = lock_path.read_text(encoding="utf-8")
    package_block = re.search(
        r'(?ms)^\[\[package\]\]\nname = "smartstitch"\nversion = "([^"]+)"',
        locked,
    )
    if package_block is None or package_block.group(1) != new_version:
        for path, contents in original.items():
            path.write_text(contents, encoding="utf-8")
        raise RuntimeError("uv.lock 未正确更新，已恢复原文件")

    print(f"SmartStitch 版本已从 {project_version} 更新为 {new_version}")
    print("请检查变更、提交到 main，然后创建并推送 Tag：")
    print(f"  git tag v{new_version}")
    print(f"  git push origin v{new_version}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, subprocess.CalledProcessError) as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(1) from error
