from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartstitch.config import ConfigStore
from smartstitch.library import (
    GENERIC_DIRECTORIES,
    MARKER_PATH,
    STANDARD_DIRECTORIES,
    LibraryConflictError,
    LibraryError,
    LibraryService,
)
from smartstitch.models import (
    AddBenefitRequest,
    AddPoolRequest,
    CreateLibraryRequest,
    DeletePoolRequest,
    LibraryPreflightRequest,
    ReorderTimelineRequest,
    SourceMode,
    UpdatePoolRequest,
)


def create_request(parent: Path, request_id: str = "request-12345678") -> CreateLibraryRequest:
    return CreateLibraryRequest(
        new_id="summer-sale",
        new_name="夏日促销",
        parent_directory=str(parent),
        folder_name="夏日 视频库",
        client_request_id=request_id,
    )


def create_generic_request(parent: Path) -> CreateLibraryRequest:
    return CreateLibraryRequest(
        new_id="generic-video",
        new_name="通用视频项目",
        parent_directory=str(parent),
        folder_name="通用视频项目库",
        workflow_type="generic",
        client_request_id="generic-request-123",
    )


def add_pool(service: LibraryService, store: ConfigStore, label: str, request_id: str):
    return service.add_pool(
        "generic-video",
        AddPoolRequest(
            label=label,
            client_request_id=request_id,
            current_config_hash=store.content_hash("generic-video"),
        ),
    )


def test_preflight_requires_an_existing_absolute_parent(tmp_path, monkeypatch):
    service = LibraryService(ConfigStore(tmp_path / "config"))

    with pytest.raises(LibraryError, match="绝对路径"):
        service.preflight(
            LibraryPreflightRequest(parent_directory="relative", folder_name="test")
        )
    with pytest.raises(LibraryError, match="不存在"):
        service.preflight(
            LibraryPreflightRequest(
                parent_directory=str(tmp_path / "missing"), folder_name="test"
            )
        )
    with pytest.raises(LibraryError, match="非法字符"):
        service.preflight(
            LibraryPreflightRequest(parent_directory=str(tmp_path), folder_name="../bad")
        )

    monkeypatch.setattr("smartstitch.library.os.access", lambda *_: False)
    with pytest.raises(LibraryError, match="不可写"):
        service.preflight(
            LibraryPreflightRequest(parent_directory=str(tmp_path), folder_name="test")
        )


def test_create_library_builds_complete_tree_and_config(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    request = create_request(tmp_path)

    result = service.create(request)
    root = tmp_path / "夏日 视频库"

    assert result["root_path"] == str(root)
    assert result["idempotent"] is False
    assert all((root / relative).is_dir() for relative in STANDARD_DIRECTORIES)
    marker = json.loads((root / MARKER_PATH).read_text(encoding="utf-8"))
    assert marker["layout_version"] == 1
    assert marker["created_for_config_id"] == "summer-sale"
    assert marker["next_benefit_number"] == 2
    assert marker["paths"]["overlays"] == "风险提示语图片"
    assert (root / "风险提示语图片").is_dir()
    assert not (root / "利益点图片").exists()

    config = store.load("summer-sale")
    assert config.source_root == str(root)
    assert config.sources["hook"].directory == "切片素材/引子"
    assert config.sources["benefit_1"].directory == "切片素材/利益点/1"
    assert config.output.directory == str(root / "成片输出")
    assert config.benefit_overlays.mode == SourceMode.OPTIONAL
    assert config.benefit_overlays.file == ""


def test_create_generic_library_starts_empty_and_uses_independent_layout(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)

    result = service.create(create_generic_request(tmp_path))
    root = Path(result["root_path"])

    assert all((root / relative).is_dir() for relative in GENERIC_DIRECTORIES)
    assert not (root / "切片素材").exists()
    marker = json.loads((root / MARKER_PATH).read_text(encoding="utf-8"))
    assert marker["layout_version"] == 2
    assert marker["next_pool_number"] == 1
    assert marker["paths"]["overlays"] == "风险提示语图片"
    assert (root / "风险提示语图片").is_dir()
    config = store.load("generic-video")
    assert config.schema_version == 3
    assert config.workflow_type == "generic"
    assert config.timeline == []
    assert config.sources == {}
    assert config.benefit_overlays.mode == SourceMode.OPTIONAL
    assert config.benefit_overlays.timing.scope == "full"


def test_inspect_upgrades_generic_library_with_overlay_directory(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    created = service.create(create_generic_request(tmp_path))
    root = Path(created["root_path"])
    legacy_config = store.load("generic-video")
    legacy_config.benefit_overlays.mode = SourceMode.DISABLED
    store.save_config("generic-video", legacy_config)
    marker_path = root / MARKER_PATH
    marker = json.loads(marker_path.read_text(encoding="utf-8"))
    del marker["paths"]["overlays"]
    marker_path.write_text(json.dumps(marker, ensure_ascii=False), encoding="utf-8")
    (root / "风险提示语图片").rmdir()

    inspected = service.inspect_by_config("generic-video")

    assert inspected["health"] == "healthy"
    assert inspected["config_updated"] is True
    assert (root / "风险提示语图片").is_dir()
    upgraded = json.loads(marker_path.read_text(encoding="utf-8"))
    assert upgraded["paths"]["overlays"] == "风险提示语图片"
    assert store.load("generic-video").benefit_overlays.mode == SourceMode.OPTIONAL


def test_generic_pool_lifecycle_and_reorder_preserve_deleted_directory(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    service.create(create_generic_request(tmp_path))

    first = add_pool(service, store, "开场", "pool-request-0001")
    second = add_pool(service, store, "产品展示", "pool-request-0002")
    first_directory = Path(first["directory"])
    assert first["pool_id"] == "pool_1"
    assert second["pool_id"] == "pool_2"
    assert first_directory.is_dir()

    updated = service.update_pool(
        "generic-video",
        "pool_2",
        UpdatePoolRequest(
            label="行动引导",
            description="最后一段",
            mode="optional",
            default_weight=2,
            current_config_hash=store.content_hash("generic-video"),
        ),
    )
    assert updated["config"]["sources"]["pool_2"]["label"] == "行动引导"

    reordered = service.reorder_timeline(
        "generic-video",
        ReorderTimelineRequest(
            timeline=["pool_2", "pool_1"],
            current_config_hash=store.content_hash("generic-video"),
        ),
    )
    assert reordered["timeline"] == ["pool_2", "pool_1"]

    deleted = service.delete_pool(
        "generic-video",
        "pool_1",
        DeletePoolRequest(current_config_hash=store.content_hash("generic-video")),
    )
    assert deleted["retained_directory"] == str(first_directory)
    assert first_directory.is_dir()
    assert store.load("generic-video").timeline == ["pool_2"]

    third = add_pool(service, store, "补充", "pool-request-0003")
    assert third["pool_id"] == "pool_3"


def test_generic_slice_targets_use_pool_labels(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    created = service.create(create_generic_request(tmp_path))
    add_pool(service, store, "开场画面", "pool-request-0001")

    targets = service.slice_targets("generic-video")

    assert targets[0] == {
        "category": "unclassified",
        "label": "未归类",
        "directory": str(Path(created["root_path"]) / "未归类"),
    }
    assert targets[1]["category"] == "pool_1"
    assert targets[1]["label"] == "开场画面"
    assert service.resolve_slice_target("generic-video", "pool_1").name == "pool_1"


def test_create_library_is_idempotent_but_other_request_conflicts(tmp_path):
    service = LibraryService(ConfigStore(tmp_path / "config"))
    request = create_request(tmp_path)

    first = service.create(request)
    repeated = service.create(request)

    assert repeated["library_id"] == first["library_id"]
    assert repeated["idempotent"] is True
    with pytest.raises(LibraryConflictError, match="同名"):
        service.create(create_request(tmp_path, "different-request"))


def test_add_benefit_creates_directory_and_persists_config(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    created = service.create(create_request(tmp_path))
    root = Path(created["root_path"])

    result = service.add_benefit(
        "summer-sale",
        AddBenefitRequest(
            client_request_id="benefit-request-1",
            current_config_hash=store.content_hash("summer-sale"),
        ),
    )

    assert result["category"] == "benefit_2"
    assert (root / "切片素材/利益点/2").is_dir()
    config = store.load("summer-sale")
    assert config.sources["benefit_2"].directory == "切片素材/利益点/2"
    assert config.timeline == [
        "pre_roll",
        "hook",
        "benefit_1",
        "benefit_2",
        "ending",
        "end_card",
    ]
    marker = json.loads((root / MARKER_PATH).read_text(encoding="utf-8"))
    assert marker["next_benefit_number"] == 3

    repeated = service.add_benefit(
        "summer-sale",
        AddBenefitRequest(
            client_request_id="benefit-request-1",
            current_config_hash="0" * 64,
        ),
    )
    assert repeated["category"] == "benefit_2"
    assert repeated["idempotent"] is True


def test_deleted_benefit_number_is_not_reused(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    service.create(create_request(tmp_path))
    service.add_benefit(
        "summer-sale",
        AddBenefitRequest(
            client_request_id="benefit-request-1",
            current_config_hash=store.content_hash("summer-sale"),
        ),
    )
    config = store.load("summer-sale")
    config.timeline.remove("benefit_2")
    del config.sources["benefit_2"]
    store.save_config("summer-sale", config)

    result = service.add_benefit(
        "summer-sale",
        AddBenefitRequest(
            client_request_id="benefit-request-2",
            current_config_hash=store.content_hash("summer-sale"),
        ),
    )

    assert result["category"] == "benefit_3"
    assert Path(result["directory"]).is_dir()
    assert (Path(result["directory"]).parent / "2").is_dir()


def test_add_benefit_rejects_stale_hash_without_creating_directory(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    created = service.create(create_request(tmp_path))

    with pytest.raises(LibraryConflictError, match="已被修改"):
        service.add_benefit(
            "summer-sale",
            AddBenefitRequest(
                client_request_id="benefit-request-1",
                current_config_hash="0" * 64,
            ),
        )

    assert not (Path(created["root_path"]) / "切片素材/利益点/2").exists()


def test_add_benefit_rejects_unmanaged_config(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    # Create a standard config first, then remove only the marker to model an external config.
    created = service.create(create_request(tmp_path))
    (Path(created["root_path"]) / MARKER_PATH).unlink()

    with pytest.raises(LibraryError, match="不是 SmartStitch"):
        service.add_benefit(
            "summer-sale",
            AddBenefitRequest(
                client_request_id="benefit-request-1",
                current_config_hash=store.content_hash("summer-sale"),
            ),
        )


def test_slice_target_rejects_symbolic_link(tmp_path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    created = service.create(create_request(tmp_path))
    target = Path(created["root_path"]) / "切片素材/未归类"
    target.rmdir()
    target.symlink_to(Path(created["root_path"]) / "切片素材/引子", target_is_directory=True)

    with pytest.raises(LibraryError, match="符号链接"):
        service.resolve_slice_target("summer-sale", "unclassified")
