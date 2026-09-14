from __future__ import annotations

import pytest

from smartstitch.models import AppConfig, Asset, MediaProbe, ScanResult
from smartstitch.planner import PlanError, build_plan


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


def core_signatures(plan):
    return {
        tuple(item.selections[category].id for category in ("hook", "benefit_video", "ending"))
        for item in plan.items
    }


def test_best_effort_preserves_quotas_and_rearranges_core_combinations(tmp_path):
    config = make_config(tmp_path)
    config.randomization.duplicate_policy = "best_effort"
    scan = ScanResult(
        config_id=config.id,
        assets={
            category: [asset(category, "a"), asset(category, "b")]
            for category in ("hook", "benefit_video", "ending")
        }
        | {"benefit_overlay": []},
    )

    plan = build_plan(config, scan, 6, seed=31)

    assert len(core_signatures(plan)) == 6
    for category in ("hook", "benefit_video", "ending"):
        assert sorted(plan.distribution[category].values()) == [3, 3]


def test_strict_selects_unique_core_combinations_and_only_fails_when_exhausted(tmp_path):
    config = make_config(tmp_path)
    config.randomization.duplicate_policy = "strict"
    scan = ScanResult(
        config_id=config.id,
        assets={
            "hook": [asset("hook", "a", 5), asset("hook", "b")],
            "benefit_video": [asset("benefit_video", "a"), asset("benefit_video", "b")],
            "ending": [asset("ending", "only")],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 4, seed=17)
    assert len(core_signatures(plan)) == 4

    with pytest.raises(PlanError, match="只有 4 种可用核心组合"):
        build_plan(config, scan, 5, seed=17)
