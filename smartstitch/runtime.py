from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


APP_NAME = "SmartStitch"
DATA_DIRECTORY_ENV = "SMARTSTITCH_DATA_DIRECTORY"


def is_frozen() -> bool:
    """Return whether SmartStitch is running from a PyInstaller bundle."""

    return bool(getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"))


def resource_root() -> Path:
    """Read-only root containing packaged frontend/config/bin resources."""

    if is_frozen():
        return Path(sys._MEIPASS).resolve()  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent


def application_support_root() -> Path:
    override = os.environ.get(DATA_DIRECTORY_ENV, "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / APP_NAME


def writable_data_directory(application_root: Path) -> Path:
    if is_frozen():
        return application_support_root() / "data"
    return application_root / "data"


def writable_config_directory(application_root: Path) -> Path:
    if is_frozen():
        return application_support_root() / "config"
    return application_root / "config"


def seed_packaged_configs(source: Path, destination: Path) -> None:
    """Copy committed defaults once without overwriting user-edited configs."""

    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_dir() or source.resolve() == destination.resolve():
        return
    for path in source.glob("*.yaml"):
        target = destination / path.name
        if not target.exists():
            shutil.copy2(path, target)


def configure_bundled_media_tools(root: Path | None = None) -> Path | None:
    """Put packaged FFmpeg ahead of every system path when it is present."""

    binary_directory = (root or resource_root()) / "bin"
    required = (binary_directory / "ffmpeg", binary_directory / "ffprobe")
    if not all(path.is_file() and os.access(path, os.X_OK) for path in required):
        return None
    current = os.environ.get("PATH", "")
    entries = [entry for entry in current.split(os.pathsep) if entry]
    resolved_binary_directory = str(binary_directory.resolve())
    os.environ["PATH"] = os.pathsep.join(
        [resolved_binary_directory]
        + [entry for entry in entries if entry != resolved_binary_directory]
    )
    return binary_directory
