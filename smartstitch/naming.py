from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from .models import (
    AppConfig,
    Asset,
    AssetNamingMetadata,
    NamingSourceRecord,
    PlanNamingMetadata,
)


MAX_FILENAME_BYTES = 240
INVALID_FILENAME_CHARACTERS = re.compile(r"[<>:\"/\\|?*\x00-\x1f\x7f]")


class NamingError(ValueError):
    pass


def category_is_naming_source(config: AppConfig, category: str) -> bool:
    patterns = config.output.naming.source_metadata.categories
    return any(pattern == "pool_*" or pattern == category for pattern in patterns)


def _parse_restriction_date(value: str, formats: list[str]) -> date:
    for date_format in formats:
        try:
            if date_format == "%y%m%d":
                if not re.fullmatch(r"\d{6}", value):
                    continue
                if int(value[:2]) > 69:
                    continue
                return date.fromisoformat(f"20{value[:2]}-{value[2:4]}-{value[4:6]}")
            return datetime.strptime(value, date_format).date()
        except ValueError:
            continue
    raise NamingError(f"无效的限制日期: {value}")


def parse_asset_naming(config: AppConfig, asset: Asset) -> AssetNamingMetadata:
    settings = config.output.naming.source_metadata
    source_stem = Path(asset.name).stem
    if settings.strip_smartstitch_suffix:
        source_stem = source_stem.split("__", 1)[0]
    match = re.fullmatch(settings.pattern, source_stem)
    if match is None:
        raise NamingError("文件名不符合解析规则")
    talent = (match.group("talent") or "").strip()
    if not talent:
        raise NamingError("达人名为空")
    restriction_date = _parse_restriction_date(
        match.group("restriction_date"), settings.restriction_date_formats
    )
    return AssetNamingMetadata(
        source_stem=source_stem,
        talent=talent,
        restriction_date=restriction_date.isoformat(),
    )


def derive_plan_naming(
    config: AppConfig, selections: dict[str, Asset | None]
) -> PlanNamingMetadata:
    records: list[NamingSourceRecord] = []
    talents: list[str] = []
    seen_talents: set[str] = set()
    dates: list[date] = []
    for category in config.timeline:
        if not category_is_naming_source(config, category):
            continue
        asset = selections.get(category)
        if asset is None:
            continue
        try:
            metadata = asset.naming_metadata or parse_asset_naming(config, asset)
        except NamingError as exc:
            label = config.sources[category].label.strip() or category
            raise NamingError(f"{label}（{category}）: {asset.name}: {exc}") from exc
        records.append(
            NamingSourceRecord(
                category=category,
                asset=asset.name,
                source_stem=metadata.source_stem,
                talent=metadata.talent,
                restriction_date=metadata.restriction_date,
            )
        )
        if metadata.talent not in seen_talents:
            seen_talents.add(metadata.talent)
            talents.append(metadata.talent)
        dates.append(date.fromisoformat(metadata.restriction_date))
    if not records:
        raise NamingError("本条成片没有可用于命名的视频素材")
    return PlanNamingMetadata(
        product=config.output.naming.product,
        benefit=config.output.naming.benefit,
        talents=talents,
        restriction_date=min(dates).isoformat(),
        sources=records,
    )


def _safe_filename(value: str) -> str:
    normalized = INVALID_FILENAME_CHARACTERS.sub("-", value)
    normalized = re.sub(r"\s+", " ", normalized).strip(" .")
    if not normalized:
        raise NamingError("成片文件名为空")
    if len(normalized.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise NamingError(
            f"成片文件名超过 {MAX_FILENAME_BYTES} 字节，请缩短产品、利益点或达人名"
        )
    return normalized


def render_plan_filename(config: AppConfig, naming: PlanNamingMetadata) -> str:
    restriction_date = date.fromisoformat(naming.restriction_date).strftime(
        config.output.naming.restriction_date.output_format
    )
    values = {
        "product": naming.product,
        "benefit": naming.benefit,
        "talents": config.output.naming.talent.separator.join(naming.talents),
        "restriction_date": restriction_date,
    }
    try:
        rendered = config.output.naming.template.format(**values)
    except (KeyError, ValueError) as exc:
        raise NamingError(f"成片命名模板无效: {exc}") from exc
    rendered = _safe_filename(rendered)
    suffix = Path(rendered).suffix
    if not suffix:
        rendered = f"{rendered}.mp4"
    elif suffix.lower() != ".mp4":
        raise NamingError("业务命名的扩展名必须是 .mp4")
    return _safe_filename(rendered)


def append_duplicate_suffix(config: AppConfig, filename: str, serial: int) -> str:
    try:
        suffix = config.output.naming.duplicate_suffix.format(serial=serial)
    except (KeyError, ValueError) as exc:
        raise NamingError(f"重名后缀模板无效: {exc}") from exc
    path = Path(filename)
    return _safe_filename(f"{path.stem}{suffix}{path.suffix}")
