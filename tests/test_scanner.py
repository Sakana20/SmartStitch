from __future__ import annotations

import json
import subprocess
import threading
import time
from collections import Counter

import pytest
import smartstitch.scanner as scanner_module
from smartstitch.database import SQLiteStore
from smartstitch.models import AppConfig, AssetItemConfig, MediaProbe
from smartstitch.probe_cache import MediaProbeCache
from smartstitch.scanner import scan_config, scan_visual_border


def generate_qtrle_border(path, *, alpha=True, width=180, height=320):
    pixel_format = "argb" if alpha else "rgb24"
    source = f"color=c=black@0.0:s={width}x{height}:r=12:d=0.4,format={pixel_format}"
    if alpha:
        source += ",drawbox=x=0:y=0:w=iw:h=20:color=red@1:t=fill:replace=1"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", source,
            "-c:v", "qtrle", "-pix_fmt", pixel_format, str(path),
        ],
        check=True,
    )


def generate_prores_4444_border(path, *, width=180, height=320):
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            (
                f"color=c=black@0.0:s={width}x{height}:r=12:d=0.4,"
                "format=yuva444p10le,"
                "drawbox=x=0:y=0:w=iw:h=20:color=red@1:t=fill:replace=1"
            ),
            "-c:v", "prores_ks", "-profile:v", "4444",
            "-pix_fmt", "yuva444p10le", str(path),
        ],
        check=True,
    )


def visual_border_config(tmp_path, border_path):
    return AppConfig.model_validate(
        {
            "id": "visual-border",
            "name": "视觉边框",
            "source_root": str(tmp_path),
            "timeline": ["hook", "benefit_1", "ending"],
            "sources": {
                category: {"directory": category}
                for category in ("hook", "benefit_1", "ending")
            },
            "benefit_overlays": {"mode": "disabled", "file": ""},
            "visual_dedup": {
                "enabled": True,
                "background": {"enabled": False},
                "border_overlay": {
                    "mode": "required",
                    "file": str(border_path),
                    "scale_mode": "exact",
                },
            },
            "output": {
                "directory": str(tmp_path / "out"),
                "width": 180,
                "height": 320,
            },
        }
    )


def test_visual_border_accepts_transparent_qtrle_and_rejects_opaque_qtrle(tmp_path):
    transparent = tmp_path / "transparent.mov"
    generate_qtrle_border(transparent, alpha=True)
    assets, errors, warnings = scan_visual_border(
        visual_border_config(tmp_path, transparent)
    )

    assert errors == []
    assert warnings == []
    assert assets[0].valid
    assert assets[0].probe.video_codec == "qtrle"
    assert assets[0].probe.pixel_format == "argb"
    assert assets[0].probe.has_alpha is True

    opaque = tmp_path / "opaque.mov"
    generate_qtrle_border(opaque, alpha=False)
    assets, errors, _warnings = scan_visual_border(
        visual_border_config(tmp_path, opaque)
    )
    assert not assets[0].valid
    assert "不含 Alpha" in errors[0]


def test_visual_border_accepts_transparent_prores_4444(tmp_path):
    border = tmp_path / "prores-4444.mov"
    generate_prores_4444_border(border)

    assets, errors, warnings = scan_visual_border(
        visual_border_config(tmp_path, border)
    )

    assert errors == []
    assert warnings == []
    assert assets[0].valid
    assert assets[0].probe.video_codec == "prores"
    assert assets[0].probe.has_alpha is True


def test_visual_border_exact_mode_rejects_wrong_dimensions(tmp_path):
    border = tmp_path / "small.mov"
    generate_qtrle_border(border, alpha=True, width=90, height=160)

    assets, errors, _warnings = scan_visual_border(
        visual_border_config(tmp_path, border)
    )

    assert not assets[0].valid
    assert "90x160" in errors[0]
    assert "180x320" in errors[0]


def test_missing_required_directory_is_error(tmp_path):
    config = AppConfig.model_validate(
        {
            "id": "missing-test",
            "name": "缺失测试",
            "source_root": str(tmp_path),
            "timeline": ["hook", "benefit_1", "ending"],
            "sources": {
                category: {"mode": "required", "directory": category}
                for category in ["hook", "benefit_1", "ending"]
            },
            "benefit_overlays": {"mode": "disabled", "directory": "overlay"},
            "output": {"directory": str(tmp_path / "out")},
        }
    )
    result = scan_config(config)
    assert not result.ok
    assert len(result.errors) == 3
    assert "目录不存在" in result.errors[0]


def test_fixed_overlay_accepts_one_image_file(tmp_path):
    overlay = tmp_path / "固定风险提示语.png"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=yellow:s=80x80",
            "-frames:v", "1", str(overlay),
        ],
        check=True,
    )
    config = AppConfig.model_validate(
        {
            "id": "fixed-overlay",
            "name": "固定图片",
            "source_root": str(tmp_path),
            "timeline": ["hook", "benefit_1", "ending"],
            "sources": {
                category: {"mode": "optional", "directory": category}
                for category in ["hook", "benefit_1", "ending"]
            },
            "benefit_overlays": {"mode": "required", "file": str(overlay)},
            "output": {"directory": str(tmp_path / "out")},
        }
    )
    result = scan_config(config)
    assert not any("benefit_overlay" in error for error in result.errors)
    assert len(result.assets["benefit_overlay"]) == 1
    assert result.assets["benefit_overlay"][0].name == "固定风险提示语.png"


def _write_managed_marker(root):
    marker = root / ".smartstitch/library.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(
        json.dumps({"paths": {"overlays": "风险提示语图片"}}, ensure_ascii=False),
        encoding="utf-8",
    )


def _managed_overlay_config(root):
    return AppConfig.model_validate(
        {
            "id": "managed-overlay",
            "name": "自动风险提示语",
            "source_root": str(root),
            "timeline": ["hook", "benefit_1", "ending"],
            "sources": {
                category: {"mode": "optional", "directory": category}
                for category in ["hook", "benefit_1", "ending"]
            },
            "benefit_overlays": {"mode": "optional", "file": ""},
            "output": {"directory": str(root / "out")},
        }
    )


def test_managed_library_auto_discovers_single_overlay(tmp_path):
    _write_managed_marker(tmp_path)
    overlay_directory = tmp_path / "风险提示语图片"
    overlay_directory.mkdir()
    overlay = overlay_directory / "自动识别.png"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=yellow:s=80x80",
            "-frames:v", "1", str(overlay),
        ],
        check=True,
    )
    # macOS 资源分叉文件不会被当作第二张图。
    (overlay_directory / "._自动识别.png").write_bytes(b"metadata")

    result = scan_config(_managed_overlay_config(tmp_path))

    assert not result.errors
    assert [asset.name for asset in result.assets["benefit_overlay"]] == ["自动识别.png"]
    assert result.assets["benefit_overlay"][0].valid


def test_managed_library_rejects_multiple_auto_overlays(tmp_path):
    _write_managed_marker(tmp_path)
    overlay_directory = tmp_path / "风险提示语图片"
    overlay_directory.mkdir()
    (overlay_directory / "一.png").write_bytes(b"first")
    (overlay_directory / "二.jpg").write_bytes(b"second")

    result = scan_config(_managed_overlay_config(tmp_path))

    assert result.assets["benefit_overlay"] == []
    assert len(result.errors) == 1
    assert "只能放置一张图片" in result.errors[0]


def test_generic_scan_parses_naming_metadata_and_reports_bad_filename(tmp_path, monkeypatch):
    pool = tmp_path / "pool_1"
    pool.mkdir()
    valid = pool / "00016_张三-红果拿下了我全家-2026-10-31.mp4"
    invalid = pool / "无法解析.mp4"
    valid.write_bytes(b"video")
    invalid.write_bytes(b"video")
    monkeypatch.setattr(
        scanner_module,
        "probe_media",
        lambda *_args, **_kwargs: MediaProbe(duration=1, width=720, height=1280),
    )
    config = AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "scan-naming",
            "name": "扫描命名",
            "source_root": str(tmp_path),
            "timeline": ["pool_1"],
            "sources": {"pool_1": {"label": "主素材", "directory": "pool_1"}},
            "benefit_overlays": {"mode": "disabled", "file": ""},
            "output": {
                "directory": str(tmp_path / "out"),
                "naming": {
                    "enabled": True,
                    "product": "燕麦奶",
                    "benefit": "第二件半价",
                },
            },
        }
    )

    result = scan_config(config)

    parsed = next(asset for asset in result.assets["pool_1"] if asset.name == valid.name)
    assert parsed.naming_metadata is not None
    assert parsed.naming_metadata.talent == "张三"
    assert parsed.naming_metadata.restriction_date == "2026-10-31"
    assert any(invalid.name in error and "识别达人名和限制日期" in error for error in result.errors)


def _probe_test_config(tmp_path, *, concurrency=8, cache_enabled=True):
    source = tmp_path / "source"
    source.mkdir(exist_ok=True)
    return AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "probe-performance",
            "name": "探测性能测试",
            "source_root": str(tmp_path),
            "timeline": ["pool_1"],
            "sources": {
                "pool_1": {
                    "label": "主素材",
                    "mode": "required",
                    "directory": "source",
                    "extensions": [".mp4"],
                }
            },
            "benefit_overlays": {"mode": "disabled", "file": ""},
            "output": {"directory": str(tmp_path / "out")},
            "scanner": {
                "probe_cache_enabled": cache_enabled,
                "probe_concurrency": concurrency,
            },
        }
    )


def test_scan_reuses_persistent_probe_cache_and_invalidates_changed_file(
    tmp_path, monkeypatch
):
    config = _probe_test_config(tmp_path)
    first = tmp_path / "source" / "first.mp4"
    second = tmp_path / "source" / "second.mp4"
    first.write_bytes(b"first")
    second.write_bytes(b"second")
    calls = []

    def fake_probe(path, *_args, **_kwargs):
        calls.append(path)
        return MediaProbe(duration=1, width=720, height=1280)

    monkeypatch.setattr(scanner_module, "probe_media", fake_probe)
    cache = MediaProbeCache(SQLiteStore(tmp_path / "data" / "smartstitch.db"))

    initial = scan_config(config, probe_cache=cache)
    warm = scan_config(config, probe_cache=cache)

    assert initial.ok and warm.ok
    assert len(calls) == 2

    first.write_bytes(b"first-updated")
    changed = scan_config(config, probe_cache=cache)

    assert changed.ok
    assert Counter(calls) == Counter({first.resolve(): 2, second.resolve(): 1})


def test_scan_omits_deleted_nas_file_kept_in_weight_items(tmp_path, monkeypatch):
    config = _probe_test_config(tmp_path)
    deleted = tmp_path / "source" / "deleted.mp4"
    remaining = tmp_path / "source" / "remaining.mp4"
    deleted.write_bytes(b"deleted")
    remaining.write_bytes(b"remaining")
    config.sources["pool_1"].items = [
        AssetItemConfig(
            path=str(deleted), enabled=True, weight=2, tags=["旧素材"]
        ),
        AssetItemConfig(
            path=str(remaining), enabled=True, weight=3, tags=["保留"]
        ),
    ]
    monkeypatch.setattr(
        scanner_module,
        "probe_media",
        lambda *_args, **_kwargs: MediaProbe(duration=1, width=720, height=1280),
    )

    before = scan_config(config)
    deleted.unlink()
    after = scan_config(config)

    assert [asset.name for asset in before.assets["pool_1"]] == [
        "deleted.mp4",
        "remaining.mp4",
    ]
    assert [asset.name for asset in after.assets["pool_1"]] == ["remaining.mp4"]
    assert after.assets["pool_1"][0].weight == 3
    assert not any("文件不可用" in warning for warning in after.warnings)


def test_scan_keeps_existing_but_unreadable_file_as_abnormal(tmp_path, monkeypatch):
    config = _probe_test_config(tmp_path)
    broken = tmp_path / "source" / "broken.mp4"
    broken.write_bytes(b"broken")

    def fail_probe(*_args, **_kwargs):
        raise ValueError("媒体损坏")

    monkeypatch.setattr(scanner_module, "probe_media", fail_probe)

    result = scan_config(config)

    assert [asset.name for asset in result.assets["pool_1"]] == ["broken.mp4"]
    assert result.assets["pool_1"][0].valid is False
    assert result.assets["pool_1"][0].error == "媒体损坏"


def test_scan_limits_concurrent_ffprobe_processes(tmp_path, monkeypatch):
    config = _probe_test_config(tmp_path, concurrency=3, cache_enabled=False)
    for index in range(8):
        (tmp_path / "source" / f"{index}.mp4").write_bytes(b"video")

    lock = threading.Lock()
    active = 0
    maximum_active = 0

    def fake_probe(*_args, **_kwargs):
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.02)
        with lock:
            active -= 1
        return MediaProbe(duration=1, width=720, height=1280)

    monkeypatch.setattr(scanner_module, "probe_media", fake_probe)

    result = scan_config(config)

    assert result.ok
    assert 2 <= maximum_active <= 3


def test_scan_temporarily_caches_probe_failures(tmp_path, monkeypatch):
    config = _probe_test_config(tmp_path)
    (tmp_path / "source" / "broken.mp4").write_bytes(b"broken")
    calls = 0

    def fake_probe(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        raise ValueError("媒体损坏")

    monkeypatch.setattr(scanner_module, "probe_media", fake_probe)
    cache = MediaProbeCache(SQLiteStore(tmp_path / "data" / "smartstitch.db"))

    first = scan_config(config, probe_cache=cache)
    second = scan_config(config, probe_cache=cache)

    assert not first.ok and not second.ok
    assert calls == 1
    assert first.assets["pool_1"][0].error == "媒体损坏"
    assert second.assets["pool_1"][0].error == "媒体损坏"


def test_probe_media_reports_timeout(monkeypatch, tmp_path):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired(cmd="ffprobe", timeout=3)

    monkeypatch.setattr(scanner_module.subprocess, "run", timeout)

    with pytest.raises(ValueError, match=r"ffprobe 超时（3 秒）"):
        scanner_module.probe_media(
            tmp_path / "slow.mp4",
            timeout_seconds=3,
        )
