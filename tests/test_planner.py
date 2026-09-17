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
            "timeline": ["hook", "benefit_1", "ending"],
            "sources": {
                category: {"mode": "required", "directory": category}
                for category in ["hook", "benefit_1", "ending"]
            },
            "benefit_overlays": {"mode": "disabled", "directory": "overlays"},
            "output": {"directory": str(tmp_path / "output")},
            "batch": {"max_count": 100},
        }
    )


def make_generic_config(tmp_path, timeline=None):
    timeline = ["pool_2", "pool_1"] if timeline is None else timeline
    return AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "generic-config",
            "name": "通用配置",
            "source_root": str(tmp_path),
            "timeline": timeline,
            "sources": {
                category: {
                    "label": label,
                    "mode": "required",
                    "directory": f"视频库/{category}",
                }
                for category, label in [("pool_1", "开场"), ("pool_2", "展示")]
                if category in timeline
            },
            "benefit_overlays": {
                "mode": "disabled",
                "file": "",
                "timing": {"scope": "full"},
            },
            "output": {"directory": str(tmp_path / "output")},
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
            "benefit_1": [asset("benefit_1", "only")],
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
            "benefit_1": [asset("benefit_1", "b")],
            "ending": [asset("ending", "c")],
            "benefit_overlay": [],
        },
    )
    plan = build_plan(config, scan, 4, seed=8)
    assert len(plan.items) == 4
    assert all(all(item.selections[category] for category in config.timeline) for item in plan.items)


def test_generic_plan_uses_dynamic_timeline_order(tmp_path):
    config = make_generic_config(tmp_path)
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [asset("pool_1", "opening")],
            "pool_2": [asset("pool_2", "showcase")],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 2, seed=5)

    assert list(plan.items[0].selections) == ["pool_2", "pool_1"]
    assert set(plan.distribution) == {"pool_1", "pool_2", "benefit_overlay"}


def test_generic_empty_project_cannot_generate(tmp_path):
    config = AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "empty-generic",
            "name": "空项目",
            "source_root": str(tmp_path),
            "timeline": [],
            "sources": {},
            "benefit_overlays": {
                "mode": "disabled",
                "file": "",
                "timing": {"scope": "full"},
            },
            "output": {"directory": str(tmp_path / "output")},
        }
    )
    scan = ScanResult(config_id=config.id, assets={"benefit_overlay": []})

    with pytest.raises(PlanError, match="至少一个视频库"):
        build_plan(config, scan, 1, seed=1)


def core_signatures(plan):
    return {
        tuple(
            item.selections[category].id if item.selections[category] else None
            for category in ("hook", "benefit_1", "ending")
        )
        for item in plan.items
    }


def test_best_effort_preserves_quotas_and_rearranges_core_combinations(tmp_path):
    config = make_config(tmp_path)
    config.randomization.duplicate_policy = "best_effort"
    scan = ScanResult(
        config_id=config.id,
        assets={
            category: [asset(category, "a"), asset(category, "b")]
            for category in ("hook", "benefit_1", "ending")
        }
        | {"benefit_overlay": []},
    )

    plan = build_plan(config, scan, 6, seed=31)

    assert len(core_signatures(plan)) == 6
    for category in ("hook", "benefit_1", "ending"):
        assert sorted(plan.distribution[category].values()) == [3, 3]


def test_strict_selects_unique_core_combinations_and_only_fails_when_exhausted(tmp_path):
    config = make_config(tmp_path)
    config.randomization.duplicate_policy = "strict"
    scan = ScanResult(
        config_id=config.id,
        assets={
            "hook": [asset("hook", "a", 5), asset("hook", "b")],
            "benefit_1": [asset("benefit_1", "a"), asset("benefit_1", "b")],
            "ending": [asset("ending", "only")],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 4, seed=17)
    assert len(core_signatures(plan)) == 4

    with pytest.raises(PlanError, match="只有 4 种可用核心组合"):
        build_plan(config, scan, 5, seed=17)


def test_multiple_benefit_segments_are_independent_and_part_of_strict_signature(tmp_path):
    config = make_config(tmp_path)
    config.timeline.insert(2, "benefit_2")
    config.sources["benefit_2"] = config.sources["benefit_1"].model_copy(
        update={"directory": "benefit_2"}
    )
    config.randomization.duplicate_policy = "strict"
    scan = ScanResult(
        config_id=config.id,
        assets={
            "hook": [asset("hook", "only")],
            "benefit_1": [asset("benefit_1", "a"), asset("benefit_1", "b")],
            "benefit_2": [asset("benefit_2", "a"), asset("benefit_2", "b")],
            "ending": [asset("ending", "only")],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 4, seed=29)
    signatures = {
        tuple(
            item.selections[category].id
            for category in ("hook", "benefit_1", "benefit_2", "ending")
        )
        for item in plan.items
    }

    assert len(signatures) == 4
    assert all(item.selections["benefit_1"] for item in plan.items)
    assert all(item.selections["benefit_2"] for item in plan.items)
    with pytest.raises(PlanError, match="只有 4 种可用核心组合"):
        build_plan(config, scan, 5, seed=29)


def test_strict_large_combination_space_does_not_enumerate_full_product(tmp_path):
    config = make_config(tmp_path)
    config.randomization.duplicate_policy = "strict"
    scan = ScanResult(
        config_id=config.id,
        assets={
            category: [asset(category, str(index)) for index in range(64)]
            for category in ("hook", "benefit_1", "ending")
        }
        | {"benefit_overlay": []},
    )

    first = build_plan(config, scan, 20, seed=113)
    second = build_plan(config, scan, 20, seed=113)

    assert len(core_signatures(first)) == 20
    assert [
        tuple(item.selections[category].id for category in config.timeline)
        for item in first.items
    ] == [
        tuple(item.selections[category].id for category in config.timeline)
        for item in second.items
    ]


def test_strict_allows_an_empty_optional_benefit_segment(tmp_path):
    config = make_config(tmp_path)
    config.sources["benefit_1"].mode = "optional"
    config.randomization.duplicate_policy = "strict"
    scan = ScanResult(
        config_id=config.id,
        assets={
            "hook": [asset("hook", "a"), asset("hook", "b")],
            "benefit_1": [],
            "ending": [asset("ending", "only")],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 2, seed=7)

    assert all(item.selections["benefit_1"] is None for item in plan.items)
    assert len(core_signatures(plan)) == 2
