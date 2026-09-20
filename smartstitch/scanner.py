from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from .naming import NamingError, category_is_naming_source, parse_asset_naming
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
VISUAL_BORDER_IMAGE_EXTENSIONS = {".png", ".webp"}
VISUAL_BORDER_EXTENSIONS = VISUAL_BORDER_IMAGE_EXTENSIONS | {".mov"}
MANAGED_LIBRARY_MARKER = Path(".smartstitch/library.json")


def _pixel_format_has_alpha(pixel_format: str | None) -> bool:
    if not pixel_format:
        return False
    value = pixel_format.lower()
    return value in {
        "argb",
        "rgba",
        "abgr",
        "bgra",
        "ya8",
        "ya16be",
        "ya16le",
    } or value.startswith(("yuva", "gbrap", "rgba", "bgra"))


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
            "stream=width,height,codec_name,pix_fmt",
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
            pixel_format=stream.get("pix_fmt"),
            has_alpha=_pixel_format_has_alpha(stream.get("pix_fmt")),
        )

    command = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration:stream=codec_type,codec_name,pix_fmt,width,height,r_frame_rate,sample_rate,channels",
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
        pixel_format=video.get("pix_fmt"),
        has_alpha=_pixel_format_has_alpha(video.get("pix_fmt")),
        has_audio=audio is not None,
        audio_codec=audio.get("codec_name") if audio else None,
        sample_rate=int(audio["sample_rate"]) if audio and audio.get("sample_rate") else None,
        channels=audio.get("channels") if audio else None,
    )


def _contains_transparent_samples(path: Path, probe: MediaProbe) -> bool:
    """Return whether representative frames contain at least one non-opaque pixel."""
    sample_times = [0.0]
    if path.suffix.lower() not in IMAGE_EXTENSIONS and probe.duration > 0:
        sample_times.extend([probe.duration / 2, max(0.0, probe.duration - 0.05)])
    for timestamp in sample_times:
        command = ["ffmpeg", "-hide_banner", "-loglevel", "error"]
        if timestamp > 0:
            command.extend(["-ss", f"{timestamp:.6f}"])
        command.extend(
            [
                "-i",
                str(path),
                "-frames:v",
                "1",
                "-vf",
                "alphaextract,scale=64:64",
                "-f",
                "rawvideo",
                "-pix_fmt",
                "gray",
                "pipe:1",
            ]
        )
        result = subprocess.run(command, capture_output=True, check=False)
        if result.returncode != 0:
            detail = result.stderr.decode("utf-8", errors="replace").strip()
            raise ValueError(detail or "无法检查透明通道")
        if result.stdout and any(value < 255 for value in result.stdout):
            return True
    return False


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
    category_name = f"{group.label}（{category}）" if group.label else category
    if not directory.exists():
        message = f"{category_name}: 目录不存在或外接磁盘未挂载: {directory}"
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
        errors.append(f"{category_name}: 必需素材组没有可用且权重大于 0 的素材")
    return assets, errors


def _discover_managed_overlay(config: AppConfig) -> tuple[Path | None, str | None]:
    """Find the single overlay stored in a managed standard library."""
    root = Path(config.source_root).expanduser().resolve()
    marker_path = root / MANAGED_LIBRARY_MARKER
    if not marker_path.is_file():
        return None, None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        paths = marker["paths"]
        relative = paths.get("overlays")
        if not relative and marker.get("workflow_type") == "generic":
            # Compatibility with generic layout v2 projects created before
            # the risk-overlay directory was added.
            relative = "风险提示语图片"
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("缺少 paths.overlays")
        directory = (root / relative).resolve()
        directory.relative_to(root)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, "benefit_overlay: 标准视频库的风险提示语图片目录配置无效"

    if not directory.is_dir():
        return None, f"benefit_overlay: 风险提示语图片目录不存在: {directory}"

    candidates = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name in config.scanner.ignore_names:
            continue
        if any(name.startswith(prefix) for prefix in config.scanner.ignore_prefixes):
            continue
        if config.scanner.ignore_hidden_files and name.startswith("."):
            continue
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            candidates.append(path.resolve())
    candidates.sort(key=lambda item: str(item).casefold())
    if len(candidates) > 1:
        return None, (
            "benefit_overlay: 风险提示语图片目录只能放置一张图片，"
            f"当前识别到 {len(candidates)} 张: {directory}"
        )
    return (candidates[0], None) if candidates else (None, None)


def scan_fixed_overlay(config: AppConfig) -> tuple[list[Asset], list[str]]:
    group = config.benefit_overlays
    if group.mode == SourceMode.DISABLED:
        return [], []
    if group.file.strip():
        path = Path(group.file).expanduser()
        if not path.is_absolute():
            path = Path(config.source_root).expanduser() / path
        path = path.resolve()
    else:
        path, discovery_error = _discover_managed_overlay(config)
        if discovery_error:
            return [], [discovery_error]
        if path is None:
            message = "benefit_overlay: 请指定唯一的风险提示语图片文件"
            return [], [message] if group.mode == SourceMode.REQUIRED else []

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


def _discover_managed_visual_border(
    config: AppConfig,
) -> tuple[Path | None, str | None]:
    root = Path(config.source_root).expanduser().resolve()
    marker_path = root / MANAGED_LIBRARY_MARKER
    if not marker_path.is_file():
        return None, None
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        paths = marker["paths"]
        relative = paths.get("visual_borders") or "视觉去重边框"
        if not isinstance(relative, str) or not relative.strip():
            raise ValueError("缺少 paths.visual_borders")
        directory = (root / relative).resolve()
        directory.relative_to(root)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, "visual_border: 标准视频库的视觉去重边框目录配置无效"
    if not directory.is_dir():
        return None, f"visual_border: 视觉去重边框目录不存在: {directory}"

    candidates = []
    for path in directory.iterdir():
        if not path.is_file():
            continue
        name = path.name
        if name in config.scanner.ignore_names:
            continue
        if any(name.startswith(prefix) for prefix in config.scanner.ignore_prefixes):
            continue
        if config.scanner.ignore_hidden_files and name.startswith("."):
            continue
        if path.suffix.lower() in VISUAL_BORDER_EXTENSIONS:
            candidates.append(path.resolve())
    candidates.sort(key=lambda item: str(item).casefold())
    if len(candidates) > 1:
        return None, (
            "visual_border: 视觉去重边框目录只能放置一个素材，"
            f"当前识别到 {len(candidates)} 个: {directory}"
        )
    return (candidates[0], None) if candidates else (None, None)


def scan_visual_border(
    config: AppConfig,
) -> tuple[list[Asset], list[str], list[str]]:
    settings = config.visual_dedup
    group = settings.border_overlay
    if not settings.enabled or group.mode == SourceMode.DISABLED:
        return [], [], []

    explicit_file = bool(group.file.strip())
    if explicit_file:
        path = Path(group.file).expanduser()
        if not path.is_absolute():
            path = Path(config.source_root).expanduser() / path
        path = path.resolve()
    else:
        path, discovery_error = _discover_managed_visual_border(config)
        if discovery_error:
            return [], [discovery_error], []
        if path is None:
            message = "visual_border: 请选择透明 MOV、PNG 或 WebP 边框"
            return [], [message] if group.mode == SourceMode.REQUIRED else [], []

    exists = path.exists() and path.is_file()
    extension = path.suffix.lower()
    error: str | None = None
    probe: MediaProbe | None = None
    media_type = "image" if extension in VISUAL_BORDER_IMAGE_EXTENSIONS else "video"
    if path.exists() and path.is_dir():
        error = "视觉去重边框必须指定具体文件，不能填写目录"
    elif not exists:
        error = "边框文件不存在或外接磁盘未挂载"
    elif extension not in VISUAL_BORDER_EXTENSIONS:
        error = "边框仅支持 qtrle/ARGB MOV、透明 PNG 或透明 WebP"
    else:
        try:
            probe = probe_media(path)
            if extension == ".mov" and probe.video_codec != "qtrle":
                error = "MOV 边框必须使用 QuickTime Animation/qtrle 编码"
            elif not probe.has_alpha:
                error = f"边框像素格式 {probe.pixel_format or '未知'} 不含 Alpha 通道"
            elif group.scale_mode == "exact" and (
                probe.width != config.output.width or probe.height != config.output.height
            ):
                error = (
                    f"边框尺寸 {probe.width}x{probe.height} 与输出画布 "
                    f"{config.output.width}x{config.output.height} 不一致"
                )
            elif not _contains_transparent_samples(path, probe):
                error = "边框抽样帧完全不透明，可能覆盖整个主画面"
        except Exception as exc:
            error = str(exc)

    stat = path.stat() if exists else None
    asset = Asset(
        id=_asset_id("visual_border", path),
        category="visual_border",
        path=str(path),
        name=path.name,
        media_type=media_type,
        enabled=True,
        weight=1,
        exists=exists,
        valid=error is None,
        error=error,
        size_bytes=stat.st_size if stat else None,
        modified_at=stat.st_mtime if stat else None,
        probe=probe,
    )
    errors = []
    if not asset.selectable and (group.mode == SourceMode.REQUIRED or explicit_file or exists):
        errors.append(f"visual_border: {asset.error or '边框不可用'}: {path}")
    warnings = []
    if asset.valid and probe and probe.has_audio:
        warnings.append("visual_border: 边框素材包含音轨，生成时将忽略该音轨")
    return [asset], errors, warnings


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
            label = group.label.strip()
            name = f"{label}（{category}）" if label else category
            warnings.append(f"{name}: {len(invalid)} 个文件不可用")

    if config.workflow_type == "generic" and config.output.naming.enabled:
        naming_categories = [
            category
            for category in config.timeline
            if config.sources[category].mode != SourceMode.DISABLED
            and category_is_naming_source(config, category)
        ]
        if not naming_categories:
            errors.append("成片命名规则没有匹配任何已启用的视频库")
        selectable_count = 0
        for category in naming_categories:
            group = config.sources[category]
            label = group.label.strip() or category
            for asset in assets.get(category, []):
                if not asset.selectable:
                    continue
                selectable_count += 1
                try:
                    asset.naming_metadata = parse_asset_naming(config, asset)
                except NamingError as exc:
                    errors.append(
                        f"{label}（{category}）: 无法从“{asset.name}”"
                        f"识别达人名和限制日期: {exc}"
                    )
        if naming_categories and selectable_count == 0:
            errors.append("参与成片命名的视频库中没有可用素材")

    overlays, overlay_errors = scan_fixed_overlay(config)
    assets["benefit_overlay"] = overlays
    errors.extend(overlay_errors)
    if any(not asset.valid for asset in overlays):
        warnings.append("benefit_overlay: 存在不可用图片")
    borders, border_errors, border_warnings = scan_visual_border(config)
    assets["visual_border"] = borders
    errors.extend(border_errors)
    warnings.extend(border_warnings)
    if any(not asset.valid for asset in borders):
        warnings.append("visual_border: 存在不可用边框")
    return ScanResult(config_id=config.id, assets=assets, errors=errors, warnings=warnings)
