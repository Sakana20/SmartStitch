from __future__ import annotations

from conftest import authenticated_client

import base64
import json
import struct
import subprocess
from pathlib import Path
from urllib.parse import quote

import pytest
import yaml
from fastapi.testclient import TestClient

from smartstitch import __version__
import smartstitch.api as api_module
from smartstitch.api import (
    SharedConfigUnavailableError,
    canonical_config_directory,
    create_app,
    resolve_config_directory,
)
from smartstitch.slicer import SliceConflictError


def test_importing_api_does_not_create_application() -> None:
    assert not hasattr(api_module, "app")


def write_config_template(config_directory, tmp_path):
    template = {
        "schema_version": 2,
        "id": "new-config",
        "name": "新配置",
        "source_root": str(tmp_path),
        "timeline": ["hook", "benefit_1", "ending"],
        "sources": {
            category: {"mode": "required", "directory": category}
            for category in ["hook", "benefit_1", "ending"]
        },
        "benefit_overlays": {"mode": "disabled", "file": ""},
        "output": {"directory": str(tmp_path / "output")},
    }
    (config_directory / "template.commented.yaml").write_text(
        yaml.safe_dump(template, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


def acquire_edit_lease(client, config_id, session_id="test-browser-session"):
    acquired = client.post(
        f"/api/v1/configs/{config_id}/lock/acquire",
        json={"browser_session_id": session_id},
    )
    assert acquired.status_code == 200
    payload = acquired.json()
    return payload, {
        "X-SmartStitch-Lease": payload["lease_token"],
        "X-SmartStitch-Config-Hash": payload["content_hash"],
    }


def test_health_and_config_listing(tmp_path):
    (tmp_path / "config").mkdir()
    client = authenticated_client(create_app(tmp_path))
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["config_directory"] == str(tmp_path / "config")
    assert response.json()["shared_config"] is False
    assert client.get("/api/v1/configs").json() == []
    updates = client.get("/api/v1/system/updates")
    assert updates.status_code == 200
    assert updates.json()["status"] == "nas_unavailable"
    assert "请连接 NAS" in updates.json()["message"]


def test_batch_dedup_settings_are_independent_of_project_configs(tmp_path):
    (tmp_path / "config").mkdir()
    source = tmp_path / "videos"
    source.mkdir()
    client = authenticated_client(create_app(tmp_path))

    initial = client.get("/api/v1/tools/batch-dedup/settings")
    assert initial.status_code == 200
    settings = initial.json()
    assert settings["enabled"] is True
    settings["effect_layers"][0]["opacity_percent"] = 43
    saved = client.put("/api/v1/tools/batch-dedup/settings", json=settings)
    assert saved.status_code == 200
    assert client.get("/api/v1/tools/batch-dedup/settings").json()["effect_layers"][0]["opacity_percent"] == 43

    submitted = client.post("/api/v1/tools/batch-dedup", json={
        "source_directory": str(source), "visual_dedup": settings,
    })
    assert submitted.status_code == 422
    assert "没有可用视频" in submitted.json()["detail"]


def test_update_endpoints_use_application_release_checker(tmp_path):
    class FakeReleaseChecker:
        def check(self, current_version):
            return {
                "current_version": current_version,
                "latest_version": "9.0.0",
                "update_available": True,
            }

        def open_latest_release(self):
            return {"opened": True, "version": "9.0.0"}

    app = create_app(tmp_path)
    app.state.release_checker = FakeReleaseChecker()
    client = authenticated_client(app)

    update = client.get("/api/v1/system/updates")
    opened = client.post("/api/v1/system/updates/open")

    assert update.status_code == 200
    assert update.json()["current_version"] == __version__
    assert update.json()["update_available"] is True
    assert opened.json()["opened"] is True


def test_config_directory_prefers_mounted_shared_root_and_supports_override(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("SMARTSTITCH_CONFIG_DIRECTORY", raising=False)
    application_root = tmp_path / "application"
    shared_root = tmp_path / "nas" / "Smartstitch"
    shared_root.mkdir(parents=True)

    assert resolve_config_directory(
        application_root,
        allow_shared_default=True,
        shared_directory=shared_root,
    ) == shared_root.resolve()
    assert resolve_config_directory(
        application_root,
        allow_shared_default=False,
        shared_directory=shared_root,
    ) == application_root / "config"

    missing_canonical = tmp_path / "volumes" / "home" / "Smartstitch"
    homes_root = tmp_path / "volumes" / "homes"
    alternate = homes_root / "nas-user" / "Smartstitch"
    alternate.mkdir(parents=True)
    assert resolve_config_directory(
        application_root,
        allow_shared_default=True,
        shared_directory=missing_canonical,
        alternate_shared_parent=homes_root,
    ) == alternate.resolve()
    assert canonical_config_directory(
        Path("/Volumes/homes/yiranmobi/Smartstitch")
    ) == Path("/Volumes/home/Smartstitch")

    alternate.rmdir()
    with pytest.raises(SharedConfigUnavailableError, match="未连接 NAS"):
        resolve_config_directory(
            application_root,
            allow_shared_default=True,
            shared_directory=missing_canonical,
            alternate_shared_parent=homes_root,
        )

    override = tmp_path / "custom-configs"
    monkeypatch.setenv("SMARTSTITCH_CONFIG_DIRECTORY", str(override))
    assert resolve_config_directory(
        application_root,
        allow_shared_default=False,
        shared_directory=Path("/missing"),
    ) == override.resolve()


def test_structured_config_update(tmp_path):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    config = {
        "id": "visual-test",
        "name": "可视化测试",
        "source_root": str(tmp_path),
        "timeline": ["hook", "benefit_video", "ending"],
        "sources": {
            category: {"mode": "required", "directory": category}
            for category in ["hook", "benefit_video", "ending"]
        },
        "benefit_overlays": {"mode": "disabled", "directory": "overlay"},
        "output": {"directory": str(tmp_path / "output")},
    }
    (config_directory / "visual-test.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    client = authenticated_client(create_app(tmp_path))
    loaded_response = client.get("/api/v1/configs/visual-test").json()
    loaded = loaded_response["config"]
    assert loaded["schema_version"] == 2
    assert loaded["timeline"] == ["hook", "benefit_1", "ending"]
    assert "benefit_1" in loaded["sources"]
    assert "benefit_video" not in loaded["sources"]
    loaded["name"] = "网页修改后的配置"
    loaded["output"]["fps"] = 25
    _lease, headers = acquire_edit_lease(client, "visual-test")
    response = client.put(
        "/api/v1/configs/visual-test/structured",
        json={"config": loaded},
        headers=headers,
    )
    assert response.status_code == 200
    saved = client.get("/api/v1/configs/visual-test").json()["config"]
    assert saved["name"] == "网页修改后的配置"
    assert saved["output"]["fps"] == 25
    saved_yaml = yaml.safe_load((config_directory / "visual-test.yaml").read_text("utf-8"))
    assert saved_yaml["schema_version"] == 2
    assert saved_yaml["timeline"] == ["hook", "benefit_1", "ending"]
    assert list((config_directory / "backups").glob("*.yaml"))


def test_source_inventory_api_includes_disabled_libraries(tmp_path):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    source_root = tmp_path / "library"
    active = source_root / "active"
    disabled = source_root / "disabled"
    active.mkdir(parents=True)
    disabled.mkdir()
    (active / "one.mp4").write_bytes(b"active")
    (disabled / "one.mp4").write_bytes(b"disabled-1")
    (disabled / "two.mp4").write_bytes(b"disabled-2")
    config = {
        "schema_version": 3,
        "workflow_type": "generic",
        "id": "inventory-test",
        "name": "素材库概览测试",
        "source_root": str(source_root),
        "timeline": ["pool_1", "pool_2", "pool_3"],
        "sources": {
            "pool_1": {"label": "启用库", "mode": "required", "directory": "active"},
            "pool_2": {"label": "关闭库", "mode": "disabled", "directory": "disabled"},
            "pool_3": {"label": "掉线库", "mode": "disabled", "directory": "missing"},
        },
        "benefit_overlays": {"mode": "disabled", "file": ""},
        "output": {"directory": str(tmp_path / "output")},
    }
    (config_directory / "inventory-test.yaml").write_text(
        yaml.safe_dump(config, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )
    client = authenticated_client(create_app(tmp_path))

    response = client.get("/api/v1/configs/inventory-test/source-inventory")

    assert response.status_code == 200
    sources = response.json()["sources"]
    assert sources["pool_1"]["discovered_count"] == 1
    assert sources["pool_1"]["enabled"] is True
    assert sources["pool_2"]["discovered_count"] == 2
    assert sources["pool_2"]["enabled"] is False
    assert sources["pool_2"]["directory_status"] == "available"
    assert sources["pool_3"]["directory_status"] == "unavailable"


def test_create_and_delete_config_with_recoverable_backup(tmp_path):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    write_config_template(config_directory, tmp_path)
    client = authenticated_client(create_app(tmp_path))

    response = client.post(
        "/api/v1/configs", json={"new_id": "summer-sale", "new_name": "夏日促销"}
    )
    assert response.status_code == 200
    created = response.json()["config"]
    assert created["id"] == "summer-sale"
    assert created["name"] == "夏日促销"
    assert (config_directory / "summer-sale.yaml").exists()

    duplicate = client.post(
        "/api/v1/configs", json={"new_id": "summer-sale", "new_name": "重复"}
    )
    assert duplicate.status_code == 422

    _lease, headers = acquire_edit_lease(client, "summer-sale")
    response = client.delete("/api/v1/configs/summer-sale", headers=headers)
    assert response.status_code == 200
    assert not (config_directory / "summer-sale.yaml").exists()
    backups = list((config_directory / "backups").glob("summer-sale-*.deleted.yaml"))
    assert len(backups) == 1
    assert yaml.safe_load(backups[0].read_text(encoding="utf-8"))["name"] == "夏日促销"


def test_create_managed_library_and_add_benefit(tmp_path):
    (tmp_path / "config").mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    client = authenticated_client(create_app(tmp_path))

    preflight = client.post(
        "/api/v1/libraries/preflight",
        json={"parent_directory": str(storage), "folder_name": "商品 视频库"},
    )
    assert preflight.status_code == 200
    assert preflight.json()["root_path"] == str(storage / "商品 视频库")

    response = client.post(
        "/api/v1/libraries",
        json={
            "new_id": "product-library",
            "new_name": "商品库",
            "parent_directory": str(storage),
            "folder_name": "商品 视频库",
            "client_request_id": "library-request-1",
        },
    )
    assert response.status_code == 200
    created = response.json()
    assert created["config"]["source_root"] == str(storage / "商品 视频库")

    inspected = client.get("/api/v1/libraries/by-config/product-library")
    assert inspected.status_code == 200
    assert inspected.json()["managed"] is True
    assert inspected.json()["health"] == "healthy"

    targets = client.get(
        "/api/v1/libraries/by-config/product-library/slice-targets"
    )
    assert targets.status_code == 200
    assert {item["category"] for item in targets.json()["targets"]} == {
        "unclassified",
        "pre_roll",
        "hook",
        "benefit_1",
        "ending",
        "end_card",
    }

    _lease, headers = acquire_edit_lease(client, "product-library")
    benefit = client.post(
        "/api/v1/configs/product-library/benefits",
        json={
            "client_request_id": "benefit-request-1",
            "current_config_hash": created["content_hash"],
        },
        headers=headers,
    )
    assert benefit.status_code == 200
    payload = benefit.json()
    assert payload["category"] == "benefit_2"
    assert (storage / "商品 视频库" / "切片素材" / "利益点" / "2").is_dir()


def test_generic_library_pool_api_supports_crud_and_reorder(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    client = authenticated_client(create_app(tmp_path))
    created = client.post(
        "/api/v1/libraries",
        json={
            "new_id": "generic-library",
            "new_name": "通用项目",
            "parent_directory": str(storage),
            "folder_name": "通用项目库",
            "workflow_type": "generic",
            "client_request_id": "generic-library-request",
        },
    ).json()

    _lease, headers = acquire_edit_lease(client, "generic-library")
    first = client.post(
        "/api/v1/configs/generic-library/pools",
        json={
            "label": "开场",
            "client_request_id": "pool-api-request-1",
            "current_config_hash": created["content_hash"],
        },
        headers=headers,
    )
    assert first.status_code == 200
    second = client.post(
        "/api/v1/configs/generic-library/pools",
        json={
            "label": "展示",
            "client_request_id": "pool-api-request-2",
            "current_config_hash": first.json()["content_hash"],
        },
        headers=headers,
    )
    assert second.status_code == 200

    opened_directories = []

    def fake_open_directory(path):
        opened_directories.append(path)
        return {"ok": True, "path": str(path), "manager": "Finder"}

    monkeypatch.setattr(
        "smartstitch.library.open_directory_in_file_manager", fake_open_directory
    )
    opened = client.post(
        "/api/v1/configs/generic-library/sources/pool_2/open-directory"
    )
    assert opened.status_code == 200
    assert opened.json()["folder_name"] == "pool_2"
    assert opened_directories == [storage / "通用项目库" / "视频库" / "pool_2"]

    renamed = client.patch(
        "/api/v1/configs/generic-library/pools/pool_2",
        json={
            "label": "产品展示",
            "description": "主体画面",
            "mode": "optional",
            "default_weight": 2,
            "current_config_hash": second.json()["content_hash"],
        },
        headers=headers,
    )
    assert renamed.status_code == 200
    assert renamed.json()["config"]["sources"]["pool_2"]["label"] == "产品展示"

    reordered = client.put(
        "/api/v1/configs/generic-library/timeline",
        json={
            "timeline": ["pool_2", "pool_1"],
            "current_config_hash": renamed.json()["content_hash"],
        },
        headers=headers,
    )
    assert reordered.status_code == 200
    assert reordered.json()["timeline"] == ["pool_2", "pool_1"]

    deleted = client.request(
        "DELETE",
        "/api/v1/configs/generic-library/pools/pool_1",
        json={"current_config_hash": reordered.json()["content_hash"]},
        headers=headers,
    )
    assert deleted.status_code == 200
    assert (storage / "通用项目库" / "视频库" / "pool_1").is_dir()


def test_managed_library_can_replace_overlay_image(tmp_path):
    (tmp_path / "config").mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    client = authenticated_client(create_app(tmp_path))
    created = client.post(
        "/api/v1/libraries",
        json={
            "new_id": "overlay-library",
            "new_name": "风险图项目",
            "parent_directory": str(storage),
            "folder_name": "风险图项目库",
            "workflow_type": "generic",
            "client_request_id": "overlay-library-request",
        },
    ).json()
    image = tmp_path / "风险提示.png"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=yellow:s=80x80",
            "-frames:v", "1", str(image),
        ],
        check=True,
    )

    overlay_directory = storage / "风险图项目库" / "风险提示语图片"
    assert client.get("/api/v1/configs/overlay-library/overlay-image/status").json()["assets"] == []
    (overlay_directory / image.name).write_bytes(image.read_bytes())
    discovered = client.get("/api/v1/configs/overlay-library/overlay-image/status")
    assert discovered.status_code == 200
    assert [asset["name"] for asset in discovered.json()["assets"]] == [image.name]
    assert discovered.json()["assets"][0]["valid"]
    (overlay_directory / image.name).unlink()

    _lease, headers = acquire_edit_lease(client, "overlay-library")
    response = client.post(
        "/api/v1/configs/overlay-library/overlay-image",
        json={
            "filename": image.name,
            "data_base64": base64.b64encode(image.read_bytes()).decode("ascii"),
            "current_config_hash": created["content_hash"],
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["filename"] == image.name
    assert (storage / "风险图项目库" / "风险提示语图片" / image.name).is_file()
    assert payload["config"]["benefit_overlays"]["mode"] == "required"

    replacement = tmp_path / "新版提示.png"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=blue:s=80x80",
            "-frames:v", "1", str(replacement),
        ],
        check=True,
    )
    replaced = client.post(
        "/api/v1/configs/overlay-library/overlay-image",
        json={
            "filename": replacement.name,
            "data_base64": base64.b64encode(replacement.read_bytes()).decode("ascii"),
            "current_config_hash": payload["content_hash"],
        },
        headers=headers,
    )
    assert replaced.status_code == 200
    assert len(replaced.json()["backups"]) == 1
    assert not (storage / "风险图项目库" / "风险提示语图片" / image.name).exists()
    assert (storage / "风险图项目库" / "风险提示语图片" / replacement.name).is_file()


def test_managed_library_streams_and_replaces_visual_border(tmp_path):
    (tmp_path / "config").mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    client = authenticated_client(create_app(tmp_path))
    created = client.post(
        "/api/v1/libraries",
        json={
            "new_id": "visual-border-library",
            "new_name": "视觉边框项目",
            "parent_directory": str(storage),
            "folder_name": "视觉边框项目库",
            "workflow_type": "generic",
            "client_request_id": "visual-border-request",
        },
    ).json()
    border = tmp_path / "动态边框.mov"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            (
                "color=c=black@0.0:s=720x1280:r=2:d=0.5,format=argb,"
                "drawbox=x=0:y=0:w=iw:h=40:color=red@1:t=fill:replace=1"
            ),
            "-c:v", "qtrle", "-pix_fmt", "argb", str(border),
        ],
        check=True,
    )

    _lease, headers = acquire_edit_lease(client, "visual-border-library")
    response = client.post(
        "/api/v1/configs/visual-border-library/visual-border",
        content=border.read_bytes(),
        headers={
            **headers,
            "X-SmartStitch-Filename": quote(border.name),
            "X-SmartStitch-Config-Hash": created["content_hash"],
            "Content-Type": "application/octet-stream",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    target = storage / "视觉边框项目库" / "视觉去重边框" / border.name
    assert target.is_file()
    assert payload["config"]["visual_dedup"]["enabled"] is True
    assert payload["config"]["visual_dedup"]["background"]["enabled"] is True
    assert payload["config"]["visual_dedup"]["border_overlay"]["mode"] == "required"


def test_global_visual_border_api_streams_into_shared_config_root(tmp_path):
    (tmp_path / "config").mkdir()
    client = authenticated_client(create_app(tmp_path))
    initial = client.get("/api/v1/global-assets/visual-borders").json()
    border = tmp_path / "全局动态边框.mov"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            (
                "color=c=black@0.0:s=180x320:r=2:d=0.5,format=argb,"
                "drawbox=x=0:y=0:w=iw:h=20:color=red@1:t=fill:replace=1"
            ),
            "-c:v", "qtrle", "-pix_fmt", "argb", str(border),
        ],
        check=True,
    )

    response = client.post(
        "/api/v1/global-assets/visual-borders",
        content=border.read_bytes(),
        headers={
            "X-SmartStitch-Filename": quote(border.name),
            "X-SmartStitch-Library-Revision": str(initial["revision"]),
            "Content-Type": "application/octet-stream",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["revision"] == 1
    assert payload["asset"]["display_name"] == border.name
    assert (tmp_path / "config" / payload["asset"]["storage_path"]).is_file()
    assert Path(payload["directory"]) == (
        tmp_path / "config" / "全局素材库" / "视觉特效" / "effect_1"
    )

    conflict = client.patch(
        f"/api/v1/global-assets/visual-borders/{payload['asset']['asset_id']}",
        json={"library_revision": 0, "enabled": False},
    )
    assert conflict.status_code == 409

    updated = client.patch(
        f"/api/v1/global-assets/visual-borders/{payload['asset']['asset_id']}",
        json={"library_revision": 1, "default_weight": 2.5},
    )
    assert updated.status_code == 200
    assert updated.json()["asset"]["default_weight"] == 2.5
    assert updated.json()["revision"] == 2


def test_global_visual_effect_library_api_manages_groups_and_assets(
    tmp_path, monkeypatch
):
    (tmp_path / "config").mkdir()
    client = authenticated_client(create_app(tmp_path))
    initial = client.get("/api/v1/global-assets/visual-effect-libraries").json()

    created = client.post(
        "/api/v1/global-assets/visual-effect-libraries",
        json={"name": "烟花", "library_revision": initial["revision"]},
    )
    assert created.status_code == 200
    created_payload = created.json()
    library_id = created_payload["library"]["library_id"]
    assert library_id == "effect_2"

    renamed = client.patch(
        f"/api/v1/global-assets/visual-effect-libraries/{library_id}",
        json={
            "name": "节庆烟花",
            "library_revision": created_payload["revision"],
        },
    )
    assert renamed.status_code == 200
    created_payload = renamed.json()
    assert created_payload["library"]["name"] == "节庆烟花"
    renamed_library = next(
        item
        for item in created_payload["libraries"]
        if item["library_id"] == library_id
    )
    assert Path(renamed_library["directory"]).name == library_id

    monkeypatch.setattr(
        "smartstitch.api.open_directory_in_file_manager",
        lambda path: {"ok": True, "path": str(path), "manager": "Finder"},
    )
    opened = client.post(
        f"/api/v1/global-assets/visual-effect-libraries/{library_id}/open-directory"
    )
    assert opened.status_code == 200
    assert opened.json()["folder_name"] == "effect_2"

    effect = tmp_path / "烟花.mov"
    subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i",
            (
                "color=c=black@0.0:s=180x320:r=2:d=0.5,format=argb,"
                "drawbox=x=20:y=20:w=40:h=40:color=yellow@1:t=fill:replace=1"
            ),
            "-c:v", "qtrle", "-pix_fmt", "argb", str(effect),
        ],
        check=True,
    )
    uploaded = client.post(
        f"/api/v1/global-assets/visual-effect-libraries/{library_id}/assets",
        content=effect.read_bytes(),
        headers={
            "X-SmartStitch-Filename": quote(effect.name),
            "X-SmartStitch-Library-Revision": str(created_payload["revision"]),
            "Content-Type": "application/octet-stream",
        },
    )
    assert uploaded.status_code == 200
    uploaded_payload = uploaded.json()
    asset = uploaded_payload["asset"]
    assert asset["display_name"] == effect.name
    assert (tmp_path / "config" / asset["storage_path"]).is_file()

    deleted_asset = client.delete(
        f"/api/v1/global-assets/visual-effect-libraries/{library_id}/assets/{asset['asset_id']}",
        headers={
            "X-SmartStitch-Library-Revision": str(uploaded_payload["revision"])
        },
    )
    assert deleted_asset.status_code == 200
    deleted_library = client.delete(
        f"/api/v1/global-assets/visual-effect-libraries/{library_id}",
        headers={
            "X-SmartStitch-Library-Revision": str(
                deleted_asset.json()["revision"]
            )
        },
    )
    assert deleted_library.status_code == 200
    assert all(
        library["library_id"] != library_id
        for library in deleted_library.json()["libraries"]
    )


def test_directory_picker_api_returns_selected_path(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(
        "smartstitch.api.pick_directory",
        lambda: {"cancelled": False, "path": str(tmp_path)},
    )
    client = authenticated_client(create_app(tmp_path))

    response = client.post("/api/v1/system/directory-picker")

    assert response.status_code == 200
    assert response.json() == {"cancelled": False, "path": str(tmp_path)}


def test_timeline_sources_lists_supported_videos_in_natural_order(tmp_path):
    (tmp_path / "config").mkdir()
    source_directory = tmp_path / "源视频"
    source_directory.mkdir()
    for name in ["视频10.mp4", "视频2.MOV", "视频1.mkv", "说明.txt", ".隐藏.mp4"]:
        (source_directory / name).write_bytes(b"video")
    (source_directory / "子目录").mkdir()
    (source_directory / "子目录" / "嵌套.mp4").write_bytes(b"video")

    response = authenticated_client(create_app(tmp_path)).post(
        "/api/v1/timeline/sources",
        json={"source_directory": f"'{source_directory}'"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["source_directory"] == str(source_directory)
    assert payload["count"] == 3
    assert [video["name"] for video in payload["videos"]] == [
        "视频1.mkv",
        "视频2.MOV",
        "视频10.mp4",
    ]
    assert all(video["path"].startswith(str(source_directory)) for video in payload["videos"])


def test_timeline_sources_rejects_a_file_path(tmp_path):
    (tmp_path / "config").mkdir()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"video")

    response = authenticated_client(create_app(tmp_path)).post(
        "/api/v1/timeline/sources", json={"source_directory": str(source)}
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "源视频路径必须是文件夹"


def test_timeline_slice_conflict_returns_409(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)

    def reject_stale_review(_request):
        raise SliceConflictError("断点审核已变更")

    monkeypatch.setattr(app.state.timeline_slicer, "export", reject_stale_review)
    response = authenticated_client(app).post(
        "/api/v1/timeline/slices",
        json={
            "analysis_id": "a" * 24,
            "config_id": "example",
            "review_revision": "b" * 64,
            "current_config_hash": "c" * 64,
            "assignments": [{"segment_index": 1, "category": "hook"}],
            "client_request_id": "slice-request-conflict",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "断点审核已变更"


def test_timeline_waveform_returns_downsampled_visible_range(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    analyzer = app.state.timeline_analyzer
    analysis_id = "d" * 24
    record = {
        "analysis_id": analysis_id,
        "fps": 10,
        "frame_count": 100,
        "audio": {
            "waveform_status": "ready",
            "buckets_per_second": 400,
            "bucket_count": 4,
        },
    }
    (analyzer.data_directory / f"{analysis_id}.json").write_text(
        json.dumps(record), encoding="utf-8"
    )
    (analyzer.waveform_directory / f"{analysis_id}.wfm").write_bytes(
        b"".join(
            struct.pack("<hh", -value, value) for value in [100, 200, 300, 400]
        )
    )

    response = authenticated_client(app).get(
        f"/api/v1/timeline/waveforms/{analysis_id}",
        params={"start_frame": 0, "end_frame": 10, "width_px": 2},
    )

    assert response.status_code == 200
    assert response.json()["bucket_count"] == 2
    assert response.json()["peaks"][-1] == [
        round(-400 / 32768, 5),
        round(400 / 32768, 5),
    ]


def test_delete_job_record_keeps_active_jobs_protected(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    client = authenticated_client(app)
    manager = app.state.job_manager
    manager.database.save({"id": "finished-job", "status": "completed"})
    manager.database.save({"id": "running-job", "status": "running"})

    response = client.delete("/api/v1/jobs/finished-job")
    assert response.status_code == 200
    assert manager.database.get("finished-job") is None

    response = client.delete("/api/v1/jobs/running-job")
    assert response.status_code == 409
    assert manager.database.get("running-job") is not None

    response = client.delete("/api/v1/jobs")
    assert response.status_code == 409

    manager.database.save({"id": "running-job", "status": "completed"})
    manager.database.save({"id": "another-finished-job", "status": "failed"})
    response = client.delete("/api/v1/jobs")
    assert response.status_code == 200
    assert response.json()["deleted_count"] == 2
    assert manager.database.list() == []
