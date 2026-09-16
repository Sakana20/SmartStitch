from __future__ import annotations

import json
from pathlib import Path

import pytest

from smartstitch.config import ConfigStore
from smartstitch.library import (
    MARKER_PATH,
    STANDARD_DIRECTORIES,
    LibraryConflictError,
    LibraryError,
    LibraryService,
)
from smartstitch.models import (
    AddBenefitRequest,
    CreateLibraryRequest,
    LibraryPreflightRequest,
    SourceMode,
)


def create_request(parent: Path, request_id: str = "request-12345678") -> CreateLibraryRequest:
    return CreateLibraryRequest(
        new_id="summer-sale",
        new_name="夏日促销",
        parent_directory=str(parent),
        folder_name="夏日 视频库",
        client_request_id=request_id,
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
