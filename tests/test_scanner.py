from __future__ import annotations

from smartstitch.models import AppConfig
from smartstitch.scanner import scan_config


def test_missing_required_directory_is_error(tmp_path):
    config = AppConfig.model_validate(
        {
            "id": "missing-test",
            "name": "缺失测试",
            "source_root": str(tmp_path),
            "timeline": ["hook", "benefit_video", "ending"],
            "sources": {
                category: {"mode": "required", "directory": category}
                for category in ["hook", "benefit_video", "ending"]
            },
            "benefit_overlays": {"mode": "disabled", "directory": "overlay"},
            "output": {"directory": str(tmp_path / "out")},
        }
    )
    result = scan_config(config)
    assert not result.ok
    assert len(result.errors) == 3
    assert "目录不存在" in result.errors[0]

