from __future__ import annotations

import copy
import re
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


BENEFIT_CATEGORY_PATTERN = re.compile(r"^benefit_[1-9][0-9]*$")
MAX_BENEFIT_CATEGORIES = 20


def is_benefit_category(category: str) -> bool:
    return BENEFIT_CATEGORY_PATTERN.fullmatch(category) is not None


class SourceMode(StrEnum):
    REQUIRED = "required"
    OPTIONAL = "optional"
    DISABLED = "disabled"


class AssetItemConfig(BaseModel):
    path: str
    enabled: bool = True
    weight: float = Field(default=1, ge=0)
    tags: list[str] = Field(default_factory=list)


class SourceGroupConfig(BaseModel):
    mode: SourceMode = SourceMode.REQUIRED
    directory: str
    extensions: list[str] = Field(default_factory=lambda: [".mp4"])
    default_weight: float = Field(default=1, ge=0)
    image_duration_seconds: float = Field(default=1.5, gt=0)
    items: list[AssetItemConfig] = Field(default_factory=list)

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
    # 每个配置固定使用唯一一张利益点图片；directory 仅用于兼容旧配置。
    file: str = ""
    directory: str | None = Field(default=None, exclude=True)
    image_duration_seconds: float = Field(default=1.5, gt=0)
    placement: OverlayPlacement = Field(default_factory=OverlayPlacement)
    timing: OverlayTiming = Field(default_factory=OverlayTiming)

    @model_validator(mode="after")
    def migrate_legacy_directory(self) -> BenefitOverlayConfig:
        if not self.file and self.directory:
            self.file = self.directory
        return self


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


class OutputConfig(BaseModel):
    directory: str
    width: int = Field(default=720, gt=0)
    height: int = Field(default=1280, gt=0)
    fps: float = Field(default=30, gt=0)
    video_codec: str = "libx264"
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


class AppConfig(BaseModel):
    schema_version: Literal[2] = 2
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
    matching: MatchingConfig = Field(default_factory=MatchingConfig)
    randomization: RandomizationConfig = Field(default_factory=RandomizationConfig)
    output: OutputConfig
    batch: BatchConfig = Field(default_factory=BatchConfig)
    scanner: ScannerConfig = Field(default_factory=ScannerConfig)

    @model_validator(mode="before")
    @classmethod
    def migrate_schema_v1(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        data = copy.deepcopy(value)
        version = data.get("schema_version", 1)
        if version == 2:
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
        core_groups = {"hook", "ending"}
        missing = core_groups - set(self.sources)
        if missing:
            raise ValueError(f"缺少必需素材组: {', '.join(sorted(missing))}")
        missing_from_timeline = core_groups - set(self.timeline)
        if missing_from_timeline:
            raise ValueError(
                "timeline 必须包含核心素材组: " + ", ".join(sorted(missing_from_timeline))
            )
        unknown = set(self.timeline) - set(self.sources)
        if unknown:
            raise ValueError(f"timeline 引用了未知素材组: {', '.join(sorted(unknown))}")

        if len(self.timeline) != len(set(self.timeline)):
            raise ValueError("timeline 中的素材组不得重复")

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
    has_audio: bool = False
    audio_codec: str | None = None
    sample_rate: int | None = None
    channels: int | None = None


class Asset(BaseModel):
    id: str
    category: str
    path: str
    name: str
    media_type: Literal["video", "image"]
    enabled: bool = True
    weight: float = Field(default=1, ge=0)
    tags: list[str] = Field(default_factory=list)
    exists: bool = True
    valid: bool = True
    error: str | None = None
    size_bytes: int | None = None
    modified_at: float | None = None
    probe: MediaProbe | None = None

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


class PlanItem(BaseModel):
    index: int
    selections: dict[str, Asset | None]
    overlay: Asset | None = None
    output_name: str
    estimated_duration: float


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


class JobCreateRequest(PreviewRequest):
    concurrency: int | None = Field(default=None, ge=1, le=16)
    auto_start: bool = True


class ConfigUpdateRequest(BaseModel):
    yaml_text: str


class StructuredConfigUpdateRequest(BaseModel):
    config: AppConfig


class CloneConfigRequest(BaseModel):
    new_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    new_name: str


class CreateConfigRequest(BaseModel):
    new_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    new_name: str = Field(min_length=1)


class LibraryPreflightRequest(BaseModel):
    parent_directory: str = Field(min_length=1)
    folder_name: str = Field(min_length=1)


class CreateLibraryRequest(LibraryPreflightRequest):
    new_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    new_name: str = Field(min_length=1)
    client_request_id: str = Field(min_length=8, max_length=128)


class AddBenefitRequest(BaseModel):
    client_request_id: str = Field(min_length=8, max_length=128)
    current_config_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


class SliceAssignment(BaseModel):
    segment_index: int = Field(ge=1)
    category: str = Field(min_length=1)


class TimelineSliceRequest(BaseModel):
    analysis_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    config_id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    assignments: list[SliceAssignment] = Field(min_length=1)
    client_request_id: str = Field(min_length=8, max_length=128)


class WeightUpdate(BaseModel):
    category: str
    path: str
    enabled: bool
    weight: float = Field(ge=0)
    tags: list[str] = Field(default_factory=list)


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


class TimelineAnalyzeRequest(BaseModel):
    source_path: str = Field(min_length=1)
    scene_threshold: float = Field(default=0.3, ge=0.05, le=0.9)
    silence_duration_seconds: float = Field(default=0.35, ge=0.1, le=3)


class TimelineDecisionRequest(BaseModel):
    analysis_id: str = Field(pattern=r"^[a-f0-9]{24}$")
    frame_indexes: list[int]


def resolve_directory(config: AppConfig, directory: str) -> Path:
    path = Path(directory).expanduser()
    return path if path.is_absolute() else Path(config.source_root).expanduser() / path


def public_model(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
