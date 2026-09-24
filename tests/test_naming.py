from __future__ import annotations

from datetime import date

import pytest
from pydantic import ValidationError

from smartstitch.models import AppConfig, Asset, MediaProbe, ScanResult, SourceMode
from smartstitch.naming import (
    NamingError, derive_plan_naming, filename_signature, filename_tokens, parse_asset_naming,
    render_plan_filename,
    source_block_value,
)
from smartstitch.planner import build_plan


def naming_config(tmp_path, *, categories=None) -> AppConfig:
    return AppConfig.model_validate(
        {
            "schema_version": 3,
            "workflow_type": "generic",
            "id": "naming-test",
            "name": "命名测试",
            "source_root": str(tmp_path),
            "timeline": ["pool_2", "pool_1"],
            "sources": {
                "pool_1": {"label": "视频库 1", "directory": "pool_1"},
                "pool_2": {"label": "视频库 2", "directory": "pool_2"},
            },
            "benefit_overlays": {
                "mode": "disabled",
                "file": "",
                "timing": {"scope": "full"},
            },
            "output": {
                "directory": str(tmp_path / "output"),
                "naming": {
                    "enabled": True,
                    "product": "燕麦奶",
                    "benefit": "第二件半价",
                    "source_metadata": {"categories": categories or ["pool_*"]},
                },
            },
        }
    )


def naming_asset(category: str, filename: str) -> Asset:
    return Asset(
        id=f"{category}-{filename}",
        category=category,
        path=f"/{category}/{filename}",
        name=filename,
        media_type="video",
        probe=MediaProbe(duration=1, width=720, height=1280, fps=30),
    )


def test_generic_dynamic_naming_uses_timeline_order_earliest_date_and_sequence(tmp_path):
    config = naming_config(tmp_path)
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [
                naming_asset(
                    "pool_1",
                    "00016_张三-红果拿下了我全家-2026-10-31__001_pool-1_f0-10.mp4",
                )
            ],
            "pool_2": [
                naming_asset(
                    "pool_2",
                    "00018_李四-红果拿下了我全家-2026-10-20__002_pool-2_f10-20.mp4",
                )
            ],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 2, seed=7)

    assert plan.items[0].output_name == "燕麦奶-第二件半价-李四+张三-20261020-1.mp4"
    assert plan.items[1].output_name == "燕麦奶-第二件半价-李四+张三-20261020-2.mp4"
    assert plan.items[0].naming is not None
    assert plan.items[0].naming.talents == ["李四", "张三"]
    assert plan.items[0].naming.restriction_date == "2026-10-20"
    assert plan.items[0].naming.sequence == 1
    assert plan.items[1].naming.sequence == 2
    assert [source.category for source in plan.items[0].naming.sources] == [
        "pool_2",
        "pool_1",
    ]


def test_builder_uses_selected_asset_segments_and_editable_text(tmp_path):
    data = naming_config(tmp_path).model_dump(mode="json")
    names = ["瑞幸-咖啡-ai1.mp4", "通用-热菜-烤鸭.mp4"]
    signature = filename_signature(filename_tokens(names[0]))
    blocks = [
        {"id": "brand", "type": "source", "category": "pool_1", "role": "product",
         "variants": [{"sample_name": names[0], "signature": signature, "token_index": 0}]},
        {"id": "dash1", "type": "text", "text": "-"},
        {"id": "category", "type": "source", "category": "pool_1",
         "variants": [{"sample_name": names[0], "signature": signature, "token_index": 2}]},
        {"id": "dash2", "type": "text", "text": "-"},
        {"id": "offer", "type": "text", "text": "最高25元红包", "role": "benefit"},
        {"id": "dash3", "type": "text", "text": "-"},
        {"id": "number", "type": "sequence"},
    ]
    data["output"]["naming"]["builder"] = {"enabled": True, "blocks": blocks}
    config = AppConfig.model_validate(data)
    scan = ScanResult(config_id=config.id, assets={
        "pool_1": [naming_asset("pool_1", name) for name in names],
        "pool_2": [naming_asset("pool_2", "1.mp4")],
        "benefit_overlay": [],
    })
    plan = build_plan(config, scan, 2, seed=2)
    assert {item.output_name for item in plan.items} == {
        "瑞幸-咖啡-最高25元红包-1.mp4",
        "通用-热菜-最高25元红包-2.mp4",
    } or {item.output_name for item in plan.items} == {
        "瑞幸-咖啡-最高25元红包-2.mp4",
        "通用-热菜-最高25元红包-1.mp4",
    }
    for item in plan.items:
        assert item.naming.product == item.output_name.split("-")[0]
        assert item.naming.benefit == "最高25元红包"
        assert item.naming.block_values[0]["asset"] == item.naming.block_values[2]["asset"]
    config.output.naming.builder.blocks[4].text = "买一送一"
    changed = build_plan(config, scan, 1, seed=2).items[0]
    assert "买一送一" in changed.output_name
    assert changed.naming.benefit == "买一送一"


def test_builder_selected_fields_accept_extra_filename_segments(tmp_path):
    from smartstitch.models import NamingBlockConfig

    sample = "沪上阿姨-奶茶-店员.mp4"
    actual = naming_asset("pool_1", "霸王茶姬-奶茶-店员-优惠价.mp4")
    signature = filename_signature(filename_tokens(sample))
    for token_index, expected in ((0, "霸王茶姬"), (2, "奶茶")):
        block = NamingBlockConfig.model_validate({
            "id": f"field-{token_index}", "type": "source", "category": "pool_1",
            "variants": [{"sample_name": sample, "signature": signature, "token_index": token_index}],
        })
        assert source_block_value(block, actual) == expected
        with pytest.raises(NamingError):
            source_block_value(block, naming_asset("pool_1", "霸王茶姬_奶茶.mp4"))


def test_builder_date_fragment_and_missing_format(tmp_path):
    config = naming_config(tmp_path)
    sample = "00016_磊金夫妇-红果拿下了我全家-2027-05-17__001_pool-1_f0-10.mp4"
    tokens = filename_tokens(sample)
    assert tokens[2] == "磊金夫妇"
    assert tokens[6] == "2027-05-17"
    from smartstitch.models import NamingBlockConfig
    block = NamingBlockConfig.model_validate({
        "id": "date", "type": "source", "category": "pool_1",
        "variants": [{"sample_name": sample, "signature": filename_signature(tokens), "token_index": 6}],
    })
    assert source_block_value(block, naming_asset("pool_1", sample)) == "2027-05-17"
    with pytest.raises(NamingError, match="片段"):
        source_block_value(block, naming_asset("pool_1", "1.mp4"))

    data = config.model_dump(mode="json")
    variant = {"sample_name": sample, "signature": filename_signature(tokens)}
    data["output"]["naming"]["builder"] = {"enabled": True, "blocks": [
        {"id": "product", "type": "text", "text": "红果短剧", "role": "product"},
        {"id": "sep1", "type": "text", "text": "-"},
        {"id": "benefit", "type": "text", "text": "功能综述", "role": "benefit"},
        {"id": "sep2", "type": "text", "text": "-"},
        {"id": "talent", "type": "source", "category": "pool_1", "role": "talent",
         "variants": [{**variant, "token_index": 2}]},
        {"id": "sep3", "type": "text", "text": "-"},
        {"id": "date", "type": "source", "category": "pool_1", "role": "restriction_date",
         "date_format": "compact", "variants": [{**variant, "token_index": 6}]},
        {"id": "sep4", "type": "text", "text": "-"},
        {"id": "sequence", "type": "sequence"},
    ]}
    configured = AppConfig.model_validate(data)
    plan = build_plan(configured, ScanResult(config_id=configured.id, assets={
        "pool_1": [naming_asset("pool_1", sample)],
        "pool_2": [naming_asset("pool_2", "1.mp4")], "benefit_overlay": [],
    }), 1, seed=1)
    assert plan.items[0].output_name == "红果短剧-功能综述-磊金夫妇-20270517-1.mp4"
    assert plan.items[0].naming.restriction_date == "2027-05-17"


def test_builder_today_date_block_uses_mmdd(tmp_path):
    data = naming_config(tmp_path).model_dump(mode="json")
    data["output"]["naming"]["builder"] = {"enabled": True, "blocks": [
        {"id": "name", "type": "text", "text": "闪购"},
        {"id": "dash", "type": "text", "text": "-"},
        {"id": "today", "type": "date", "date_format": "mmdd"},
        {"id": "dash2", "type": "text", "text": "-"},
        {"id": "sequence", "type": "sequence"},
    ]}
    config = AppConfig.model_validate(data)
    naming = derive_plan_naming(config, {}, 3, naming_date=date(2026, 9, 24))
    assert render_plan_filename(config, naming) == "闪购-0924-3.mp4"
    assert naming.restriction_date is None
    assert naming.block_values[2]["value"] == "0924"


def test_explicit_naming_pools_exclude_other_stitched_pools(tmp_path):
    config = naming_config(tmp_path, categories=["pool_1"])
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [naming_asset("pool_1", "00016_张三-中间文案-2026-10-31.mp4")],
            "pool_2": [naming_asset("pool_2", "不需解析.mp4")],
            "benefit_overlay": [],
        },
    )

    item = build_plan(config, scan, 1, seed=3).items[0]

    assert item.output_name == "燕麦奶-第二件半价-张三-20261031-1.mp4"
    assert item.naming is not None
    assert [source.category for source in item.naming.sources] == ["pool_1"]


def test_disabled_naming_pool_omits_talent_and_date(tmp_path):
    config = naming_config(tmp_path, categories=["pool_1"])
    config.sources["pool_1"].mode = SourceMode.DISABLED
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [],
            "pool_2": [naming_asset("pool_2", "普通素材.mp4")],
            "benefit_overlay": [],
        },
    )

    item = build_plan(config, scan, 1, seed=3).items[0]

    assert item.output_name == "燕麦奶-第二件半价-1.mp4"
    assert item.naming.talents == []
    assert item.naming.restriction_date is None
    assert item.naming.sources == []


def test_sequence_can_start_from_configured_number(tmp_path):
    config = naming_config(tmp_path)
    config.output.naming.sequence_start = 50
    scan = ScanResult(
        config_id=config.id,
        assets={
            "pool_1": [naming_asset("pool_1", "00016_张三-中间文案-2026-10-31.mp4")],
            "pool_2": [naming_asset("pool_2", "00018_李四-中间文案-2026-10-20.mp4")],
            "benefit_overlay": [],
        },
    )

    plan = build_plan(config, scan, 2, seed=3)

    assert [item.naming.sequence for item in plan.items] == [50, 51]
    assert plan.items[0].output_name.endswith("-50.mp4")
    assert plan.items[1].output_name.endswith("-51.mp4")


def test_naming_rejects_invalid_date_and_non_generic_enablement(tmp_path):
    config = naming_config(tmp_path)
    with pytest.raises(NamingError, match="无效的限制日期"):
        parse_asset_naming(
            config,
            naming_asset("pool_1", "00016_张三-中间文案-2026-02-30.mp4"),
        )

    legacy = config.model_dump(mode="json")
    legacy.update(
        {
            "schema_version": 2,
            "workflow_type": "taobao_flash",
            "timeline": ["hook", "benefit_1", "ending"],
            "sources": {
                category: {"directory": category}
                for category in ["hook", "benefit_1", "ending"]
            },
        }
    )
    with pytest.raises(ValidationError, match="仅支持通用视频项目"):
        AppConfig.model_validate(legacy)


def test_naming_template_requires_all_business_fields(tmp_path):
    data = naming_config(tmp_path).model_dump(mode="json")
    data["output"]["naming"]["template"] = "{product}-{talents}.mp4"

    with pytest.raises(ValidationError, match="缺少必需变量"):
        AppConfig.model_validate(data)


@pytest.mark.parametrize(
    ("filename", "talent", "restriction_date"),
    [
        (
            "00016_小慧不大乖-红果拿下了我全家-2026-11-14__002_pool-8_f3-1327.mp4",
            "小慧不大乖",
            "2026-11-14",
        ),
        (
            "00018_磊金夫妇-红果拿下了我全家-2027-05-17__001_pool-8_f0-1391.mp4",
            "磊金夫妇",
            "2027-05-17",
        ),
    ],
)
def test_pool_8_real_filename_examples(tmp_path, filename, talent, restriction_date):
    metadata = parse_asset_naming(
        naming_config(tmp_path),
        naming_asset("pool_8", filename),
    )

    assert metadata.talent == talent
    assert metadata.restriction_date == restriction_date
