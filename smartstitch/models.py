from __future__ import annotations

import copy
import re
import urllib.parse
from enum import StrEnum
from pathlib import Path
from string import Formatter
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


BENEFIT_CATEGORY_PATTERN = re.compile(r"^benefit_[1-9][0-9]*$")
POOL_CATEGORY_PATTERN = re.compile(r"^pool_[1-9][0-9]*$")
MAX_BENEFIT_CATEGORIES = 20
MAX_GENERIC_POOLS = 50
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".bmp"}
PATH_QUOTE_PAIRS = {"'": "'", '"': '"', "‘": "’", "“": "”"}


def is_benefit_category(category: str) -> bool:
    return BENEFIT_CATEGORY_PATTERN.fullmatch(category) is not None


def is_pool_category(category: str) -> bool:
    return POOL_CATEGORY_PATTERN.fullmatch(category) is not None


def normalize_path_input(value: Any) -> Any:
    """Normalize a pathname copied from macOS without changing inner characters."""
    if not isinstance(value, str):
        return value
    normalized = value.strip()
    if len(normalized) >= 2 and PATH_QUOTE_PAIRS.get(normalized[0]) == normalized[-1]:
        return normalized[1:-1]
    return normalized


class SourceMode(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    DISABLED = "disabled"


class AssetItemConfig(BaseModel):
    path: str
    enabled: bool = True
    weight: float = Field(default=1, ge=0)
    tags: list[str] = Field(default_factory=list)
    image_duration_seconds: float | None = Field(default=None, ge=0.1, le=60)

    _normalize_path = field_validator("path", mode="before")(normalize_path_input)


class SourceGroupConfig(BaseModel):
    label: str = Field(default="", max_length=100)
    description: str = Field(default="", max_length=500)
    mode: SourceMode = SourceMode.REQUIRED
    media_type: Literal["video", "image"] = "video"
    directory: str
    extensions: list[str] = Field(default_factory=lambda: [".mp4"])
    default_weight: float = Field(default=1, ge=0)
    image_duration_seconds: float = Field(default=1.5, gt=0)
    items: list[AssetItemConfig] = Field(default_factory=list)

    _normalize_directory = field_validator("directory", mode="before")(
        normalize_path_input
    )

    @field_validator("extensions")
    @classmethod
    def normalize_extensions(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            value = value.lower().strip()
            normalized.append(value if value.startswith(".") else f".{value}")
        return normalized

    @model_validator(mode="after")
    def reject_duplicate_items(self) -> SourceGroupConfig:
        paths = [item.path for item in self.items]
        if len(paths) != len(set(paths)):
            raise ValueError("items 中存在重复路径")
        return self


class OverlayPlacement(BaseModel):
    scale_mode: Literal["original", "fit", "stretch"] = "original"
    x: str = "0"
    y: str = "0"
    shrink_if_oversized: bool = True
    opacity: float = Field(default=1, ge=0, le=1)


class OverlayTiming(BaseModel):
    scope: Literal["full", "main", "benefits", "custom"] = "benefits"
    start_seconds: float = Field(default=0, ge=0)
    end_seconds: float | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def validate_custom_range(self) -> OverlayTiming:
        if self.scope == "custom" and self.end_seconds is not None:
            if self.end_seconds <= self.start_seconds:
                raise ValueError("custom 结束时间必须大于开始时间")
        return self


class BenefitOverlayConfig(BaseModel):
    mode: SourceMode = SourceMode.DISABLED
    # 每个配置固定使用唯一一张风险提示语图片；directory 仅用于兼容旧配置。
    file: str = ""
    directory: str | None = Field(default=None, exclude=True)
    image_duration_seconds: float = Field(default=1.5, gt=0)
    placement: OverlayPlacement = Field(default_factory=OverlayPlacement)
    timing: OverlayTiming = Field(default_factory=OverlayTiming)

    _normalize_paths = field_validator("file", "directory", mode="before")(
        normalize_path_input
    )

    @model_validator(mode="after")
    def migrate_legacy_directory(self) -> BenefitOverlayConfig:
        if not self.file and self.directory:
            self.file = self.directory
        return self


class VisualDedupForegroundConfig(BaseModel):
    scale: float = Field(default=0.9, ge=0.7, le=1.0)


class VisualDedupBackgroundConfig(BaseModel):
    enabled: bool = True
    mode: Literal["gaussian_blur"] = "gaussian_blur"
    sigma: float = Field(default=20.0, ge=0, le=100)
    steps: int = Field(default=2, ge=1, le=6)
    brightness: float = Field(default=0.0, ge=-1, le=1)


class VisualBorderOverlayConfig(BaseModel):
    mode: SourceMode = SourceMode.DISABLED
    source: Literal["global_library", "legacy_file"] = "global_library"
    selection_mode: Literal["random", "fixed"] = "random"
    fixed_asset_id: str = ""
    enabled_asset_ids: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    file: str = ""
    media_kind: Literal["auto"] = "auto"
    scale_mode: Literal["exact", "stretch"] = "exact"
    playback: Literal["loop"] = "loop"
    opacity: float = Field(default=1.0, ge=0, le=1)
    alpha_mode: Literal["auto", "straight", "premultiplied"] = "auto"

    _normalize_file = field_validator("file", mode="before")(normalize_path_input)

    @field_validator("fixed_asset_id")
    @classmethod
    def strip_fixed_asset_id(cls, value: str) -> str:
        return value.strip()

    @field_validator("enabled_asset_ids")
    @classmethod
    def normalize_enabled_asset_ids(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("weights")
    @classmethod
    def validate_border_weights(cls, values: dict[str, float]) -> dict[str, float]:
        normalized = {key.strip(): value for key, value in values.items() if key.strip()}
        if any(value < 0 for value in normalized.values()):
            raise ValueError("全局边框权重不能为负数")
        return normalized


class VisualEffectLayerConfig(BaseModel):
    layer_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=100)
    type: Literal["overlay", "blur_frame"]
    enabled: bool = True
    required: bool = True
    opacity_percent: int = Field(default=100, ge=0, le=100)
    library_id: str = ""
    selection_mode: Literal["random", "fixed"] = "random"
    fixed_asset_id: str = ""
    enabled_asset_ids: list[str] = Field(default_factory=list)
    weights: dict[str, float] = Field(default_factory=dict)
    legacy_file: str = ""
    scale_mode: Literal["exact", "stretch"] = "exact"
    playback: Literal["loop"] = "loop"
    alpha_mode: Literal["auto", "straight", "premultiplied"] = "auto"
    foreground_scale: float = Field(default=0.9, ge=0.7, le=1.0)
    sigma: float = Field(default=20.0, ge=0, le=100)
    steps: int = Field(default=2, ge=1, le=6)
    brightness: float = Field(default=0.0, ge=-1, le=1)

    _normalize_legacy_file = field_validator("legacy_file", mode="before")(
        normalize_path_input
    )

    @field_validator("library_id", "fixed_asset_id")
    @classmethod
    def strip_identifiers(cls, value: str) -> str:
        return value.strip()

    @field_validator("enabled_asset_ids")
    @classmethod
    def normalize_enabled_assets(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("weights")
    @classmethod
    def validate_weights(cls, values: dict[str, float]) -> dict[str, float]:
        normalized = {key.strip(): value for key, value in values.items() if key.strip()}
        if any(value < 0 for value in normalized.values()):
            raise ValueError("特效素材权重不能为负数")
        return normalized

    @model_validator(mode="after")
    def validate_layer_kind(self) -> VisualEffectLayerConfig:
        if self.type == "overlay" and not self.library_id and not self.legacy_file:
            raise ValueError("素材特效层必须选择全局素材库")
        return self


def _default_visual_effect_layers() -> list[VisualEffectLayerConfig]:
    return [
        VisualEffectLayerConfig(
            layer_id="blur-frame-main",
            name="模糊边框",
            type="blur_frame",
        )
    ]


class VisualDedupConfig(BaseModel):
    enabled: bool = False
    foreground: VisualDedupForegroundConfig = Field(
        default_factory=VisualDedupForegroundConfig
    )
    background: VisualDedupBackgroundConfig = Field(
        default_factory=VisualDedupBackgroundConfig
    )
    border_overlay: VisualBorderOverlayConfig = Field(
        default_factory=VisualBorderOverlayConfig
    )
    effect_layers: list[VisualEffectLayerConfig] = Field(
        default_factory=_default_visual_effect_layers, max_length=10
    )
    effect_layers_explicit: bool = Field(default=False, exclude=True)

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_master_switch(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = copy.deepcopy(value)
        data["effect_layers_explicit"] = "effect_layers" in data
        legacy_enabled = data.get("enabled")
        background = data.setdefault("background", {})
        if not isinstance(background, dict):
            return data
        if "enabled" not in background and isinstance(legacy_enabled, bool):
            background["enabled"] = True
        if "effect_layers" not in data:
            foreground = data.get("foreground")
            if not isinstance(foreground, dict):
                foreground = {}
            border = data.get("border_overlay")
            if not isinstance(border, dict):
                border = {}
            layers: list[dict[str, Any]] = []
            border_mode = str(border.get("mode", SourceMode.DISABLED))
            if border_mode != SourceMode.DISABLED:
                layers.append(
                    {
                        "layer_id": "legacy-visual-border",
                        "name": "透明边框",
                        "type": "overlay",
                        "enabled": True,
                        "required": border_mode == SourceMode.REQUIRED,
                        "library_id": "effect_1",
                        "selection_mode": border.get("selection_mode", "random"),
                        "fixed_asset_id": border.get("fixed_asset_id", ""),
                        "enabled_asset_ids": border.get("enabled_asset_ids", []),
                        "weights": border.get("weights", {}),
                        "legacy_file": border.get("file", ""),
                        "scale_mode": border.get("scale_mode", "exact"),
                        "playback": border.get("playback", "loop"),
                        "alpha_mode": border.get("alpha_mode", "auto"),
                        "opacity_percent": round(float(border.get("opacity", 1)) * 100),
                    }
                )
            if background.get("enabled", True):
                layers.append(
                    {
                        "layer_id": "blur-frame-main",
                        "name": "模糊边框",
                        "type": "blur_frame",
                        "enabled": True,
                        "opacity_percent": 100,
                        "foreground_scale": foreground.get("scale", 0.9),
                        "sigma": background.get("sigma", 20),
                        "steps": background.get("steps", 2),
                        "brightness": background.get("brightness", 0),
                    }
                )
            data["effect_layers"] = layers
        effect_layers = data.get("effect_layers")
        if isinstance(effect_layers, list):
            for layer in effect_layers:
                if (
                    isinstance(layer, dict)
                    and layer.get("type") == "overlay"
                    and layer.get("library_id") == "visual-border"
                ):
                    layer["library_id"] = "effect_1"
        return data

    @model_validator(mode="after")
    def validate_effect_layers(self) -> VisualDedupConfig:
        identifiers = [layer.layer_id for layer in self.effect_layers]
        if len(identifiers) != len(set(identifiers)):
            raise ValueError("effect_layers 中存在重复 layer_id")
        if sum(layer.type == "blur_frame" for layer in self.effect_layers) > 1:
            raise ValueError("一个配置最多只能有一个模糊边框层")
        return self

    def resolved_effect_layers(self) -> list[VisualEffectLayerConfig]:
        if self.effect_layers_explicit:
            return list(self.effect_layers)
        layers: list[VisualEffectLayerConfig] = []
        border = self.border_overlay
        if border.mode != SourceMode.DISABLED:
            layers.append(
                VisualEffectLayerConfig(
                    layer_id="legacy-visual-border",
                    name="透明边框",
                    type="overlay",
                    enabled=True,
                    required=border.mode == SourceMode.REQUIRED,
                    library_id="effect_1",
                    selection_mode=border.selection_mode,
                    fixed_asset_id=border.fixed_asset_id,
                    enabled_asset_ids=border.enabled_asset_ids,
                    weights=border.weights,
                    legacy_file=border.file,
                    scale_mode=border.scale_mode,
                    playback=border.playback,
                    alpha_mode=border.alpha_mode,
                    opacity_percent=round(border.opacity * 100),
                )
            )
        if self.background.enabled:
            layers.append(
                VisualEffectLayerConfig(
                    layer_id="blur-frame-main",
                    name="模糊边框",
                    type="blur_frame",
                    enabled=True,
                    opacity_percent=100,
                    foreground_scale=self.foreground.scale,
                    sigma=self.background.sigma,
                    steps=self.background.steps,
                    brightness=self.background.brightness,
                )
            )
        return layers


class MatchingConfig(BaseModel):
    enabled: bool = False
    strategy: Literal["product_tag"] = "product_tag"
    allow_untagged_as_global: bool = True
    on_missing_match: Literal["error", "fallback_global"] = "error"


class RandomizationConfig(BaseModel):
    mode: Literal["quota_shuffle", "independent_random"] = "quota_shuffle"
    duplicate_policy: Literal["allow", "best_effort", "strict"] = "best_effort"
    default_seed: int | None = None


class LoudnessConfig(BaseModel):
    enabled: bool = False
    target_lufs: float = Field(default=-14, ge=-24, le=-8)
    loudness_range_lu: float = Field(default=7, ge=1, le=20)
    true_peak_dbtp: float = Field(default=-1.5, ge=-9, le=-0.1)
    preview_duration_seconds: float = Field(default=12, ge=3, le=30)


NAMING_TEMPLATE_FIELDS = {
    "product",
    "benefit",
    "talents",
    "restriction_date",
    "sequence",
}
DUPLICATE_SUFFIX_FIELDS = {"serial"}


def _validate_template_fields(value: str, allowed: set[str], label: str) -> str:
    try:
        parsed = list(Formatter().parse(value))
    except ValueError as exc:
        raise ValueError(f"{label}无效: {exc}") from exc
    fields = {field for _, field, _, _ in parsed if field is not None}
    unknown = fields - allowed
    if unknown:
        raise ValueError(f"{label}包含未知变量: {', '.join(sorted(unknown))}")
    if not fields:
        raise ValueError(f"{label}至少需要一个变量")
    return value


class NamingSourceMetadataConfig(BaseModel):
    categories: list[str] = Field(default_factory=lambda: ["pool_*"])
    strip_smartstitch_suffix: bool = True
    pattern: str = (
        r"^(?P<source_index>\d+)_(?P<talent>[^-]+)-"
        r"(?P<source_title>.+)-(?P<restriction_date>\d{4}-\d{2}-\d{2})$"
    )
    restriction_date_formats: list[str] = Field(
        default_factory=lambda: ["%Y-%m-%d"]
    )
    on_unmatched: Literal["error"] = "error"

    @field_validator("categories")
    @classmethod
    def validate_categories(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        if not normalized:
            raise ValueError("参与命名的视频库不能为空")
        invalid = [
            value for value in normalized if value != "pool_*" and not is_pool_category(value)
        ]
        if invalid:
            raise ValueError(
                "命名视频库必须是 pool_<正整数> 或 pool_*: "
                + ", ".join(invalid)
            )
        return list(dict.fromkeys(normalized))

    @field_validator("pattern")
    @classmethod
    def validate_pattern(cls, value: str) -> str:
        try:
            compiled = re.compile(value)
        except re.error as exc:
            raise ValueError(f"文件名解析正则无效: {exc}") from exc
        missing = {"talent", "restriction_date"} - set(compiled.groupindex)
        if missing:
            raise ValueError(
                "文件名解析正则缺少命名分组: "
                + ", ".join(sorted(missing))
            )
        return value

    @field_validator("restriction_date_formats")
    @classmethod
    def validate_date_formats(cls, values: list[str]) -> list[str]:
        normalized = [value.strip() for value in values if value.strip()]
        supported = {"%Y-%m-%d", "%Y%m%d", "%y%m%d"}
        invalid = [value for value in normalized if value not in supported]
        if not normalized or invalid:
            detail = ", ".join(invalid) if invalid else "空列表"
            raise ValueError(
                f"限制日期格式仅支持 %Y-%m-%d、%Y%m%d 和 %y%m%d: {detail}"
            )
        return list(dict.fromkeys(normalized))


class NamingTalentConfig(BaseModel):
    merge: Literal["ordered_unique"] = "ordered_unique"
    separator: str = Field(default="+", min_length=1, max_length=10)


class NamingRestrictionDateConfig(BaseModel):
    merge: Literal["earliest"] = "earliest"
    output_format: Literal["%Y%m%d"] = "%Y%m%d"


class OutputNamingConfig(BaseModel):
    enabled: bool = False
    product: str = Field(default="", max_length=100)
    benefit: str = Field(default="", max_length=100)
    sequence_start: int = Field(default=1, ge=1, le=999999)
    template: str = "{product}-{benefit}-{talents}-{restriction_date}-{sequence}.mp4"
    source_metadata: NamingSourceMetadataConfig = Field(
        default_factory=NamingSourceMetadataConfig
    )
    talent: NamingTalentConfig = Field(default_factory=NamingTalentConfig)
    restriction_date: NamingRestrictionDateConfig = Field(
        default_factory=NamingRestrictionDateConfig
    )
    duplicate_suffix: str = "-{serial:02d}"

    @field_validator("product", "benefit")
    @classmethod
    def strip_business_fields(cls, value: str) -> str:
        return value.strip()

    @field_validator("template")
    @classmethod
    def validate_template(cls, value: str) -> str:
        validated = _validate_template_fields(
            value, NAMING_TEMPLATE_FIELDS, "成片命名模板"
        )
        fields = {field for _, field, _, _ in Formatter().parse(validated) if field}
        missing = NAMING_TEMPLATE_FIELDS - fields
        if missing:
            raise ValueError(
                "成片命名模板缺少必需变量: "
                + ", ".join(sorted(missing))
            )
        return validated

    @field_validator("duplicate_suffix")
    @classmethod
    def validate_duplicate_suffix(cls, value: str) -> str:
        return _validate_template_fields(value, DUPLICATE_SUFFIX_FIELDS, "重名后缀模板")


class FeishuBaseSyncConfig(BaseModel):
    enabled: bool = False
    base_url: str = ""
    table_id: str = Field(default="", max_length=128, pattern=r"^[A-Za-z0-9_-]*$")
    field_schema: Literal["auto", "generic", "taobao_flash"] = "auto"
    trigger: Literal["job_terminal"] = "job_terminal"
    row_scope: Literal["all_items", "succeeded_only"] = "all_items"
    write_mode: Literal["upsert"] = "upsert"

    @field_validator("base_url", "table_id")
    @classmethod
    def strip_feishu_target_fields(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def require_enabled_target(self) -> FeishuBaseSyncConfig:
        if self.enabled and not self.base_url:
            raise ValueError("启用飞书多维表格同步时必须填写多维表格链接")
        if self.enabled:
            parsed = urllib.parse.urlparse(self.base_url)
            parts = [part for part in parsed.path.split("/") if part]
            has_resource_token = any(
                marker in parts and parts.index(marker) + 1 < len(parts)
                for marker in ("base", "wiki")
            )
            if parsed.scheme not in {"http", "https"} or not has_resource_token:
                raise ValueError("请填写飞书多维表格的 /base/ 或 /wiki/ 链接")
        if self.enabled and not self.table_id:
            raise ValueError("启用飞书多维表格同步时必须选择数据表")
        return self


class OutputConfig(BaseModel):
    directory: str
    width: int = Field(default=720, gt=0)
    height: int = Field(default=1280, gt=0)
    fps: float = Field(default=30, gt=0)
    video_codec: str = "h264_videotoolbox"
    pixel_format: str = "yuv420p"
    video_preset: str = "medium"
    rate_control: Literal["crf", "vbr"] = "crf"
    video_bitrate_kbps: int = Field(default=3000, gt=0)
    crf: int = Field(default=18, ge=0, le=51)
    audio_codec: str = "aac"
    audio_bitrate: str = "192k"
    audio_sample_rate: int = Field(default=48000, gt=0)
    audio_channels: Literal[1, 2] = 2
    loudness: LoudnessConfig = Field(default_factory=LoudnessConfig)
    resize_mode: Literal["fit_pad", "fill_crop", "stretch"] = "fit_pad"
    background_color: str = "black"
    filename_template: str = "{config}_{date}_{batch}_{index:04d}.mp4"
    collision_policy: Literal["increment", "error", "overwrite"] = "increment"
    faststart: bool = True
    naming: OutputNamingConfig = Field(default_factory=OutputNamingConfig)
    feishu_base_sync: FeishuBaseSyncConfig = Field(default_factory=FeishuBaseSyncConfig)

    _normalize_directory = field_validator("directory", mode="before")(
        normalize_path_input
    )


class BatchConfig(BaseModel):
    default_count: int = Field(default=10, ge=1)
    max_count: int = Field(default=1000, ge=1)
    concurrency: int = Field(default=1, ge=1, le=16)
    retry_count: int = Field(default=1, ge=0, le=10)
    minimum_free_space_gb: float = Field(default=5, ge=0)


class ScannerConfig(BaseModel):
    recursive: bool = False
    ignore_hidden_files: bool = True
    ignore_prefixes: list[str] = Field(default_factory=lambda: ["._", ".~"])
    ignore_names: list[str] = Field(default_factory=lambda: [".DS_Store"])
    probe_cache_enabled: bool = True
    probe_concurrency: int = Field(default=8, ge=1, le=16)
    probe_timeout_seconds: float = Field(default=15, ge=3, le=120)
    probe_failure_ttl_seconds: int = Field(default=60, ge=0, le=3600)


class AppConfig(BaseModel):
    schema_version: Literal[2, 3] = 2
    workflow_type: Literal["taobao_flash", "generic"] = "taobao_flash"
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    name: str
    enabled: bool = True
    description: str = ""
    source_root: str
    timeline: list[str] = Field(
        default_factory=lambda: ["pre_roll", "hook", "benefit_1", "ending", "end_card"]
    )
    sources: dict[str, SourceGroupConfig]
    benefit_overlays: BenefitOverlayConfig
    visual_dedup: VisualDedupConfig = Field(default_factory=VisualDedupConfig)
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    randomization: RandomizationConfig = Field(default_factory=RandomizationConfig)
    output: OutputConfig
    batch: BatchConfig = Field(default_factory=BatchConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)

    _normalize_source_root = field_validator("source_root", mode="before")(
        normalize_path_input
    )

    @model_validator(mode="before")
    @classmethod
    def migrate_schema_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = copy.deepcopy(value)
        version = data.get("schema_version", 1)
        if version in {2, 3}:
            return data
        if version != 1:
            raise ValueError(f"不支持的 schema_version: {version}")

        sources = data.get("sources")
        if isinstance(sources, dict) and "benefit_video" in sources:
            if "benefit_1" in sources:
                raise ValueError(
                    "旧配置同时包含 benefit_video 和 benefit_1，无法自动迁移"
                )
            sources["benefit_1"] = sources.pop("benefit_video")

        timeline = data.get("timeline")
        if isinstance(timeline, list):
            data["timeline"] = [
                "benefit_1" if item == "benefit_video" else item for item in timeline
            ]

        overlays = data.get("benefit_overlays")
        if isinstance(overlays, dict):
            timing = overlays.get("timing")
            if isinstance(timing, dict) and timing.get("scope") == "benefit_video":
                timing["scope"] = "benefits"

        data["schema_version"] = 2
        return data

    @model_validator(mode="after")
    def require_core_groups(self) -> AppConfig:
        unknown = set(self.timeline) - set(self.sources)
        if unknown:
            raise ValueError(f"timeline 引用了未知素材组: {', '.join(sorted(unknown))}")

        if len(self.timeline) != len(set(self.timeline)):
            raise ValueError("timeline 中的素材组不得重复")

        if self.workflow_type == "generic":
            if self.schema_version != 3:
                raise ValueError("通用视频库必须使用 schema_version 3")
            malformed_pools = {
                category
                for category in set(self.sources) | set(self.timeline)
                if not is_pool_category(category)
            }
            if malformed_pools:
                raise ValueError(
                    "通用视频库素材组必须命名为 pool_<正整数>: "
                    + ", ".join(sorted(malformed_pools))
                )
            if len(self.sources) > MAX_GENERIC_POOLS:
                raise ValueError(f"通用视频库最多 {MAX_GENERIC_POOLS} 个素材库")
            unlabeled = sorted(
                category
                for category, group in self.sources.items()
                if not group.label.strip()
            )
            if unlabeled:
                raise ValueError(
                    "通用视频库必须填写显示名称: " + ", ".join(unlabeled)
                )
            for category, group in self.sources.items():
                extensions = set(group.extensions)
                directory = resolve_directory(self, group.directory).resolve()
                if group.media_type == "image":
                    if not 0.1 <= group.image_duration_seconds <= 60:
                        raise ValueError(f"{category}: 图片库默认展示时长需在 0.1～60 秒之间")
                    if not extensions or extensions - IMAGE_EXTENSIONS:
                        raise ValueError(f"{category}: 图片库只能使用图片扩展名")
                elif extensions & IMAGE_EXTENSIONS:
                    raise ValueError(f"{category}: 视频库不能使用图片扩展名")
                for item in group.items:
                    item_path = Path(item.path).expanduser()
                    if not item_path.is_absolute():
                        item_path = directory / item_path
                    if not item_path.resolve().is_relative_to(directory):
                        raise ValueError(f"{category}: 素材条目越过素材库目录")
                    if item_path.suffix.lower() not in extensions:
                        raise ValueError(f"{category}: 素材条目扩展名与库类型不符")
                    if group.media_type == "image":
                        if Path(item.path).suffix.lower() not in IMAGE_EXTENSIONS:
                            raise ValueError(f"{category}: 图片库条目必须是图片")
                    elif item.image_duration_seconds is not None:
                        raise ValueError(f"{category}: 视频库条目不能设置图片时长")
            if (
                self.benefit_overlays.mode != SourceMode.DISABLED
                and self.benefit_overlays.timing.scope not in {"full", "custom"}
            ):
                raise ValueError("通用视频库的贴图范围仅支持 full 或 custom")
            unreferenced = sorted(set(self.sources) - set(self.timeline))
            if unreferenced:
                raise ValueError(
                    "通用视频库的 timeline 必须包含全部素材组: "
                    + ", ".join(unreferenced)
                )
            naming = self.output.naming
            if naming.enabled:
                missing_fields = [
                    label
                    for label, value in (
                        ("产品", naming.product),
                        ("利益点", naming.benefit),
                    )
                    if not value
                ]
                if missing_fields:
                    raise ValueError(
                        "启用业务命名时必须填写: " + ", ".join(missing_fields)
                    )
                missing_pools = sorted(
                    category
                    for category in naming.source_metadata.categories
                    if category != "pool_*" and category not in self.sources
                )
                if missing_pools:
                    raise ValueError(
                        "命名规则引用了不存在的视频库: "
                        + ", ".join(missing_pools)
                    )
            return self

        if any(
            item.image_duration_seconds is not None
            for group in self.sources.values()
            for item in group.items
        ):
            raise ValueError("逐张图片时长仅支持通用项目图片库")
        if self.output.naming.enabled:
            raise ValueError("业务动态命名目前仅支持通用视频项目")
        if self.schema_version != 2:
            raise ValueError("淘宝闪购配置必须使用 schema_version 2")
        core_groups = {"hook", "ending"}
        missing = core_groups - set(self.sources)
        if missing:
            raise ValueError(f"缺少必需素材组: {', '.join(sorted(missing))}")
        missing_from_timeline = core_groups - set(self.timeline)
        if missing_from_timeline:
            raise ValueError(
                "timeline 必须包含核心素材组: " + ", ".join(sorted(missing_from_timeline))
            )
        malformed_benefits = {
            category
            for category in set(self.sources) | set(self.timeline)
            if category.startswith("benefit_")
            and category != "benefit_overlay"
            and not is_benefit_category(category)
        }
        if malformed_benefits:
            raise ValueError(
                "利益点素材组必须命名为 benefit_<正整数>: "
                + ", ".join(sorted(malformed_benefits))
            )

        source_benefits = [category for category in self.sources if is_benefit_category(category)]
        benefit_categories = [
            category for category in self.timeline if is_benefit_category(category)
        ]
        if len(source_benefits) > MAX_BENEFIT_CATEGORIES:
            raise ValueError(f"利益点段最多 {MAX_BENEFIT_CATEGORIES} 个")
        active_benefits = [
            category
            for category in benefit_categories
            if self.sources[category].mode != SourceMode.DISABLED
        ]
        if not active_benefits:
            raise ValueError("timeline 至少需要一个未停用的利益点段")

        unreferenced_enabled = [
            category
            for category, group in self.sources.items()
            if category not in self.timeline and group.mode != SourceMode.DISABLED
        ]
        if unreferenced_enabled:
            raise ValueError(
                "sources 中的启用素材组未被 timeline 引用: "
                + ", ".join(sorted(unreferenced_enabled))
            )

        return self

    def benefit_categories(self, *, active_only: bool = False) -> list[str]:
        categories = [category for category in self.timeline if is_benefit_category(category)]
        if active_only:
            return [
                category
                for category in categories
                if self.sources[category].mode != SourceMode.DISABLED
            ]
        return categories


class MediaProbe(BaseModel):
    duration: float
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    video_codec: str | None = None
    pixel_format: str | None = None
    has_alpha: bool = False
    has_audio: bool = False
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None


class AssetNamingMetadata(BaseModel):
    source_stem: str
    talent: str
    restriction_date: str


class NamingSourceRecord(AssetNamingMetadata):
    category: str
    asset: str


class PlanNamingMetadata(BaseModel):
    product: str
    benefit: str
    talents: list[str]
    restriction_date: str | None = None
    sequence: int = Field(ge=1)
    sources: list[NamingSourceRecord]


class Asset(BaseModel):
    id: str
    category: str
    path: str
    name: str
    media_type: Literal["video", "image"]
    image_duration_seconds: float | None = None
    enabled: bool = True
    weight: float = Field(default=1, ge=0)
    tags: list[str] = Field(default_factory=list)
    exists: bool = True
    valid: bool = True
    error: str | None = None
    size_bytes: int | None = None
    modified_at: float | None = None
    content_hash: str | None = None
    alpha_mode: Literal["straight", "premultiplied"] | None = None
    probe: MediaProbe | None = None
    naming_metadata: AssetNamingMetadata | None = None

    @property
    def selectable(self) -> bool:
        return self.enabled and self.weight > 0 and self.exists and self.valid


class ScanResult(BaseModel):
    config_id: str
    assets: dict[str, list[Asset]]
    errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class SourceInventoryItem(BaseModel):
    category: str
    label: str
    directory: str
    directory_status: Literal["available", "empty", "unavailable"]
    discovered_count: int = Field(ge=0)
    enabled: bool


class SourceInventoryResult(BaseModel):
    config_id: str
    sources: dict[str, SourceInventoryItem]


class PlannedVisualEffect(BaseModel):
    layer: VisualEffectLayerConfig
    asset: Asset | None = None


class PlanItem(BaseModel):
    index: int
    selections: dict[str, Asset | None]
    overlay: Asset | None = None
    visual_border: Asset | None = None
    visual_effects: list[PlannedVisualEffect] = Field(default_factory=list)
    output_name: str
    estimated_duration: float
    naming: PlanNamingMetadata | None = None


class BatchPlan(BaseModel):
    config_id: str
    config_name: str
    count: int
    seed: int
    algorithm: str
    items: list[PlanItem]
    distribution: dict[str, dict[str, int]]
    warnings: list[str] = Field(default_factory=list)


class PreviewRequest(BaseModel):
    config_id: str
    count: int = Field(ge=1)
    seed: int | None = None
    output_directory: str | None = None

    _normalize_output_directory = field_validator("output_directory", mode="before")(
        normalize_path_input
    )


class JobCreateRequest(PreviewRequest):
    concurrency: int | None = Field(default=None, ge=1, le=16)
    auto_start: bool = True


class BatchDedupRequest(BaseModel):
    source_directory: str
    visual_dedup: VisualDedupConfig

    _normalize_source_directory = field_validator("source_directory", mode="before")(
        normalize_path_input
    )


class FolderConcatRequest(BaseModel):
    directory_a: str
    directory_b: str
    output_directory: str | None = None

    _normalize_directory_a = field_validator("directory_a", mode="before")(
        normalize_path_input
    )
    _normalize_directory_b = field_validator("directory_b", mode="before")(
        normalize_path_input
    )
    _normalize_output_directory = field_validator("output_directory", mode="before")(
        normalize_path_input
    )


class ConfigUpdateRequest(BaseModel):
    yaml_text: str


class StructuredConfigUpdateRequest(BaseModel):
    config: AppConfig


class FeishuSettingsUpdateRequest(BaseModel):
    app_id: str = Field(default="", max_length=128)
    app_secret: str | None = Field(default=None, max_length=512)

    @field_validator("app_id", "app_secret")
    @classmethod
    def strip_feishu_credentials(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class FeishuConnectionTestRequest(BaseModel):
    base_url: str = Field(min_length=1, max_length=2048)
    app_id: str | None = Field(default=None, max_length=128)
    app_secret: str | None = Field(default=None, max_length=512)

    @field_validator("base_url", "app_id", "app_secret")
    @classmethod
    def strip_feishu_test_fields(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class UserProfileUpdateRequest(BaseModel):
    display_name: str = Field(min_length=1, max_length=50)
    switch_user: bool = False


class ConfigLockAcquireRequest(BaseModel):
    browser_session_id: str = Field(min_length=8, max_length=128)


class ConfigLockActionRequest(ConfigLockAcquireRequest):
    lease_token: str = Field(min_length=16, max_length=128)


class CloneConfigRequest(BaseModel):
    new_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    new_name: str


class CreateConfigRequest(BaseModel):
    new_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    new_name: str = Field(min_length=1)


class LibraryPreflightRequest(BaseModel):
    parent_directory: str = Field(min_length=1)
    folder_name: str = Field(min_length=1)
    workflow_type: Literal["taobao_flash", "generic"] = "taobao_flash"

    _normalize_parent_directory = field_validator("parent_directory", mode="before")(
        normalize_path_input
    )


class CreateLibraryRequest(LibraryPreflightRequest):
    new_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    new_name: str = Field(min_length=1)
    client_request_id: str = Field(min_length=8, max_length=128)


class AddBenefitRequest(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    current_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class AddPoolRequest(BaseModel):
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    mode: SourceMode = SourceMode.REQUIRED
    default_weight: float = Field(default=1, ge=0)
    media_type: Literal["video", "image"] = "video"
    image_duration_seconds: float = Field(default=1.5, ge=0.1, le=60)
    client_request_id: str = Field(min_length=8, max_length=128)
    current_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class UpdatePoolRequest(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)
    mode: SourceMode | None = None
    default_weight: float | None = Field(default=None, ge=0)
    image_duration_seconds: float | None = Field(default=None, ge=0.1, le=60)
    current_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReorderTimelineRequest(BaseModel):
    timeline: list[str] = Field(max_length=MAX_GENERIC_POOLS)
    current_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class DeletePoolRequest(BaseModel):
    current_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class ReplaceOverlayImageRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=255)
    data_base64: str = Field(min_length=1)
    current_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class GlobalVisualBorderUpdateRequest(BaseModel):
    library_revision: int = Field(ge=0)
    display_name: str | None = Field(default=None, min_length=1, max_length=255)
    enabled: bool | None = None
    default_weight: float | None = Field(default=None, ge=0)
    alpha_mode: Literal["straight", "premultiplied"] | None = None


class GlobalVisualEffectLibraryCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    library_revision: int = Field(ge=0)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("特效库名称不能为空")
        return value


class GlobalVisualEffectLibraryUpdateRequest(BaseModel):
    library_revision: int = Field(ge=0)
    name: str | None = Field(default=None, min_length=1, max_length=100)
    enabled: bool | None = None


class SliceAssignment(BaseModel):
    client_unit_id: str | None = Field(
        default=None, min_length=1, max_length=128, pattern=r"^[A-Za-z0-9._-]+$"
    )
    segment_indexes: list[int] = Field(min_length=1, max_length=20)
    category: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def migrate_single_segment_index(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = copy.deepcopy(value)
        has_legacy = "segment_index" in data
        has_grouped = "segment_indexes" in data
        if has_legacy and has_grouped:
            raise ValueError("segment_index 和 segment_indexes 不能同时提交")
        if has_legacy:
            data["segment_indexes"] = [data.pop("segment_index")]
        return data

    @field_validator("segment_indexes")
    @classmethod
    def reject_duplicate_segment_indexes(cls, values: list[int]) -> list[int]:
        if any(index < 1 for index in values):
            raise ValueError("片段序号必须大于等于 1")
        if len(values) != len(set(values)):
            raise ValueError("同一输出单元不能重复包含同一片段")
        return values


class TimelineSliceRequest(BaseModel):
    analysis_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    config_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    review_revision: str = Field(pattern=r"^[a-f0-9]{64}$")
    current_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    assignments: list[SliceAssignment] = Field(min_length=1)
    client_request_id: str = Field(min_length=8, max_length=128)


class WeightUpdate(BaseModel):
    category: str
    path: str
    enabled: bool
    weight: float = Field(ge=0)
    tags: list[str] = Field(default_factory=list)
    image_duration_seconds: float | None = Field(default=None, ge=0.1, le=60)

    _normalize_path = field_validator("path", mode="before")(normalize_path_input)


class WeightUpdateRequest(BaseModel):
    items: list[WeightUpdate]


class LoudnessPreviewRequest(BaseModel):
    config_id: str
    asset_path: str
    normalized: bool = True
    target_lufs: float = Field(default=-14, ge=-24, le=-8)
    loudness_range_lu: float = Field(default=7, ge=1, le=20)
    true_peak_dbtp: float = Field(default=-1.5, ge=-9, le=-0.1)
    duration_seconds: float = Field(default=12, ge=3, le=30)

    _normalize_asset_path = field_validator("asset_path", mode="before")(
        normalize_path_input
    )


class TimelineAnalyzeRequest(BaseModel):
    source_path: str = Field(min_length=1)
    scene_threshold: float = Field(default=0.3, ge=0.05, le=0.9)
    silence_duration_seconds: float = Field(default=0.35, ge=0.1, le=3)

    _normalize_source_path = field_validator("source_path", mode="before")(
        normalize_path_input
    )


class TimelineSourceDirectoryRequest(BaseModel):
    source_directory: str = Field(min_length=1)

    _normalize_source_directory = field_validator("source_directory", mode="before")(
        normalize_path_input
    )


class TimelineDecisionRequest(BaseModel):
    analysis_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    frame_indexes: list[int]


def resolve_directory(config: AppConfig, directory: str) -> Path:
    path = Path(directory).expanduser()
    return path if path.is_absolute() else Path(config.source_root).expanduser() / path


def public_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
