from __future__ import annotations

import threading
import subprocess
from pathlib import Path

import pytest
from PIL import Image
from conftest import authenticated_client

from smartstitch.api import create_app
from smartstitch.config import ConfigStore
from smartstitch.library import LibraryError, LibraryService
from smartstitch.models import AddPoolRequest, AppConfig, CreateLibraryRequest, WeightUpdate
from smartstitch.naming import category_is_naming_source
from smartstitch.planner import build_plan
from smartstitch.renderer import render_item
from smartstitch.scanner import scan_config


def make_image_project(tmp_path: Path):
    store = ConfigStore(tmp_path / "config")
    service = LibraryService(store)
    service.create(CreateLibraryRequest(
        parent_directory=str(tmp_path), folder_name="图片项目",
        workflow_type="generic", new_id="image-project", new_name="图片项目",
        client_request_id="image-project-create",
    ))
    service.add_pool("image-project", AddPoolRequest(
        label="价格图", media_type="image", image_duration_seconds=2.5,
        client_request_id="image-pool-create",
        current_config_hash=store.content_hash("image-project"),
    ))
    config = store.load("image-project")
    config.output.width = 180
    config.output.height = 320
    config.output.fps = 20
    config.output.video_codec = "libx264"
    store.save_config(config.id, config)
    directory = Path(config.source_root) / "视频库/pool_1"
    Image.new("RGB", (90, 160), "red").save(directory / "a.png")
    Image.new("RGB", (90, 160), "blue").save(directory / "b.png")
    return store, service, directory


def test_image_pool_duration_override_and_pure_image_render(tmp_path):
    store, service, directory = make_image_project(tmp_path)
    config = store.load("image-project")
    assert config.sources["pool_1"].media_type == "image"
    assert service.slice_targets(config.id)[0]["category"] == "unclassified"
    assert len(service.slice_targets(config.id)) == 1
    with pytest.raises(LibraryError, match="图片库不能"):
        service.resolve_slice_target(config.id, "pool_1")

    store.update_weights(config.id, [
        WeightUpdate(category="pool_1", path=str(directory / "a.png"), enabled=True,
                     weight=1, image_duration_seconds=4),
        WeightUpdate(category="pool_1", path=str(directory / "b.png"), enabled=True,
                     weight=1),
    ])
    config = store.load(config.id)
    scan = scan_config(config)
    assert not scan.errors
    by_name = {asset.name: asset for asset in scan.assets["pool_1"]}
    assert by_name["a.png"].probe.duration == 4
    assert by_name["b.png"].probe.duration == 2.5
    assert by_name["a.png"].image_duration_seconds == 4
    plan = build_plan(config, scan, 2, seed=17)
    assert {item.selections["pool_1"].name for item in plan.items} == {"a.png", "b.png"}
    short_item = next(item for item in plan.items if item.selections["pool_1"].name == "b.png")
    result = render_item(config, short_item, tmp_path / "render.mp4", threading.Event())
    assert abs(result["actual_duration"] - 2.5) < 0.15

    store.update_weights(config.id, [
        WeightUpdate(category="pool_1", path=str(directory / "a.png"), enabled=True,
                     weight=1, image_duration_seconds=4),
        WeightUpdate(category="pool_1", path=str(directory / "b.png"), enabled=True,
                     weight=1),
    ])
    changed = store.load(config.id)
    changed.sources["pool_1"].image_duration_seconds = 3.5
    store.save_config(changed.id, changed)
    rescanned = scan_config(store.load(config.id))
    by_name = {asset.name: asset for asset in rescanned.assets["pool_1"]}
    assert by_name["a.png"].probe.duration == 4
    assert by_name["b.png"].probe.duration == 3.5


def test_old_generic_defaults_to_video_and_rejects_image_extensions(tmp_path):
    store, _service, _directory = make_image_project(tmp_path)
    config = store.load("image-project")
    data = config.model_dump(mode="json")
    data["sources"]["pool_1"].pop("media_type")
    data["sources"]["pool_1"]["extensions"] = [".mp4"]
    assert AppConfig.model_validate(data).sources["pool_1"].media_type == "video"
    data["sources"]["pool_1"]["extensions"] = [".png"]
    with pytest.raises(ValueError, match="视频库不能使用图片扩展名"):
        AppConfig.model_validate(data)


def test_transparent_image_uses_output_background_color(tmp_path):
    store, _service, directory = make_image_project(tmp_path)
    transparent = Image.new("RGBA", (90, 160), (0, 0, 0, 0))
    transparent.save(directory / "transparent.png")
    config = store.load("image-project")
    config.output.background_color = "white"
    store.save_config(config.id, config)
    store.update_weights(config.id, [
        WeightUpdate(category="pool_1", path=str(directory / "a.png"),
                     enabled=False, weight=0),
        WeightUpdate(category="pool_1", path=str(directory / "b.png"),
                     enabled=False, weight=0),
        WeightUpdate(category="pool_1", path=str(directory / "transparent.png"),
                     enabled=True, weight=1),
    ])
    config = store.load(config.id)
    scan = scan_config(config)
    plan = build_plan(config, scan, 1, seed=3)
    output = tmp_path / "transparent.mp4"
    render_item(config, plan.items[0], output, threading.Event())
    frame = tmp_path / "frame.png"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-i", str(output),
        "-frames:v", "1", str(frame),
    ], check=True)
    r, g, b = Image.open(frame).convert("RGB").getpixel((90, 160))
    assert min(r, g, b) > 240


def test_image_preview_only_serves_files_inside_image_pool(tmp_path):
    store, _service, directory = make_image_project(tmp_path)
    client = authenticated_client(create_app(tmp_path, config_directory=store.directory))
    endpoint = "/api/v1/configs/image-project/image-preview"
    inside = client.get(endpoint, params={"category": "pool_1", "path": str(directory / "a.png")})
    assert inside.status_code == 200
    assert inside.headers["content-type"].startswith("image/png")
    outside = client.get(endpoint, params={"category": "pool_1", "path": str(tmp_path / "outside.png")})
    assert outside.status_code == 404


def test_video_then_image_render_and_default_naming_skips_image_pool(tmp_path):
    store, service, directory = make_image_project(tmp_path)
    service.add_pool("image-project", AddPoolRequest(
        label="视频", client_request_id="video-pool-create",
        current_config_hash=store.content_hash("image-project"),
    ))
    config = store.load("image-project")
    video = Path(config.source_root) / "视频库/pool_2/clip.mp4"
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "color=c=green:s=180x320:r=20:d=0.5",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(video),
    ], check=True)
    config.timeline = ["pool_2", "pool_1"]
    config.output.naming.enabled = True
    config.output.naming.product = "商品"
    config.output.naming.benefit = "优惠"
    store.save_config(config.id, config)
    config = store.load(config.id)
    assert category_is_naming_source(config, "pool_2")
    assert not category_is_naming_source(config, "pool_1")
    config.output.naming.enabled = False
    scan = scan_config(config)
    assert not scan.errors
    plan = build_plan(config, scan, 1, seed=12)
    assert list(plan.items[0].selections) == ["pool_2", "pool_1"]
    result = render_item(config, plan.items[0], tmp_path / "mixed.mp4", threading.Event())
    assert result["actual_duration"] > 2.9


def test_short_image_duration_is_quantized_to_output_frames(tmp_path):
    store, _service, directory = make_image_project(tmp_path)
    config = store.load("image-project")
    config.output.fps = 24
    config.sources["pool_1"].image_duration_seconds = 0.1
    store.save_config(config.id, config)
    store.update_weights(config.id, [
        WeightUpdate(category="pool_1", path=str(directory / "a.png"), enabled=True, weight=1),
        WeightUpdate(category="pool_1", path=str(directory / "b.png"), enabled=False, weight=0),
    ])
    config = store.load(config.id)
    scan = scan_config(config)
    assert scan.assets["pool_1"][0].probe.duration == pytest.approx(2 / 24)
    item = build_plan(config, scan, 1, seed=2).items[0]
    result = render_item(config, item, tmp_path / "short.mp4", threading.Event())
    assert result["actual_duration"] == pytest.approx(2 / 24, abs=0.05)
