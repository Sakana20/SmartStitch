from __future__ import annotations

import json
import subprocess

from smartstitch.models import AppConfig, MediaProbe
import smartstitch.scanner as scanner_module
from smartstitch.scanner import scan_config


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
