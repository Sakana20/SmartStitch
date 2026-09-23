#!/usr/bin/env python3
"""Publish a built SmartStitch DMG to a mounted NAS after integrity checks."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


NAME = re.compile(r"^SmartStitch-v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-macOS-arm64\.dmg$")


def digest_file(path: Path) -> tuple[int, str]:
    total = 0
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            total += len(chunk)
            digest.update(chunk)
    return total, digest.hexdigest()


def publish(source: Path, nas_root: Path, notes: str = "") -> Path:
    match = NAME.fullmatch(source.name)
    if match is None or not source.is_file() or source.is_symlink():
        raise ValueError("请选择正式命名的 SmartStitch-vX.Y.Z-macOS-arm64.dmg")
    subprocess.run(["hdiutil", "verify", str(source)], check=True)
    with tempfile.TemporaryDirectory(prefix="smartstitch-publish-") as directory:
        mountpoint = Path(directory) / "volume"
        mountpoint.mkdir()
        subprocess.run(
            ["hdiutil", "attach", "-readonly", "-nobrowse", "-mountpoint", str(mountpoint), str(source)],
            check=True, stdout=subprocess.DEVNULL,
        )
        try:
            app = mountpoint / "SmartStitch.app"
            with (app / "Contents/Info.plist").open("rb") as stream:
                info = plistlib.load(stream)
            version = ".".join(match.groups())
            if info.get("CFBundleShortVersionString") != version:
                raise ValueError("DMG 内应用版本与文件名不一致")
            binary = app / "Contents/MacOS/SmartStitch"
            arch = subprocess.check_output(["lipo", "-archs", str(binary)], text=True).strip()
            if arch != "arm64":
                raise ValueError("DMG 内应用不是纯 arm64")
            subprocess.run(["codesign", "--verify", "--deep", "--strict", str(app)], check=True)
        finally:
            subprocess.run(["hdiutil", "detach", str(mountpoint)], check=True, stdout=subprocess.DEVNULL)
    expected_size, expected_digest = digest_file(source)
    updates = nas_root / "updates"
    if not nas_root.is_dir():
        raise ValueError("未连接 NAS，请检查共享目录")
    incoming = updates / "incoming"
    releases = updates / "releases"
    incoming.mkdir(parents=True, exist_ok=True)
    releases.mkdir(parents=True, exist_ok=True)
    target = releases / source.name
    if target.exists():
        if target.is_symlink() or digest_file(target) != (expected_size, expected_digest):
            raise ValueError("NAS 上同名版本已存在但内容不同，请核对版本号")
    else:
        temporary = incoming / f"{source.name}.{uuid.uuid4().hex}.part"
        try:
            with source.open("rb") as reader, temporary.open("xb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            if digest_file(temporary) != (expected_size, expected_digest):
                raise ValueError("NAS 上传后校验失败")
            temporary.replace(target)
        finally:
            temporary.unlink(missing_ok=True)
    manifest = {
        "schema_version": 1,
        "version": ".".join(match.groups()),
        "platform": "macos",
        "architecture": "arm64",
        "filename": source.name,
        "size_bytes": expected_size,
        "sha256": expected_digest,
        "published_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(),
        "notes": notes,
    }
    pending = updates / f".latest.{uuid.uuid4().hex}.json"
    try:
        with pending.open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        pending.replace(updates / "latest.json")
    finally:
        pending.unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description="向已挂载 NAS 发布 SmartStitch 更新")
    parser.add_argument("dmg", type=Path, help="已构建并校验的 DMG")
    parser.add_argument("--nas-root", required=True, type=Path, help="NAS 上 Smartstitch 配置根目录")
    parser.add_argument("--notes", default="", help="显示给用户的更新摘要")
    args = parser.parse_args()
    print(f"已发布：{publish(args.dmg, args.nas_root, args.notes)}")


if __name__ == "__main__":
    main()
