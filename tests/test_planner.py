from __future__ import annotations

from smartstitch.models import AppConfig, Asset, MediaProbe, ScanResult
from smartstitch.planner import build_plan


def make_config(tmp_path):
    return AppConfig.model_validate(
        {
            "id": "test-config",
            "name": "测试配置",
            "source_root": str(tmp_path),
            "timeline": ["hook", "benefit_video", "ending"],
            "sources": {
                category: {"mode": "required", "directory": category}
                for category in ["hook", "benefit_video", "ending"]
            },
            "benefit_overlays": {"mode": "disabled", "directory": "overlays"},
            "output": {"directory": str(tmp_path / "output")},
            "batch": {"max_count": 100},
        }
    )


def asset(category, name, weight=1):
    return Asset(
        id=f"{category}-{name}",
        category=category,
        path=f"/{category}/{name}.mp4",
        name=f"{name}.mp4",
        media_type="video",
        weight=weight,
        probe=MediaProbe(duration=1, width=720, height=1280, fps=30, has_audio=True),
    )


def test_quota_distribution_and_reproducibility(tmp_path):
    config = make_config(tmp_path)
    scan = ScanResult(
        config_id=config.id,
        assets={
            "hook": [asset("hook", "a", 6), asset("hook", "b", 3), asset("hook", "c", 1)],
            "benefit_video": [asset("benefit_video", "only")],
            "ending": [asset("ending", "only")],
            "benefit_overlay": [],
        },
    )
    first = build_plan(config, scan, 10, seed=123)
    second = build_plan(config, scan, 10, seed=123)
    assert first.distribution["hook"] == {"a.mp4": 6, "b.mp4": 3, "c.mp4": 1}
    assert [item.selections["hook"].id for item in first.items] == [
        item.selections["hook"].id for item in second.items
    ]


def test_every_item_contains_core_categories(tmp_path):
    config = make_config(tmp_path)
    scan = ScanResult(
        config_id=config.id,
        assets={
            "hook": [asset("hook", "a")],
            "benefit_video": [asset("benefit_video", "b")],
            "ending": [asset("ending", "c")],
            "benefit_overlay": [],
        },
    )
    plan = build_plan(config, scan, 4, seed=8)
    assert len(plan.items) == 4
    assert all(all(item.selections[category] for category in config.timeline) for item in plan.items)

