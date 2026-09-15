from __future__ import annotations

import subprocess

from smartstitch.models import AppConfig
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
