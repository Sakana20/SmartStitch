from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .models import (
    AppConfig,
    Asset,
    MediaProbe,
    ScanResult,
    SourceGroupConfig,
    SourceMode,
    resolve_directory,
)

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}


def _asset_id(category: str, path: Path) -> str:
    digest = hashlib.sha1(f"{category}\0{path}".encode("utf-8")).hexdigest()[:16]
    return f"{category}-{digest}"


def _fps(value: str | None) -> float | None:
    if not value or value == "0/0":
        return None
    try:
        numerator, denominator = value.split("/", 1)
        return float(numerator) / float(denominator)
    except (ValueError, ZeroDivisionError):
        return None


def probe_media(path: Path, image_duration: float = 1.5) -> MediaProbe:
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        command = [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,codec_name",
            "-of",
            "json",
            str(path),
        ]
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            raise ValueError(result.stderr.strip() or "ffprobe 无法读取图片")
        data = json.loads(result.stdout)
        streams = data.get("streams", [])
        if not streams:
            raise ValueError("图片中没有可识别的画面")
        stream = streams[0]
        return MediaProbe(
            duration=image_duration,
            width=stream.get("width"),
            height=stream.get("height"),
            video_codec=stream.get("codec_name"),
        )

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of",
        "json",
        str(path),
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise ValueError(result.stderr.strip() or "ffprobe 无法读取视频")
    data = json.loads(result.stdout)
    video = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    audio = next((s for s in data.get("streams", []) if s.get("codec_type") == "audio"), None)
    if video is None:
        raise ValueError("文件中没有视频流")
    duration = float(data.get("format", {}).get("duration") or 0)
    if duration <= 0:
        raise ValueError("视频时长无效")
    return MediaProbe(
        duration=duration,
        width=video.get("width"),
        height=video.get("height"),
        fps=_fps(video.get("r_frame_rate")),
        video_codec=video.get("codec_name"),
        has_audio=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=audio.get("channels") if audio else None,
    )


def probe_config_audio(config: AppConfig, requested_path: Path) -> MediaProbe:
    """Validate and probe one configured audio/video path without scanning every source."""
    path = requested_path.expanduser().resolve()
    allowed = False
    for group in config.sources.values():
        if group.mode == SourceMode.DISABLED:
            continue
        directory = resolve_directory(config, group.directory).expanduser().resolve()
        explicit_paths = set()
        for item in group.items:
            item_path = Path(item.path).expanduser()
            if not item_path.is_absolute():
                item_path = directory / item_path
            explicit_paths.add(item_path.resolve())
        if path in explicit_paths:
            allowed = True
            break

        name = path.name
        visible = not (
            name in config.scanner.ignore_names
            or any(name.startswith(prefix) for prefix in config.scanner.ignore_prefixes)
            or (config.scanner.ignore_hidden_files and name.startswith("."))
        )
        within_directory = (
            path.is_relative_to(directory)
            if config.scanner.recursive
            else path.parent == directory
        )
        if visible and within_directory and path.suffix.lower() in set(group.extensions):
            allowed = True
            break

    if not allowed:
        raise ValueError("试听文件不在当前配置的有效音频素材中")
    if not path.exists() or not path.is_file():
        raise ValueError("试听文件不存在或外接磁盘未挂载")
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        raise ValueError("试听文件必须是视频")

    probe = probe_media(path)
    if not probe.has_audio:
        raise ValueError("试听文件没有音轨")
    return probe


def _discover_paths(config: AppConfig, group: SourceGroupConfig) -> tuple[Path, list[Path]]:
    directory = resolve_directory(config, group.directory)
    if not directory.exists() or not directory.is_dir():
        return directory, []
    iterator = directory.rglob("*") if config.scanner.recursive else directory.iterdir()
    paths = []
    allowed = set(group.extensions)
    for path in iterator:
        if not path.is_file():
            continue
        name = path.name
        if name in config.scanner.ignore_names:
            continue
        if any(name.startswith(prefix) for prefix in config.scanner.ignore_prefixes):
            continue
        if config.scanner.ignore_hidden_files and name.startswith("."):
            continue
        if path.suffix.lower() not in allowed:
            continue
        paths.append(path.resolve())
    return directory, sorted(paths, key=lambda item: str(item).casefold())


def scan_group(config: AppConfig, category: str, group: SourceGroupConfig) -> tuple[list[Asset], list[str]]:
    if group.mode == SourceMode.DISABLED:
        return [], []
    directory, discovered = _discover_paths(config, group)
    errors: list[str] = []
    if not directory.exists():
        message = f"{category}: 目录不存在或外接磁盘未挂载: {directory}"
        if group.mode == SourceMode.REQUIRED:
            errors.append(message)
        return [], errors

    explicit: dict[Path, object] = {}
    for item in group.items:
        item_path = Path(item.path).expanduser()
        if not item_path.is_absolute():
            item_path = directory / item_path
        explicit[item_path.resolve()] = item

    candidates = sorted(set(discovered) | set(explicit), key=lambda item: str(item).casefold())
    assets: list[Asset] = []
    for path in candidates:
        item = explicit.get(path)
        enabled = item.enabled if item is not None else True
        weight = item.weight if item is not None else group.default_weight
        tags = list(item.tags) if item is not None else []
        exists = path.exists() and path.is_file()
        stat = path.stat() if exists else None
        asset = Asset(
            id=_asset_id(category, path),
            category=category,
            path=str(path),
            name=path.name,
            media_type="image" if path.suffix.lower() in IMAGE_EXTENSIONS else "video",
            enabled=enabled,
            weight=weight,
            tags=tags,
            exists=exists,
            valid=exists,
            error=None if exists else "文件不存在",
            size_bytes=stat.st_size if stat else None,
            modified_at=stat.st_mtime if stat else None,
        )
        if exists:
            try:
                asset.probe = probe_media(path, group.image_duration_seconds)
            except Exception as exc:
                asset.valid = False
                asset.error = str(exc)
        assets.append(asset)

    selectable = [asset for asset in assets if asset.selectable]
    if group.mode == SourceMode.REQUIRED and not selectable:
        errors.append(f"{category}: 必需素材组没有可用且权重大于 0 的素材")
    return assets, errors


def scan_fixed_overlay(config: AppConfig) -> tuple[list[Asset], list[str]]:
    group = config.benefit_overlays
    if group.mode == SourceMode.DISABLED:
        return [], []
    if not group.file.strip():
        message = "benefit_overlay: 请指定唯一的风险提示语图片文件"
        return [], [message] if group.mode == SourceMode.REQUIRED else []

    path = Path(group.file).expanduser()
    if not path.is_absolute():
        path = Path(config.source_root).expanduser() / path
    path = path.resolve()
    exists = path.exists() and path.is_file()
    error: str | None = None
    if path.exists() and path.is_dir():
        error = "风险提示语图片必须指定具体图片文件，不能填写目录"
    elif not exists:
        error = "图片文件不存在或外接磁盘未挂载"
    elif path.suffix.lower() not in IMAGE_EXTENSIONS:
        error = f"不支持的图片格式: {path.suffix or '无扩展名'}"

    asset = Asset(
        id=_asset_id("benefit_overlay", path),
        category="benefit_overlay",
        path=str(path),
        name=path.name,
        media_type="image",
        enabled=True,
        weight=1,
        exists=exists,
        valid=error is None,
        error=error,
        size_bytes=path.stat().st_size if exists else None,
        modified_at=path.stat().st_mtime if exists else None,
    )
    if asset.valid:
        try:
            asset.probe = probe_media(path, group.image_duration_seconds)
        except Exception as exc:
            asset.valid = False
            asset.error = str(exc)

    errors = []
    if group.mode == SourceMode.REQUIRED and not asset.selectable:
        errors.append(f"benefit_overlay: {asset.error or '唯一风险提示语图片不可用'}: {path}")
    return [asset], errors


def scan_config(config: AppConfig) -> ScanResult:
    assets: dict[str, list[Asset]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    for category, group in config.sources.items():
        group_assets, group_errors = scan_group(config, category, group)
        assets[category] = group_assets
        errors.extend(group_errors)
        invalid = [asset.name for asset in group_assets if not asset.valid]
        if invalid:
            warnings.append(f"{category}: {len(invalid)} 个文件不可用")

    overlays, overlay_errors = scan_fixed_overlay(config)
    assets["benefit_overlay"] = overlays
    errors.extend(overlay_errors)
    if any(not asset.valid for asset in overlays):
        warnings.append("benefit_overlay: 存在不可用图片")
    return ScanResult(config_id=config.id, assets=assets, errors=errors, warnings=warnings)
