from __future__ import annotations

import heapq
import math
import random
import secrets
from collections import Counter
from datetime import datetime
from itertools import product
from pathlib import Path

from .models import AppConfig, Asset, BatchPlan, PlanItem, ScanResult, SourceMode


class PlanError(ValueError):
    pass


CORE_CATEGORIES = ("hook", "benefit_video", "ending")


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


def _core_signature(selections: dict[str, Asset | None]) -> tuple[str | None, ...]:
    return tuple(
        selections[category].id if selections.get(category) is not None else None
        for category in CORE_CATEGORIES
    )


def _sequence_score(sequences: dict[str, list[Asset | None]], count: int) -> tuple[int, int]:
    signatures = [
        tuple(
            sequences[category][index].id if sequences[category][index] else None
            for category in CORE_CATEGORIES
        )
        for index in range(count)
    ]
    duplicates = count - len(set(signatures))
    adjacent_reuse = sum(
        sequences[category][index] is not None
        and sequences[category][index - 1] is not None
        and sequences[category][index].id == sequences[category][index - 1].id
        for category in CORE_CATEGORIES
        for index in range(1, count)
    )
    return duplicates, adjacent_reuse


def _arrange_core_best_effort(
    sequences: dict[str, list[Asset | None]], count: int, rng: random.Random
) -> None:
    """Keep each category's selected quota while searching for fewer repeated core combinations."""
    best = {category: list(sequences[category]) for category in CORE_CATEGORIES}
    best_score = _sequence_score(best, count)
    for _ in range(128):
        candidate = {category: list(sequences[category]) for category in CORE_CATEGORIES}
        for values in candidate.values():
            rng.shuffle(values)
        score = _sequence_score(candidate, count)
        if score < best_score:
            best, best_score = candidate, score
    for category in CORE_CATEGORIES:
        sequences[category] = best[category]


def _strict_unique_core_sequences(
    candidates: dict[str, list[Asset]], count: int, rng: random.Random
) -> dict[str, list[Asset]]:
    combination_count = math.prod(len(candidates[category]) for category in CORE_CATEGORIES)
    if count > combination_count:
        raise PlanError(
            f"严格去重无法满足：本次需要 {count} 条，但只有 {combination_count} 种可用核心组合"
        )

    # Weighted sampling without replacement. A material's weight remains influential,
    # while every selected hook + benefit_video + ending signature stays unique.
    selected_heap: list[tuple[float, int, tuple[Asset, ...]]] = []
    combinations = product(*(candidates[category] for category in CORE_CATEGORIES))
    for order, combination in enumerate(combinations):
        combination_weight = math.prod(asset.weight for asset in combination)
        key = math.log(max(rng.random(), 1e-300)) / combination_weight
        entry = (key, order, combination)
        if len(selected_heap) < count:
            heapq.heappush(selected_heap, entry)
        elif key > selected_heap[0][0]:
            heapq.heapreplace(selected_heap, entry)

    selected = [entry[2] for entry in selected_heap]
    best_order = list(selected)
    best_adjacent = math.inf
    for _ in range(128):
        candidate_order = list(selected)
        rng.shuffle(candidate_order)
        adjacent = sum(
            candidate_order[index][category_index].id == candidate_order[index - 1][category_index].id
            for index in range(1, count)
            for category_index in range(len(CORE_CATEGORIES))
        )
        if adjacent < best_adjacent:
            best_order, best_adjacent = candidate_order, adjacent

    return {
        category: [combination[index] for combination in best_order]
        for index, category in enumerate(CORE_CATEGORIES)
    }


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

    core_candidates = {category: _selectable(scan, category) for category in CORE_CATEGORIES}
    if config.randomization.duplicate_policy == "strict":
        sequences.update(_strict_unique_core_sequences(core_candidates, count, rng))
    elif config.randomization.duplicate_policy == "best_effort":
        _arrange_core_best_effort(sequences, count, rng)

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
        signature = _core_signature(selections)
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

    if duplicate_count and config.randomization.duplicate_policy == "best_effort":
        warnings.append(f"为优先保持权重配额，本批仍有 {duplicate_count} 条重复核心组合")

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
