from __future__ import annotations

import hashlib
import json
import logging
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Iterable

from .naming import NamingError, category_is_naming_source, parse_asset_naming
from .models import (
    AppConfig,
    Asset,
    MediaProbe,
    ScanResult,
    SourceInventoryItem,
    SourceInventoryResult,
    SourceGroupConfig,
    SourceMode,
    resolve_directory,
)
from .probe_cache import CachedProbe, MediaProbeCache, ProbeFingerprint

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
VISUAL_BORDER_IMAGE_EXTENSIONS = {".png", ".webp"}
VISUAL_BORDER_EXTENSIONS = VISUAL_BORDER_IMAGE_EXTENSIONS | {".mov"}
VISUAL_BORDER_VIDEO_CODECS = {"qtrle", "prores"}
MANAGED_LIBRARY_MARKER = Path(".smartstitch/library.json")
LOGGER = logging.getLogger(__name__)


def _timeout_error(timeout_seconds: float | None) -> ValueError:
    value = f"{timeout_seconds:g}" if timeout_seconds is not None else "未知"
    return ValueError(f"ffprobe 超时（{value} 秒）")


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


def probe_media(
    path: Path,
    image_duration: float = 1.5,
    *,
    timeout_seconds: float | None = None,
) -> MediaProbe:
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
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise _timeout_error(timeout_seconds) from exc
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
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise _timeout_error(timeout_seconds) from exc
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


def _probe_profile(path: Path, image_duration: float) -> str:
    if path.suffix.lower() in IMAGE_EXTENSIONS:
        return f"image-v1:{image_duration:.9g}"
    return "video-v1"


@lru_cache(maxsize=1)
def _ffprobe_signature() -> str:
    executable = shutil.which("ffprobe") or "ffprobe"
    try:
        result = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        version = result.stdout.splitlines()[0].strip() if result.stdout else "unknown"
    except (OSError, subprocess.SubprocessError):
        version = "unknown"
    return f"{executable}|{version}"


class _ProbeSession:
    def __init__(
        self,
        config: AppConfig,
        cache: MediaProbeCache | None,
    ):
        self.config = config
        self.cache = cache if config.scanner.probe_cache_enabled else None
        self.results: dict[tuple[str, str], CachedProbe] = {}
        self.requested_keys: set[tuple[str, str]] = set()
        self.cache_success_hits = 0
        self.cache_failure_hits = 0
        self.probe_count = 0
        self.probe_failures = 0
        self.cache_seconds = 0.0
        self.probe_seconds = 0.0

    @staticmethod
    def _fingerprint(path: Path, image_duration: float) -> ProbeFingerprint:
        stat = path.stat()
        return ProbeFingerprint(
            path=path,
            size_bytes=stat.st_size,
            modified_at_ns=stat.st_mtime_ns,
            profile=_probe_profile(path, image_duration),
        )

    def prefetch(self, requests: Iterable[tuple[Path, float]]) -> None:
        pending: dict[tuple[str, str], tuple[ProbeFingerprint, float]] = {}
        for path, image_duration in requests:
            try:
                fingerprint = self._fingerprint(path, image_duration)
            except OSError as exc:
                key = (str(path), _probe_profile(path, image_duration))
                self.requested_keys.add(key)
                self.results[key] = CachedProbe(error=str(exc))
                continue
            self.requested_keys.add(fingerprint.key)
            if fingerprint.key not in self.results:
                pending[fingerprint.key] = (fingerprint, image_duration)
        if not pending:
            return

        signature = _ffprobe_signature()
        if self.cache is not None:
            cache_started = time.perf_counter()
            try:
                hits = self.cache.get_many(
                    (item[0] for item in pending.values()),
                    ffprobe_signature=signature,
                )
            except Exception:
                hits = {}
            self.cache_seconds += time.perf_counter() - cache_started
            self.cache_success_hits += sum(
                result.probe is not None for result in hits.values()
            )
            self.cache_failure_hits += sum(
                result.probe is None for result in hits.values()
            )
            self.results.update(hits)
            for key in hits:
                pending.pop(key, None)
        if not pending:
            return

        fresh: list[tuple[ProbeFingerprint, CachedProbe]] = []
        probe_started = time.perf_counter()

        def execute(
            fingerprint: ProbeFingerprint, image_duration: float
        ) -> CachedProbe:
            try:
                return CachedProbe(
                    probe=probe_media(
                        fingerprint.path,
                        image_duration,
                        timeout_seconds=self.config.scanner.probe_timeout_seconds,
                    )
                )
            except Exception as exc:
                return CachedProbe(error=str(exc))

        with ThreadPoolExecutor(
            max_workers=self.config.scanner.probe_concurrency,
            thread_name_prefix="ffprobe",
        ) as pool:
            futures = {
                pool.submit(execute, fingerprint, image_duration): fingerprint
                for fingerprint, image_duration in pending.values()
            }
            for future in as_completed(futures):
                fingerprint = futures[future]
                result = future.result()
                self.results[fingerprint.key] = result
                fresh.append((fingerprint, result))
        self.probe_seconds += time.perf_counter() - probe_started
        self.probe_count += len(fresh)
        self.probe_failures += sum(result.probe is None for _, result in fresh)

        if self.cache is not None:
            try:
                self.cache.put_many(
                    fresh,
                    ffprobe_signature=signature,
                    failure_ttl_seconds=(
                        self.config.scanner.probe_failure_ttl_seconds
                    ),
                )
            except Exception:
                pass

    def get(self, path: Path, image_duration: float) -> CachedProbe:
        key = (str(path), _probe_profile(path, image_duration))
        if key not in self.results:
            self.prefetch([(path, image_duration)])
        return self.results[key]


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


def validate_visual_border_media(
    path: Path,
    *,
    output_width: int | None = None,
    output_height: int | None = None,
    exact_size: bool = False,
    probe: MediaProbe | None = None,
) -> MediaProbe:
    """Validate one transparent border independently from project discovery."""
    extension = path.suffix.lower()
    if extension not in VISUAL_BORDER_EXTENSIONS:
        raise ValueError("边框仅支持 qtrle 或 ProRes 4444 MOV、透明 PNG 或透明 WebP")
    probe = probe or probe_media(path)
    if extension == ".mov" and probe.video_codec not in VISUAL_BORDER_VIDEO_CODECS:
        raise ValueError("MOV 边框必须使用 QuickTime Animation/qtrle 或 ProRes 4444 编码")
    if not probe.has_alpha:
        raise ValueError(f"边框像素格式 {probe.pixel_format or '未知'} 不含 Alpha 通道")
    if exact_size and (
        probe.width != output_width or probe.height != output_height
    ):
        raise ValueError(
            f"边框尺寸 {probe.width}x{probe.height} 与输出画布 "
            f"{output_width}x{output_height} 不一致"
        )
    if not _contains_transparent_samples(path, probe):
        raise ValueError("边框抽样帧完全不透明，可能覆盖整个主画面")
    return probe


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


@dataclass(frozen=True)
class _PreparedGroup:
    directory: Path
    explicit: dict[Path, object]
    candidates: list[Path]


def _prepare_group(config: AppConfig, group: SourceGroupConfig) -> _PreparedGroup:
    directory, discovered = _discover_paths(config, group)
    explicit: dict[Path, object] = {}
    for item in group.items:
        item_path = Path(item.path).expanduser()
        if not item_path.is_absolute():
            item_path = directory / item_path
        explicit[item_path.resolve()] = item
    # `items` stores per-file weights/tags and can outlive the underlying NAS file.
    # A deleted file is no longer a scan candidate: keeping it here would turn a
    # normal library deletion into a permanent "文件不存在 / 异常" row. Existing
    # but unreadable files remain candidates and are still reported as damaged.
    existing_explicit = {path for path in explicit if path.is_file()}
    candidates = sorted(
        set(discovered) | existing_explicit, key=lambda item: str(item).casefold()
    )
    return _PreparedGroup(
        directory=directory,
        explicit=explicit,
        candidates=candidates,
    )


def scan_source_inventory(config: AppConfig) -> SourceInventoryResult:
    """Enumerate every source group without probing media or applying its mode."""
    sources: dict[str, SourceInventoryItem] = {}
    for category, group in config.sources.items():
        directory = resolve_directory(config, group.directory)
        try:
            if not directory.is_dir():
                status = "unavailable"
                count = 0
            else:
                prepared = _prepare_group(config, group)
                count = len(prepared.candidates)
                status = "available" if count else "empty"
        except OSError:
            status = "unavailable"
            count = 0
        sources[category] = SourceInventoryItem(
            category=category,
            label=group.label,
            directory=str(directory),
            directory_status=status,
            discovered_count=count,
            enabled=group.mode != SourceMode.DISABLED,
        )
    return SourceInventoryResult(config_id=config.id, sources=sources)


def scan_group(
    config: AppConfig,
    category: str,
    group: SourceGroupConfig,
    *,
    prepared: _PreparedGroup | None = None,
    probe_session: _ProbeSession | None = None,
) -> tuple[list[Asset], list[str]]:
    if group.mode == SourceMode.DISABLED:
        return [], []
    prepared = prepared or _prepare_group(config, group)
    directory = prepared.directory
    errors: list[str] = []
    category_name = f"{group.label}（{category}）" if group.label else category
    if not directory.exists():
        message = f"{category_name}: 目录不存在或外接磁盘未挂载: {directory}"
        if group.mode == SourceMode.REQUIRED:
            errors.append(message)
        return [], errors

    assets: list[Asset] = []
    for path in prepared.candidates:
        # The file can be removed from the NAS after discovery but before this
        # loop. Treat that race exactly like a file absent at discovery time.
        if not path.is_file():
            continue
        item = prepared.explicit.get(path)
        enabled = item.enabled if item is not None else True
        weight = item.weight if item is not None else group.default_weight
        tags = list(item.tags) if item is not None else []
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue
        asset = Asset(
            id=_asset_id(category, path),
            category=category,
            path=str(path),
            name=path.name,
            media_type="image" if path.suffix.lower() in IMAGE_EXTENSIONS else "video",
            enabled=enabled,
            weight=weight,
            tags=tags,
            exists=True,
            valid=True,
            error=None,
            size_bytes=stat.st_size,
            modified_at=stat.st_mtime,
        )
        result = (
            probe_session.get(path, group.image_duration_seconds)
            if probe_session is not None
            else None
        )
        try:
            if result is not None and result.probe is not None:
                asset.probe = result.probe
            elif result is not None:
                raise ValueError(result.error or "ffprobe 无法读取媒体")
            else:
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


def scan_fixed_overlay(
    config: AppConfig,
    probe_session: _ProbeSession | None = None,
) -> tuple[list[Asset], list[str]]:
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
            if probe_session is None:
                asset.probe = probe_media(path, group.image_duration_seconds)
            else:
                result = probe_session.get(path, group.image_duration_seconds)
                if result.probe is None:
                    raise ValueError(result.error or "ffprobe 无法读取图片")
                asset.probe = result.probe
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
    global_assets: list[Asset] | None = None,
    probe_session: _ProbeSession | None = None,
) -> tuple[list[Asset], list[str], list[str]]:
    settings = config.visual_dedup
    group = settings.border_overlay
    if not settings.enabled or group.mode == SourceMode.DISABLED:
        return [], [], []

    if global_assets is not None and group.source == "global_library" and not group.file.strip():
        assets = list(global_assets)
        if group.selection_mode == "fixed":
            if not group.fixed_asset_id:
                return assets, ["visual_border: 固定模式必须选择一个全局边框"], []
            matched = [asset for asset in assets if asset.id == group.fixed_asset_id]
            if not matched:
                return assets, ["visual_border: 固定的全局边框不存在或已停用"], []
            assets = matched
        selectable = [asset for asset in assets if asset.selectable]
        errors: list[str] = []
        if group.mode == SourceMode.REQUIRED and not selectable:
            detail = next((asset.error for asset in assets if asset.error), None)
            errors.append(
                "visual_border: "
                + (detail or "全局边框库没有与当前输出兼容的可用素材")
            )
        warnings = [
            f"visual_border: {asset.name}: {asset.error}"
            for asset in assets
            if not asset.valid and asset.error
        ]
        warnings.extend(
            f"visual_border: {asset.name} 包含音轨，生成时将忽略该音轨"
            for asset in selectable
            if asset.probe and asset.probe.has_audio
        )
        return assets, errors, warnings

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
        error = "边框仅支持 qtrle 或 ProRes 4444 MOV、透明 PNG 或透明 WebP"
    else:
        try:
            cached_probe: MediaProbe | None = None
            if probe_session is not None:
                result = probe_session.get(path, 1.5)
                if result.probe is None:
                    raise ValueError(result.error or "ffprobe 无法读取边框")
                cached_probe = result.probe
            probe = validate_visual_border_media(
                path,
                output_width=config.output.width,
                output_height=config.output.height,
                exact_size=group.scale_mode == "exact",
                probe=cached_probe,
            )
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


def scan_config(
    config: AppConfig,
    global_visual_borders: list[Asset] | dict[str, list[Asset]] | None = None,
    probe_cache: MediaProbeCache | None = None,
) -> ScanResult:
    scan_started = time.perf_counter()
    assets: dict[str, list[Asset]] = {}
    errors: list[str] = []
    warnings: list[str] = []
    probe_session = _ProbeSession(config, probe_cache)
    prepared_groups = {
        category: _prepare_group(config, group)
        for category, group in config.sources.items()
        if group.mode != SourceMode.DISABLED
    }
    probe_session.prefetch(
        (path, group.image_duration_seconds)
        for category, group in config.sources.items()
        if group.mode != SourceMode.DISABLED
        for path in prepared_groups[category].candidates
        if path.is_file()
    )
    for category, group in config.sources.items():
        group_assets, group_errors = scan_group(
            config,
            category,
            group,
            prepared=prepared_groups.get(category),
            probe_session=probe_session,
        )
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
        for category in naming_categories:
            group = config.sources[category]
            label = group.label.strip() or category
            for asset in assets.get(category, []):
                if not asset.selectable:
                    continue
                try:
                    asset.naming_metadata = parse_asset_naming(config, asset)
                except NamingError as exc:
                    errors.append(
                        f"{label}（{category}）: 无法从“{asset.name}”"
                        f"识别达人名和限制日期: {exc}"
                    )

    overlays, overlay_errors = scan_fixed_overlay(config, probe_session)
    assets["benefit_overlay"] = overlays
    errors.extend(overlay_errors)
    if any(not asset.valid for asset in overlays):
        warnings.append("benefit_overlay: 存在不可用图片")
    effect_asset_map = (
        global_visual_borders if isinstance(global_visual_borders, dict) else {}
    )
    legacy_global_borders = (
        global_visual_borders if isinstance(global_visual_borders, list) else None
    )
    if effect_asset_map:
        legacy_layer = next(
            (
                layer for layer in config.visual_dedup.resolved_effect_layers()
                if layer.type == "overlay"
                and layer.library_id in {"effect_1", "visual-border"}
            ),
            None,
        )
        if legacy_layer is not None:
            legacy_global_borders = effect_asset_map.get(
                f"visual_effect:{legacy_layer.layer_id}", []
            )
    borders, border_errors, border_warnings = scan_visual_border(
        config, legacy_global_borders, probe_session
    )
    assets["visual_border"] = borders
    errors.extend(border_errors)
    warnings.extend(border_warnings)
    if any(not asset.valid for asset in borders):
        warnings.append("visual_border: 存在不可用边框")

    if config.visual_dedup.enabled:
        for layer in config.visual_dedup.resolved_effect_layers():
            if layer.type != "overlay" or not layer.enabled:
                continue
            category = f"visual_effect:{layer.layer_id}"
            layer_assets = list(effect_asset_map.get(category, []))
            if layer.layer_id == "legacy-visual-border":
                layer_assets = list(borders)
            if layer.selection_mode == "fixed":
                if not layer.fixed_asset_id:
                    errors.append(f"{layer.name}: 固定模式必须选择一个特效素材")
                else:
                    matched = [
                        asset for asset in layer_assets
                        if asset.id == layer.fixed_asset_id
                    ]
                    if not matched:
                        errors.append(f"{layer.name}: 固定素材不存在或已停用")
                    layer_assets = matched
            selectable = [asset for asset in layer_assets if asset.selectable]
            if layer.required and not selectable:
                detail = next(
                    (asset.error for asset in layer_assets if asset.error), None
                )
                errors.append(
                    f"{layer.name}: "
                    + (detail or "全局特效库没有与当前输出兼容的可用素材")
                )
            warnings.extend(
                f"{layer.name}: {asset.name}: {asset.error}"
                for asset in layer_assets
                if not asset.valid and asset.error
            )
            warnings.extend(
                f"{layer.name}: {asset.name} 包含音轨，生成时将忽略该音轨"
                for asset in selectable
                if asset.probe and asset.probe.has_audio
            )
            assets[category] = layer_assets
    result = ScanResult(
        config_id=config.id,
        assets=assets,
        errors=errors,
        warnings=warnings,
    )
    LOGGER.info(
        "media scan config=%s candidates=%d cache_success_hits=%d "
        "cache_failure_hits=%d ffprobe_calls=%d ffprobe_failures=%d "
        "concurrency=%d cache_seconds=%.3f ffprobe_seconds=%.3f total_seconds=%.3f",
        config.id,
        len(probe_session.requested_keys),
        probe_session.cache_success_hits,
        probe_session.cache_failure_hits,
        probe_session.probe_count,
        probe_session.probe_failures,
        config.scanner.probe_concurrency,
        probe_session.cache_seconds,
        probe_session.probe_seconds,
        time.perf_counter() - scan_started,
    )
    return result
