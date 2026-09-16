from __future__ import annotations

import json
import struct

import yaml
from fastapi.testclient import TestClient

from smartstitch.api import create_app
from smartstitch.slicer import SliceConflictError


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


def test_health_and_config_listing(tmp_path):
    (tmp_path / "config").mkdir()
    client = TestClient(create_app(tmp_path))
    response = client.get("/api/v1/system/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert client.get("/api/v1/configs").json() == []


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
    client = TestClient(create_app(tmp_path))
    loaded = client.get("/api/v1/configs/visual-test").json()["config"]
    assert loaded["schema_version"] == 2
    assert loaded["timeline"] == ["hook", "benefit_1", "ending"]
    assert "benefit_1" in loaded["sources"]
    assert "benefit_video" not in loaded["sources"]
    loaded["name"] = "网页修改后的配置"
    loaded["output"]["fps"] = 25
    response = client.put(
        "/api/v1/configs/visual-test/structured", json={"config": loaded}
    )
    assert response.status_code == 200
    saved = client.get("/api/v1/configs/visual-test").json()["config"]
    assert saved["name"] == "网页修改后的配置"
    assert saved["output"]["fps"] == 25
    saved_yaml = yaml.safe_load((config_directory / "visual-test.yaml").read_text("utf-8"))
    assert saved_yaml["schema_version"] == 2
    assert saved_yaml["timeline"] == ["hook", "benefit_1", "ending"]
    assert list((config_directory / "backups").glob("*.yaml"))


def test_create_and_delete_config_with_recoverable_backup(tmp_path):
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    write_config_template(config_directory, tmp_path)
    client = TestClient(create_app(tmp_path))

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

    response = client.delete("/api/v1/configs/summer-sale")
    assert response.status_code == 200
    assert not (config_directory / "summer-sale.yaml").exists()
    backups = list((config_directory / "backups").glob("summer-sale-*.deleted.yaml"))
    assert len(backups) == 1
    assert yaml.safe_load(backups[0].read_text(encoding="utf-8"))["name"] == "夏日促销"


def test_create_managed_library_and_add_benefit(tmp_path):
    (tmp_path / "config").mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    client = TestClient(create_app(tmp_path))

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

    benefit = client.post(
        "/api/v1/configs/product-library/benefits",
        json={
            "client_request_id": "benefit-request-1",
            "current_config_hash": created["content_hash"],
        },
    )
    assert benefit.status_code == 200
    payload = benefit.json()
    assert payload["category"] == "benefit_2"
    assert (storage / "商品 视频库" / "切片素材" / "利益点" / "2").is_dir()


def test_directory_picker_api_returns_selected_path(tmp_path, monkeypatch):
    (tmp_path / "config").mkdir()
    monkeypatch.setattr(
        "smartstitch.api.pick_directory",
        lambda: {"cancelled": False, "path": str(tmp_path)},
    )
    client = TestClient(create_app(tmp_path))

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

    response = TestClient(create_app(tmp_path)).post(
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

    response = TestClient(create_app(tmp_path)).post(
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
    response = TestClient(app).post(
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

    response = TestClient(app).get(
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
    client = TestClient(app)
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
