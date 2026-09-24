from __future__ import annotations

import heapq
import math
import random
import secrets
from collections import Counter
from datetime import date, datetime
from itertools import product
from pathlib import Path

from .naming import (
    NamingError,
    append_duplicate_suffix,
    derive_plan_naming,
    render_plan_filename,
)
from .models import (
    AppConfig,
    Asset,
    BatchPlan,
    PlannedVisualEffect,
    PlanItem,
    ScanResult,
    SourceMode,
)


class PlanError(ValueError):
    pass


EXACT_STRICT_COMBINATION_LIMIT = 250_000


def _core_categories(config: AppConfig) -> tuple[str, ...]:
    if config.workflow_type == "generic":
        return tuple(
            category
            for category in config.timeline
            if config.sources[category].mode != SourceMode.DISABLED
        )
    return ("hook", *config.benefit_categories(active_only=True), "ending")


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


def _category_name(config: AppConfig, category: str) -> str:
    label = config.sources[category].label.strip()
    return f"{label}（{category}）" if label else category


def _core_signature(
    selections: dict[str, Asset | None], core_categories: tuple[str, ...]
) -> tuple[str | None, ...]:
    return tuple(
        selections[category].id if selections.get(category) is not None else None
        for category in core_categories
    )


def _sequence_score(
    sequences: dict[str, list[Asset | None]],
    core_categories: tuple[str, ...],
    count: int,
) -> tuple[int, int]:
    signatures = [
        tuple(
            sequences[category][index].id if sequences[category][index] else None
            for category in core_categories
        )
        for index in range(count)
    ]
    duplicates = count - len(set(signatures))
    adjacent_reuse = sum(
        sequences[category][index] is not None
        and sequences[category][index - 1] is not None
        and sequences[category][index].id == sequences[category][index - 1].id
        for category in core_categories
        for index in range(1, count)
    )
    return duplicates, adjacent_reuse


def _arrange_core_best_effort(
    sequences: dict[str, list[Asset | None]],
    core_categories: tuple[str, ...],
    count: int,
    rng: random.Random,
) -> None:
    """Keep each category's selected quota while searching for fewer repeated core combinations."""
    best = {category: list(sequences[category]) for category in core_categories}
    best_score = _sequence_score(best, core_categories, count)
    for _ in range(128):
        candidate = {category: list(sequences[category]) for category in core_categories}
        for values in candidate.values():
            rng.shuffle(values)
        score = _sequence_score(candidate, core_categories, count)
        if score < best_score:
            best, best_score = candidate, score
    for category in core_categories:
        sequences[category] = best[category]


def _combination_at_index(
    candidates: dict[str, list[Asset | None]],
    core_categories: tuple[str, ...],
    index: int,
) -> tuple[Asset | None, ...]:
    selected: list[Asset | None] = []
    for category in reversed(core_categories):
        choices = candidates[category]
        index, choice_index = divmod(index, len(choices))
        selected.append(choices[choice_index])
    return tuple(reversed(selected))


def _large_unique_core_sample(
    candidates: dict[str, list[Asset | None]],
    core_categories: tuple[str, ...],
    combination_count: int,
    count: int,
    rng: random.Random,
) -> list[tuple[Asset | None, ...]]:
    """Favor configured weights without enumerating a potentially enormous product."""
    selected: dict[tuple[str | None, ...], tuple[Asset | None, ...]] = {}
    attempts = 0
    max_attempts = max(1_000, count * 200)
    while len(selected) < count and attempts < max_attempts:
        combination = tuple(
            rng.choices(
                candidates[category],
                weights=[
                    asset.weight if asset is not None else 1
                    for asset in candidates[category]
                ],
                k=1,
            )[0]
            for category in core_categories
        )
        signature = tuple(asset.id if asset is not None else None for asset in combination)
        selected.setdefault(signature, combination)
        attempts += 1

    # Extreme weights can make rejection sampling stall. Strict uniqueness takes
    # precedence, so fill the small remainder from uniformly sampled product indexes.
    if len(selected) < count:
        index = rng.randrange(combination_count)
        step = rng.randrange(1, combination_count)
        while math.gcd(step, combination_count) != 1:
            step = rng.randrange(1, combination_count)
        while len(selected) < count:
            combination = _combination_at_index(candidates, core_categories, index)
            signature = tuple(asset.id if asset is not None else None for asset in combination)
            selected.setdefault(signature, combination)
            index = (index + step) % combination_count
    return list(selected.values())


def _strict_unique_core_sequences(
    candidates: dict[str, list[Asset | None]],
    core_categories: tuple[str, ...],
    count: int,
    rng: random.Random,
) -> dict[str, list[Asset | None]]:
    combination_count = math.prod(len(candidates[category]) for category in core_categories)
    if count > combination_count:
        raise PlanError(
            f"严格去重无法满足：本次需要 {count} 条，"
            f"但只有 {combination_count} 种可用核心组合"
        )

    if combination_count <= EXACT_STRICT_COMBINATION_LIMIT:
        # Weighted sampling without replacement. Every complete dynamic core
        # signature remains unique while each source weight stays influential.
        selected_heap: list[tuple[float, int, tuple[Asset | None, ...]]] = []
        combinations = product(*(candidates[category] for category in core_categories))
        for order, combination in enumerate(combinations):
            combination_weight = math.prod(
                asset.weight if asset is not None else 1 for asset in combination
            )
            key = math.log(max(rng.random(), 1e-300)) / combination_weight
            entry = (key, order, combination)
            if len(selected_heap) < count:
                heapq.heappush(selected_heap, entry)
            elif key > selected_heap[0][0]:
                heapq.heapreplace(selected_heap, entry)
        selected = [entry[2] for entry in selected_heap]
    else:
        selected = _large_unique_core_sample(
            candidates, core_categories, combination_count, count, rng
        )
    best_order = list(selected)
    best_adjacent = math.inf
    for _ in range(128):
        candidate_order = list(selected)
        rng.shuffle(candidate_order)
        adjacent = sum(
            (
                candidate_order[index][category_index].id
                if candidate_order[index][category_index] is not None
                else None
            )
            == (
                candidate_order[index - 1][category_index].id
                if candidate_order[index - 1][category_index] is not None
                else None
            )
            for index in range(1, count)
            for category_index in range(len(core_categories))
        )
        if adjacent < best_adjacent:
            best_order, best_adjacent = candidate_order, adjacent

    return {
        category: [combination[index] for combination in best_order]
        for index, category in enumerate(core_categories)
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


def _reserve_business_name(
    config: AppConfig, base_name: str, reserved_names: set[str]
) -> str:
    candidate = base_name
    serial = 1
    while candidate.casefold() in reserved_names:
        serial += 1
        candidate = append_duplicate_suffix(config, base_name, serial)
    reserved_names.add(candidate.casefold())
    return candidate


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
                raise PlanError(f"{_category_name(config, category)} 没有可用素材")
            sequences[category] = [None] * count
            continue
        sequences[category] = list(_sequence(candidates, count, config.randomization.mode, rng))

    core_categories = _core_categories(config)
    if config.workflow_type == "generic" and not core_categories:
        raise PlanError("请先创建并启用至少一个视频库")
    core_candidates: dict[str, list[Asset | None]] = {}
    for category in core_categories:
        candidates = _selectable(scan, category)
        if candidates:
            core_candidates[category] = list(candidates)
        elif config.sources[category].mode == SourceMode.REQUIRED:
            raise PlanError(f"{_category_name(config, category)} 没有可用素材")
        else:
            core_candidates[category] = [None]
    if config.randomization.duplicate_policy == "strict":
        sequences.update(
            _strict_unique_core_sequences(core_candidates, core_categories, count, rng)
        )
    elif config.randomization.duplicate_policy == "best_effort":
        _arrange_core_best_effort(sequences, core_categories, count, rng)

    overlay_assets = _selectable(scan, "benefit_overlay")
    overlays: list[Asset | None] = [None] * count
    overlay_required = config.benefit_overlays.mode == SourceMode.REQUIRED
    if overlay_assets:
        if len(overlay_assets) != 1:
            raise PlanError("每个配置必须且只能有一张风险提示语图片")
        overlays = [overlay_assets[0]] * count
    elif overlay_required:
        raise PlanError("风险提示语图片为必需，但没有可用图片")

    effect_layers = (
        [layer for layer in config.visual_dedup.resolved_effect_layers() if layer.enabled]
        if config.visual_dedup.enabled
        else []
    )
    effect_sequences: dict[str, list[Asset | None]] = {}
    for layer in effect_layers:
        if layer.type == "blur_frame":
            effect_sequences[layer.layer_id] = [None] * count
            continue
        category = f"visual_effect:{layer.layer_id}"
        candidates = _selectable(scan, category)
        if not candidates and layer.layer_id == "legacy-visual-border":
            candidates = _selectable(scan, "visual_border")
        if candidates:
            if layer.selection_mode == "fixed":
                if len(candidates) != 1:
                    raise PlanError(f"{layer.name}: 固定模式必须且只能选择一个素材")
                effect_sequences[layer.layer_id] = [candidates[0]] * count
            else:
                effect_sequences[layer.layer_id] = _sequence(
                    candidates, count, config.randomization.mode, rng
                )
        elif layer.required:
            raise PlanError(f"{layer.name}: 没有可用特效素材")
        else:
            effect_sequences[layer.layer_id] = [None] * count

    warnings = list(scan.warnings)
    signatures: set[tuple[str | None, ...]] = set()
    duplicate_count = 0
    items: list[PlanItem] = []
    reserved_output_names: set[str] = set()
    naming_date = date.today()
    for index in range(count):
        selections = {category: sequence[index] for category, sequence in sequences.items()}
        signature = _core_signature(selections, core_categories)
        if signature in signatures:
            duplicate_count += 1
        signatures.add(signature)
        duration = sum(
            asset.probe.duration
            for asset in selections.values()
            if asset is not None and asset.probe is not None
        )
        naming = None
        if config.workflow_type == "generic" and config.output.naming.enabled:
            try:
                naming = derive_plan_naming(
                    config,
                    selections,
                    sequence=config.output.naming.sequence_start + index,
                    naming_date=naming_date,
                )
                output_name = _reserve_business_name(
                    config, render_plan_filename(config, naming), reserved_output_names
                )
            except NamingError as exc:
                raise PlanError(f"成片 {index + 1} 命名失败: {exc}") from exc
        else:
            output_name = _format_name(config, actual_seed, index + 1, batch_id)
        visual_effects = [
            PlannedVisualEffect(
                layer=layer,
                asset=effect_sequences[layer.layer_id][index],
            )
            for layer in effect_layers
            if layer.type == "blur_frame"
            or effect_sequences[layer.layer_id][index] is not None
        ]
        legacy_visual_border = next(
            (
                effect.asset
                for effect in visual_effects
                if effect.layer.type == "overlay"
                and effect.layer.library_id in {"effect_1", "visual-border"}
            ),
            None,
        )
        items.append(
            PlanItem(
                index=index + 1,
                selections=selections,
                overlay=overlays[index],
                visual_border=legacy_visual_border,
                visual_effects=visual_effects,
                output_name=output_name,
                estimated_duration=round(duration, 3),
                naming=naming,
            )
        )

    if duplicate_count and config.randomization.duplicate_policy == "best_effort":
        warnings.append(f"为优先保持权重配额，本批仍有 {duplicate_count} 条重复核心组合")

    distribution: dict[str, dict[str, int]] = {}
    for category, sequence in sequences.items():
        distribution[category] = dict(Counter(asset.name for asset in sequence if asset))
    distribution["benefit_overlay"] = dict(Counter(asset.name for asset in overlays if asset))
    for layer in effect_layers:
        if layer.type != "overlay":
            continue
        sequence = effect_sequences[layer.layer_id]
        distribution[f"visual_effect:{layer.layer_id}"] = dict(
            Counter(asset.name for asset in sequence if asset)
        )
        if layer.library_id in {"effect_1", "visual-border"}:
            distribution["visual_border"] = dict(
                Counter(asset.name for asset in sequence if asset)
            )
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
