from __future__ import annotations

import math
import random
import secrets
from collections import Counter
from datetime import datetime
from pathlib import Path

from .models import AppConfig, Asset, BatchPlan, PlanItem, ScanResult, SourceMode


class PlanError(ValueError):
    pass


def _quota_sequence(assets: list[Asset], count: int, rng: random.Random) -> list[Asset]:
    total = sum(asset.weight for asset in assets)
    exact = [count * asset.weight / total for asset in assets]
    allocated = [math.floor(value) for value in exact]
    remaining = count - sum(allocated)
    ranked = list(range(len(assets)))
    rng.shuffle(ranked)
    ranked.sort(key=lambda index: exact[index] - allocated[index], reverse=True)
    for index in ranked[:remaining]:
        allocated[index] += 1
    sequence = [asset for asset, amount in zip(assets, allocated, strict=True) for _ in range(amount)]
    rng.shuffle(sequence)
    return sequence


def _independent_sequence(assets: list[Asset], count: int, rng: random.Random) -> list[Asset]:
    return rng.choices(assets, weights=[asset.weight for asset in assets], k=count)


def _sequence(assets: list[Asset], count: int, mode: str, rng: random.Random) -> list[Asset]:
    if mode == "quota_shuffle":
        return _quota_sequence(assets, count, rng)
    return _independent_sequence(assets, count, rng)


def _selectable(scan: ScanResult, category: str) -> list[Asset]:
    return [asset for asset in scan.assets.get(category, []) if asset.selectable]


def _format_name(config: AppConfig, seed: int, index: int, batch_id: str) -> str:
    values = {
        "config": config.name.replace("/", "-"),
        "date": datetime.now().strftime("%Y%m%d"),
        "batch": batch_id,
        "index": index,
        "seed": seed,
    }
    try:
        name = config.output.filename_template.format(**values)
    except (KeyError, ValueError) as exc:
        raise PlanError(f"文件名模板无效: {exc}") from exc
    return name if Path(name).suffix else f"{name}.mp4"


def build_plan(
    config: AppConfig,
    scan: ScanResult,
    count: int,
    seed: int | None = None,
    batch_id: str = "preview",
) -> BatchPlan:
    if scan.errors:
        raise PlanError("素材预检失败: " + "; ".join(scan.errors))
    if count < 1 or count > config.batch.max_count:
        raise PlanError(f"生成数量必须在 1 到 {config.batch.max_count} 之间")
    actual_seed = seed if seed is not None else config.randomization.default_seed
    if actual_seed is None:
        actual_seed = secrets.randbits(63)
    rng = random.Random(actual_seed)

    sequences: dict[str, list[Asset | None]] = {}
    for category in config.timeline:
        group = config.sources[category]
        candidates = _selectable(scan, category)
        if not candidates:
            if group.mode == SourceMode.REQUIRED:
                raise PlanError(f"{category} 没有可用素材")
            sequences[category] = [None] * count
            continue
        sequences[category] = list(_sequence(candidates, count, config.randomization.mode, rng))

    overlay_assets = _selectable(scan, "benefit_overlay")
    overlays: list[Asset | None] = [None] * count
    overlay_required = config.benefit_overlays.mode == SourceMode.REQUIRED
    if overlay_assets:
        if len(overlay_assets) != 1:
            raise PlanError("每个配置必须且只能有一张利益点图片")
        overlays = [overlay_assets[0]] * count
    elif overlay_required:
        raise PlanError("利益点图片为必需，但没有可用图片")

    warnings = list(scan.warnings)
    signatures: set[tuple[str | None, ...]] = set()
    duplicate_count = 0
    items: list[PlanItem] = []
    for index in range(count):
        selections = {category: sequence[index] for category, sequence in sequences.items()}
        signature = tuple(
            [selections[category].id if selections[category] else None for category in config.timeline]
            + [overlays[index].id if overlays[index] else None]
        )
        if signature in signatures:
            duplicate_count += 1
        signatures.add(signature)
        duration = sum(
            asset.probe.duration
            for asset in selections.values()
            if asset is not None and asset.probe is not None
        )
        items.append(
            PlanItem(
                index=index + 1,
                selections=selections,
                overlay=overlays[index],
                output_name=_format_name(config, actual_seed, index + 1, batch_id),
                estimated_duration=round(duration, 3),
            )
        )

    if duplicate_count and config.randomization.duplicate_policy == "strict":
        raise PlanError(f"严格去重无法满足：计划中出现 {duplicate_count} 个重复组合")
    if duplicate_count and config.randomization.duplicate_policy == "best_effort":
        warnings.append(f"组合空间或权重限制导致 {duplicate_count} 条重复组合")

    distribution: dict[str, dict[str, int]] = {}
    for category, sequence in sequences.items():
        distribution[category] = dict(Counter(asset.name for asset in sequence if asset))
    distribution["benefit_overlay"] = dict(Counter(asset.name for asset in overlays if asset))
    return BatchPlan(
        config_id=config.id,
        config_name=config.name,
        count=count,
        seed=actual_seed,
        algorithm=config.randomization.mode,
        items=items,
        distribution=distribution,
        warnings=warnings,
    )
