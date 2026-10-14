from __future__ import annotations

from conftest import authenticated_client

from datetime import date

import subprocess
import time
import csv
import json
from pathlib import Path

import pytest
import yaml
from PIL import Image
from pydantic import ValidationError
from fastapi.testclient import TestClient

from smartstitch.api import create_app
from smartstitch.config import ConfigStore
from smartstitch.folder_concat import inspect_folders, folder_concat_config
from smartstitch.jobs import JobManager
from smartstitch.models import FolderConcatRequest, NamingBlockVariantConfig, OutputNamingConfig
from smartstitch.scanner import probe_media
from smartstitch.codec_settings import CodecSettings


@pytest.fixture
def overlay_inputs(tmp_path):
    a, b, output = tmp_path / "A", tmp_path / "B", tmp_path / "output"
    output.mkdir()
    make_video(a / "a.mp4", "red")
    make_video(b / "b.mp4", "blue")
    image = tmp_path / "风险提示.png"
    Image.new("RGBA", (20, 20), (255, 255, 0, 128)).save(image)
    return {
        "directory_a": str(a), "directory_b": str(b), "output_directory": str(output),
        "benefit_overlays": {"file": str(image)},
    }


def overlay_manager(tmp_path, monkeypatch):
    manager = JobManager(ConfigStore(tmp_path / "config"), tmp_path / "data")
    manager.codec_settings.save(CodecSettings(software_codec_enabled=True))
    monkeypatch.setattr(manager, "start", lambda *_: None)
    return manager


def wait_for_concat(manager, job):
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = manager.get_job(job["id"])
        if job["status"] in {"completed", "partial_failed", "failed", "cancelled"}:
            return job
        time.sleep(0.02)
    pytest.fail(f"拼接未在期限内完成: {job}")


def rendered_frame(path, seconds):
    result = subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-ss", str(seconds),
        "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ], capture_output=True, check=True)
    return Image.frombytes("RGB", (120, 200), result.stdout)


def test_overlay_preview_defaults_to_full_and_rechecks_each_pair(overlay_inputs):
    result = inspect_folders(FolderConcatRequest.model_validate(overlay_inputs))
    overlay = result["benefit_overlays"]
    assert overlay["enabled"] and overlay["timing"]["scope"] == "full"
    assert (overlay["width"], overlay["height"], overlay["format"]) == (20, 20, "PNG")
    assert overlay["output"] == {"width": 120, "height": 200, "fps": 20}
    duration = result["pairs"][0]["estimated_duration"]
    assert result["pairs"][0]["overlay_range"] == {"start_seconds": 0, "end_seconds": duration}
    overlay_inputs["benefit_overlays"]["timing"] = {"scope": "custom", "start_seconds": 0.1, "end_seconds": 5}
    result = inspect_folders(FolderConcatRequest.model_validate(overlay_inputs))
    assert result["pairs"][0]["overlay_range"] == {"start_seconds": 0.1, "end_seconds": duration}
    assert "截到" in result["warnings"][0]
    overlay_inputs["benefit_overlays"]["timing"]["start_seconds"] = duration
    with pytest.raises(ValueError, match="第 1 组 a.mp4 → b.mp4"):
        inspect_folders(FolderConcatRequest.model_validate(overlay_inputs))


@pytest.mark.parametrize("setting", [
    {"mode": "optional"}, {"timing": {"scope": "benefits"}}, {"timing": {"scope": "main"}},
    {"timing": {"scope": "custom", "start_seconds": float("nan")}},
    {"timing": {"scope": "custom", "end_seconds": float("inf")}},
    {"timing": {"scope": "custom", "start_seconds": 2, "end_seconds": 1}},
    {"placement": {"opacity": float("nan")}}, {"placement": {"opacity": 1.1}},
    {"placement": {"shrink_if_oversized": False}},
    {"file": ""},
])
def test_overlay_request_rejects_unsupported_settings(setting):
    with pytest.raises(ValidationError):
        FolderConcatRequest(directory_a="/A", directory_b="/B", benefit_overlays={"file": "/risk.png", **setting})


@pytest.mark.parametrize("invalid", ["missing", "directory", "broken", "fake_format", "animated", "oversized"])
def test_overlay_invalid_images_do_not_create_batch(tmp_path, monkeypatch, overlay_inputs, invalid):
    image = Path(overlay_inputs["benefit_overlays"]["file"])
    if invalid == "missing":
        image.unlink()
    elif invalid == "directory":
        image.unlink()
        image.mkdir()
    elif invalid == "broken":
        image.write_bytes(b"not an image")
    elif invalid == "fake_format":
        Image.new("RGB", (20, 20), "red").save(image, format="JPEG")
    elif invalid == "animated":
        Image.new("RGBA", (20, 20), "red").save(image, save_all=True, append_images=[Image.new("RGBA", (20, 20), "blue")], duration=100)
    elif invalid == "oversized":
        with image.open("r+b") as handle:
            handle.truncate(20 * 1024 * 1024 + 1)
    manager = overlay_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="风险提示语"):
        manager.create_folder_concat(FolderConcatRequest.model_validate(overlay_inputs))
    assert not manager.list_jobs()
    assert not list(Path(overlay_inputs["output_directory"]).iterdir())


def test_disabled_overlay_ignores_invalid_image_and_parameters(tmp_path, monkeypatch, overlay_inputs):
    overlay_inputs["benefit_overlays"] = {"mode": "disabled", "file": "/missing.png", "timing": {"scope": "bad"}}
    request = FolderConcatRequest.model_validate(overlay_inputs)
    assert request.benefit_overlays is None
    job = overlay_manager(tmp_path, monkeypatch).create_folder_concat(request)
    assert job["benefit_overlays"] == {"enabled": False}
    assert job["items"][0]["overlay"] is None
    assert not (Path(job["output_directory"]) / "assets").exists()


def test_overlay_api_preflight_and_picker(tmp_path, monkeypatch, overlay_inputs):
    (tmp_path / "config").mkdir()
    client = authenticated_client(create_app(tmp_path))
    preview = client.post("/api/v1/tools/folder-concat/preview", json=overlay_inputs)
    assert preview.status_code == 200, preview.text
    assert preview.json()["benefit_overlays"]["enabled"]
    Path(overlay_inputs["benefit_overlays"]["file"]).unlink()
    for endpoint in ["/preview", ""]:
        response = client.post("/api/v1/tools/folder-concat" + endpoint, json=overlay_inputs)
        assert response.status_code == 422, response.text
    monkeypatch.setattr("smartstitch.api.pick_image_file", lambda: {"cancelled": False, "path": "/risk.png"})
    assert client.post("/api/v1/system/image-file-picker").json()["path"] == "/risk.png"
    monkeypatch.setattr("smartstitch.api.pick_image_file", lambda: {"cancelled": True, "path": None})
    assert client.post("/api/v1/system/image-file-picker").json()["cancelled"] is True


@pytest.mark.parametrize("scope", ["full", "custom"])
def test_overlay_renders_alpha_timing_audio_and_frozen_image(tmp_path, monkeypatch, overlay_inputs, scope):
    # Two pairs verify that deleting the source after creation cannot change this batch.
    make_video(Path(overlay_inputs["directory_a"]) / "c.mp4", "red")
    make_video(Path(overlay_inputs["directory_b"]) / "d.mp4", "blue")
    overlay_inputs["benefit_overlays"].update({
        "placement": {"opacity": 0.5},
        "timing": {"scope": scope, "start_seconds": 0.15, "end_seconds": 0.35 if scope == "custom" else None},
    })
    manager = overlay_manager(tmp_path, monkeypatch)
    job = manager.create_folder_concat(FolderConcatRequest.model_validate(overlay_inputs))
    frozen = Path(job["benefit_overlays"]["snapshot_path"])
    assert frozen.is_file() and frozen.parent.name == "assets"
    assert all(item["overlay"]["path"] == str(frozen) for item in job["items"])
    snapshot = yaml.safe_load(Path(job["config_snapshot_path"]).read_text())
    assert snapshot["benefit_overlays"]["file"] == str(frozen)
    Path(overlay_inputs["benefit_overlays"]["file"]).unlink()
    JobManager.start(manager, job["id"])
    job = wait_for_concat(manager, job)
    assert job["status"] == "completed", [item["error"] for item in job["items"]]
    for item in job["items"]:
        video = Path(item["output_path"])
        probe = probe_media(video)
        assert probe.has_audio and probe.duration == pytest.approx(0.5, abs=0.06)
        for second, active in [(0.1, scope == "full"), (0.2, True), (0.3, True), (0.4, scope == "full")]:
            frame = rendered_frame(video, second)
            pixel, background = frame.getpixel((60, 100)), frame.getpixel((8, 100))
            assert background[0 if second < .25 else 2] > 220  # A red, then B blue.
            assert pixel[1] == pytest.approx(64 if active else 0, abs=15)  # Original Alpha * opacity.
    manifest = json.loads((frozen.parent.parent / "manifest.json").read_text())
    assert manifest["benefit_overlays"]["sha256"] == job["benefit_overlays"]["sha256"]
    with (frozen.parent.parent / "manifest.csv").open(encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 2 and rows[0]["overlay_name"] == "风险提示.png"
    assert float(rows[0]["overlay_start_seconds"]) == (0.15 if scope == "custom" else 0)


def test_overlay_copy_failure_cleans_batch(tmp_path, monkeypatch, overlay_inputs):
    import smartstitch.jobs as jobs
    original = jobs.freeze_folder_overlay
    def replace_source(config, summary, directory):
        Image.new("RGB", (20, 20), "black").save(config.benefit_overlays.file)
        return original(config, summary, directory)
    monkeypatch.setattr(jobs, "freeze_folder_overlay", replace_source)
    manager = overlay_manager(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="发生变化"):
        manager.create_folder_concat(FolderConcatRequest.model_validate(overlay_inputs))
    assert not manager.list_jobs()
    assert not list(Path(overlay_inputs["output_directory"]).iterdir())


def test_overlay_snapshot_tampering_fails_without_unmarked_output(tmp_path, monkeypatch, overlay_inputs):
    manager = overlay_manager(tmp_path, monkeypatch)
    job = manager.create_folder_concat(FolderConcatRequest.model_validate(overlay_inputs))
    Image.new("RGBA", (20, 20), "black").save(job["benefit_overlays"]["snapshot_path"])
    JobManager.start(manager, job["id"])
    job = wait_for_concat(manager, job)
    assert job["status"] == "failed"
    assert "副本已变化" in job["items"][0]["error"]
    assert not Path(job["items"][0]["output_path"]).exists()


@pytest.mark.parametrize("extension", [".png", ".jpg", ".webp", ".bmp"])
def test_overlay_renders_centered_supported_formats(tmp_path, monkeypatch, overlay_inputs, extension):
    image = tmp_path / f"position{extension}"
    Image.new("RGB", (20, 20), "yellow").save(image)
    overlay_inputs["benefit_overlays"] = {"file": str(image), "placement": {"x": "0", "y": "0"}}
    manager = overlay_manager(tmp_path, monkeypatch)
    job = manager.create_folder_concat(FolderConcatRequest.model_validate(overlay_inputs))
    JobManager.start(manager, job["id"])
    job = wait_for_concat(manager, job)
    assert job["status"] == "completed", job["items"][0]["error"]
    frame = rendered_frame(job["items"][0]["output_path"], 0.1)
    assert frame.getpixel((60, 100))[1] > 220
    assert frame.getpixel((1, 100))[1] < 15


@pytest.mark.parametrize("mode,source_size,top_is_yellow", [
    ("original", (240, 400), True), ("fit", (20, 20), False), ("stretch", (20, 20), True),
])
def test_overlay_renders_scaling_and_oversize_protection(tmp_path, monkeypatch, overlay_inputs, mode, source_size, top_is_yellow):
    image = Path(overlay_inputs["benefit_overlays"]["file"])
    Image.new("RGB", source_size, "yellow").save(image)
    overlay_inputs["benefit_overlays"]["placement"] = {"scale_mode": mode, "x": "(W-w)/2", "y": "(H-h)/2"}
    manager = overlay_manager(tmp_path, monkeypatch)
    job = manager.create_folder_concat(FolderConcatRequest.model_validate(overlay_inputs))
    JobManager.start(manager, job["id"])
    job = wait_for_concat(manager, job)
    assert job["status"] == "completed", job["items"][0]["error"]
    frame = rendered_frame(job["items"][0]["output_path"], 0.1)
    assert frame.getpixel((60, 100))[1] > 220
    assert (frame.getpixel((60, 10))[1] > 220) == top_is_yellow


@pytest.mark.parametrize("scope", ["a", "b"])
def test_folder_overlay_a_b_ranges_follow_each_pair_and_exclude_a_boundary(tmp_path, monkeypatch, overlay_inputs, scope):
    make_video(Path(overlay_inputs["directory_a"]) / "c.mp4", "red", duration=0.5)
    make_video(Path(overlay_inputs["directory_b"]) / "d.mp4", "blue")
    overlay_inputs["benefit_overlays"]["timing"] = {"scope": scope}
    request = FolderConcatRequest.model_validate(overlay_inputs)
    preview = inspect_folders(request)
    assert preview["benefit_overlays"]["timing"]["scope"] == scope
    for pair, boundary in zip(preview["pairs"], [0.25, 0.5]):
        assert pair["overlay_range"]["start_seconds"] == (boundary if scope == "b" else 0)
        assert pair["overlay_range"]["end_seconds"] == (boundary if scope == "a" else boundary + .25)
    manager = overlay_manager(tmp_path, monkeypatch)
    job = manager.create_folder_concat(request)
    assert yaml.safe_load(Path(job["config_snapshot_path"]).read_text())["folder_concat_overlay"]["timing"]["scope"] == scope
    JobManager.start(manager, job["id"])
    job = wait_for_concat(manager, job)
    assert job["status"] == "completed", [item["error"] for item in job["items"]]
    for item, boundary in zip(job["items"], [0.25, 0.5]):
        for second in [boundary - .05, boundary, boundary + .05]:
            pixel = rendered_frame(item["output_path"], second).getpixel((60, 100))
            active = second < boundary if scope == "a" else second >= boundary
            assert pixel[1] == pytest.approx(128 if active else 0, abs=15), (scope, second, pixel)


def test_folder_overlay_can_reuse_disabled_config_image_without_modifying_it(tmp_path, monkeypatch, overlay_inputs):
    manager = overlay_manager(tmp_path, monkeypatch)
    config = folder_concat_config(inspect_folders(FolderConcatRequest(
        directory_a=overlay_inputs["directory_a"], directory_b=overlay_inputs["directory_b"],
    )), tmp_path / "output")
    config.id, config.name = "existing-risk", "现有提示语配置"
    config.benefit_overlays.file = overlay_inputs["benefit_overlays"]["file"]
    manager.config_store.save_config(config.id, config)
    config_path = manager.config_store.directory / "existing-risk.yaml"
    before = config_path.read_bytes()
    overlay_inputs["benefit_overlays"] = {"config_id": "existing-risk", "timing": {"scope": "b"}}
    request = FolderConcatRequest.model_validate(overlay_inputs)
    result = inspect_folders(request, config_store=manager.config_store)
    assert result["benefit_overlays"]["source_config_name"] == config.name
    job = manager.create_folder_concat(request)
    assert job["benefit_overlays"]["source_config_id"] == config.id
    assert Path(job["items"][0]["overlay"]["path"]).read_bytes() == Path(config.benefit_overlays.file).read_bytes()
    assert config_path.read_bytes() == before
    Path(config.benefit_overlays.file).unlink()
    with pytest.raises(ValueError, match="图片"):
        manager.create_folder_concat(request)


def test_folder_overlay_lists_explicit_and_managed_config_images(tmp_path, monkeypatch, overlay_inputs):
    manager = overlay_manager(tmp_path, monkeypatch)
    config = folder_concat_config(inspect_folders(FolderConcatRequest(
        directory_a=overlay_inputs["directory_a"], directory_b=overlay_inputs["directory_b"],
    )), tmp_path / "output")
    config.id, config.name = "explicit-risk", "显式图片配置"
    config.benefit_overlays.file = overlay_inputs["benefit_overlays"]["file"]
    manager.config_store.save_config(config.id, config)
    config.id, config.name = "empty-risk", "无图片配置"
    config.benefit_overlays.file = ""
    manager.config_store.save_config(config.id, config)
    client = authenticated_client(create_app(tmp_path))
    response = client.get("/api/v1/tools/folder-concat/overlay-images")
    assert response.status_code == 200, response.text
    choices = {item["config_id"]: item for item in response.json()["items"]}
    assert choices["explicit-risk"]["available"]
    assert not choices["empty-risk"]["available"]
    root = tmp_path / "managed"
    (root / ".smartstitch").mkdir(parents=True)
    images = root / "风险提示语图片"
    images.mkdir()
    Image.new("RGB", (20, 20), "yellow").save(images / "提示.png")
    (root / ".smartstitch" / "library.json").write_text(json.dumps({"paths": {"overlays": "风险提示语图片"}}))
    config.id, config.name, config.source_root = "managed-risk", "受管目录配置", str(root)
    manager.config_store.save_config(config.id, config)
    choices = {item["config_id"]: item for item in client.get("/api/v1/tools/folder-concat/overlay-images").json()["items"]}
    assert choices["managed-risk"]["available"]
    assert choices["managed-risk"]["file"] == str(images / "提示.png")
    overlay_inputs["benefit_overlays"] = {"config_id": "managed-risk"}
    assert client.post("/api/v1/tools/folder-concat/preview", json=overlay_inputs).status_code == 200
    Image.new("RGB", (20, 20), "blue").save(images / "第二张.png")
    choices = {item["config_id"]: item for item in client.get("/api/v1/tools/folder-concat/overlay-images").json()["items"]}
    assert not choices["managed-risk"]["available"]
    assert client.post("/api/v1/tools/folder-concat/preview", json=overlay_inputs).status_code == 422


def test_folder_overlay_requires_exactly_one_source():
    for source in [{}, {"file": "/risk.png", "config_id": "existing-risk"}]:
        with pytest.raises(ValidationError, match="只能选择一种来源"):
            FolderConcatRequest(directory_a="/A", directory_b="/B", benefit_overlays=source)


def make_video(path: Path, color: str, duration: float = 0.25) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", f"color=c={color}:s=120x200:r=20:d={duration}",
        "-f", "lavfi", "-i", f"sine=frequency=440:sample_rate=44100:duration={duration}",
        "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
        "-c:a", "aac", str(path),
    ], check=True)



def builder_naming():
    return OutputNamingConfig.model_validate({
        "enabled": True, "sequence_start": 7,
        "builder": {"enabled": True, "blocks": [
            {"id": "a", "type": "source", "category": "pool_1", "variants": [
                {"sample_name": "a.mp4", "field_count": 1, "field_index": 0},
            ]},
            {"id": "separator", "type": "text", "text": "-"},
            {"id": "b", "type": "source", "category": "pool_2", "variants": [
                {"sample_name": "b.mp4", "field_count": 1, "field_index": 0},
            ]},
            {"id": "date", "type": "date", "date_format": "mmdd"},
            {"id": "sequence", "type": "sequence"},
        ]},
    })

def test_preview_pairs_sorted_videos_without_recursing(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    for path in [a / "z.mp4", a / "a.MOV", a / "nested" / "ignored.mp4", b / "2.mp4"]:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()
    result = inspect_folders(FolderConcatRequest(directory_a=str(a), directory_b=str(b)))
    assert result["files_a"] == ["a.MOV", "z.mp4"]
    assert result["files_b"] == ["2.mp4"]
    assert result["count_a"] == 2
    assert result["pair_count"] == 1
    assert result["unpaired_a"] == 1
    assert [(pair["a_name"], pair["b_name"]) for pair in result["pairs"]] == [("a.MOV", "2.mp4")]
    with pytest.raises(ValueError, match="不同的文件夹"):
        inspect_folders(FolderConcatRequest(directory_a=str(a), directory_b=str(a)))


def test_folder_concat_preview_api_reports_pair_count(tmp_path):
    (tmp_path / "config").mkdir()
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    (a / "one.mp4").touch()
    (b / "two.mp4").touch()
    client = authenticated_client(create_app(tmp_path))
    response = client.post("/api/v1/tools/folder-concat/preview", json={
        "directory_a": str(a), "directory_b": str(b),
    })
    assert response.status_code == 200
    assert response.json()["pair_count"] == 1


@pytest.mark.parametrize("use_builder", [False, True])
def test_folder_concat_renders_a_then_b_and_records_result(tmp_path, use_builder):
    a, b, output = tmp_path / "A", tmp_path / "B", tmp_path / "output"
    output.mkdir()
    make_video(a / "z.mp4", "red")
    make_video(a / "a.mp4", "green")
    make_video(b / "b.mp4", "blue")
    config_directory = tmp_path / "config"
    config_directory.mkdir()
    manager = JobManager(ConfigStore(config_directory), tmp_path / "data")
    job = manager.create_folder_concat(FolderConcatRequest(
        directory_a=str(a), directory_b=str(b), output_directory=str(output),
        naming=builder_naming() if use_builder else None,
    ))
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = manager.get_job(job["id"])
        if job["status"] in {"completed", "partial_failed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job["status"] == "completed", [item["error"] for item in job["items"]]
    assert job["job_type"] == "folder_concat"
    assert job["count"] == 1
    assert job["items"][0]["selections"]["pool_1"]["name"] == "a.mp4"
    assert job["items"][0]["selections"]["pool_2"]["name"] == "b.mp4"
    rendered = Path(job["items"][0]["output_path"])
    assert rendered.name == (f"a-b{date.today():%m%d}7.mp4" if use_builder else "a_拼接.mp4")
    if use_builder:
        assert job["items"][0]["naming"]["sequence"] == 7
        assert job["items"][0]["naming"]["block_values"][2]["asset"] == "b.mp4"
        assert "enabled: true" in Path(job["config_snapshot_path"]).read_text()
    assert rendered.exists()
    assert probe_media(rendered).duration > 0.4
    assert len(list(a.glob("*.mp4"))) == 2
    assert (rendered.parent / "manifest.json").exists()
    assert (rendered.parent / "manifest.csv").exists()


def test_folder_concat_reads_pipe_separator_from_both_folders(tmp_path):
    a, b, output = tmp_path / "A", tmp_path / "B", tmp_path / "output"
    output.mkdir()
    source_a = a / "00006|!|周小小闹_大学宿舍_|!|标题-甲|!|2027-02-28.mp4"
    source_b = b / "瑞幸|!|咖啡|!|广告-乙.mp4"
    make_video(source_a, "red")
    make_video(source_b, "blue")
    naming = builder_naming()
    naming.builder.blocks[0].variants = [
        NamingBlockVariantConfig(sample_name=source_a.name, field_count=4, field_index=1),
    ]
    naming.builder.blocks[2].variants = [
        NamingBlockVariantConfig(sample_name=source_b.name, field_count=3, field_index=0),
    ]
    # Revalidate the request as the browser/API does.
    request = FolderConcatRequest(
        directory_a=str(a), directory_b=str(b), output_directory=str(output),
        naming=naming.model_dump(mode="json"),
    )
    preview = inspect_folders(request)
    expected = f"周小小闹_大学宿舍_-瑞幸{date.today():%m%d}7.mp4"
    assert preview["pairs"][0]["output_name"] == expected
    manager = JobManager(ConfigStore(tmp_path / "config"), tmp_path / "data")
    job = manager.create_folder_concat(request)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        job = manager.get_job(job["id"])
        if job["status"] in {"completed", "partial_failed", "failed", "cancelled"}:
            break
        time.sleep(0.05)
    assert job["status"] == "completed", [item["error"] for item in job["items"]]
    rendered = Path(job["items"][0]["output_path"])
    assert rendered.name == expected
    assert probe_media(rendered).duration > 0.4


def test_folder_naming_mixed_field_counts_keep_selected_position_and_freeze_rules(tmp_path, monkeypatch):
    a, b, output = tmp_path / "A", tmp_path / "B", tmp_path / "output"
    output.mkdir()
    names = ["沪上阿姨|!|奶茶|!|店员.mp4", "通用|!|热菜|!|冒菜|!|1.mp4"]
    for index, name in enumerate(names, start=1):
        make_video(a / name, "red")
        make_video(b / f"{index}.mp4", "blue")
    naming = builder_naming()
    naming.builder.blocks[0].variants = [NamingBlockVariantConfig(
        sample_name=names[0], field_count=3, field_index=1,
    )]
    naming.builder.blocks = [naming.builder.blocks[0], naming.builder.blocks[-1]]
    request = FolderConcatRequest(directory_a=str(a), directory_b=str(b), output_directory=str(output), naming=naming)
    preview = inspect_folders(request)
    assert [pair["output_name"] for pair in preview["pairs"]] == ["奶茶7.mp4", "热菜8.mp4"]
    assert [variant["field_count"] for variant in preview["naming"]["builder"]["blocks"][0]["variants"]] == [3, 4]
    assert len(request.naming.builder.blocks[0].variants) == 1  # Request and project config are not mutated.
    manager = overlay_manager(tmp_path, monkeypatch)
    job = manager.create_folder_concat(request)
    snapshot = yaml.safe_load(Path(job["config_snapshot_path"]).read_text())
    assert len(snapshot["output"]["naming"]["builder"]["blocks"][0]["variants"]) == 2
    JobManager.start(manager, job["id"])
    job = wait_for_concat(manager, job)
    assert job["status"] == "completed", [item["error"] for item in job["items"]]
    assert [Path(item["output_path"]).name for item in job["items"]] == ["奶茶7.mp4", "热菜8.mp4"]


def test_folder_naming_preserves_explicit_different_position_mapping(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(); b.mkdir()
    names = ["品牌|!|品类|!|文案.mp4", "编号|!|品牌|!|品类|!|文案.mp4"]
    for index, name in enumerate(names):
        (a / name).touch(); (b / f"{index}.mp4").touch()
    naming = builder_naming()
    naming.builder.blocks = naming.builder.blocks[:1]
    naming.builder.blocks[0].variants = [
        NamingBlockVariantConfig(sample_name=names[0], field_count=3, field_index=0),
        NamingBlockVariantConfig(sample_name=names[1], field_count=4, field_index=1),
    ]
    result = inspect_folders(FolderConcatRequest(directory_a=str(a), directory_b=str(b), naming=naming))
    assert [pair["output_name"] for pair in result["pairs"]] == ["品牌.mp4", "品牌-02.mp4"]
    (a / "额外|!|编号|!|品牌|!|品类|!|文案.mp4").touch()
    (b / "2.mp4").touch()
    with pytest.raises(ValueError, match="不同字段位置"):
        inspect_folders(FolderConcatRequest(directory_a=str(a), directory_b=str(b), naming=naming))


def test_folder_naming_missing_selected_field_still_blocks_with_clear_error(tmp_path):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(); b.mkdir()
    (a / "品牌|!|品类|!|文案.mp4").touch()
    (b / "1.mp4").touch()
    naming = builder_naming()
    naming.builder.blocks = naming.builder.blocks[:1]
    naming.builder.blocks[0].variants = [NamingBlockVariantConfig(
        sample_name="品牌|!|品类|!|文案|!|序号.mp4", field_count=4, field_index=3,
    )]
    with pytest.raises(ValueError, match="只有 3 个字段，无法读取所选第 4 个字段"):
        inspect_folders(FolderConcatRequest(directory_a=str(a), directory_b=str(b), naming=naming))


def test_folder_concat_naming_preview_and_duplicates(tmp_path):
    a, b = tmp_path / "中文 A", tmp_path / "中文 B"
    a.mkdir()
    b.mkdir()
    for name in ("商品.mp4", "商品.mov"):
        (a / name).touch()
    for name in ("达人甲.mp4", "达人乙.mp4"):
        (b / name).touch()
    naming = builder_naming()
    naming.builder.blocks = naming.builder.blocks[:1]
    client = authenticated_client(create_app(tmp_path))
    payload = {"directory_a": str(a), "directory_b": str(b), "naming": naming.model_dump(mode="json")}
    response = client.post("/api/v1/tools/folder-concat/preview", json=payload)
    assert response.status_code == 200
    assert [pair["output_name"] for pair in response.json()["pairs"]] == ["商品.mp4", "商品-02.mp4"]
    payload["naming"]["duplicate_suffix"] = "-固定{serial!s:.0}"
    response = client.post("/api/v1/tools/folder-concat/preview", json=payload)
    assert response.status_code == 200
    # An unchanged suffix must fail rather than loop on a third collision.
    (a / "商品.mkv").touch()
    (b / "达人丙.mp4").touch()
    response = client.post("/api/v1/tools/folder-concat/preview", json=payload)
    assert response.status_code == 422
    assert "递增序号" in response.json()["detail"]


@pytest.mark.parametrize("invalid", ["category", "field_count", "filename"])
def test_folder_concat_rejects_invalid_builder_before_creating_job(tmp_path, invalid):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir()
    b.mkdir()
    (a / "a.mp4").touch()
    (b / "b.mp4").touch()
    naming = builder_naming().model_dump(mode="json")
    if invalid == "category":
        naming["builder"]["blocks"][0]["category"] = "pool_3"
    elif invalid == "field_count":
        naming["builder"]["blocks"][0]["variants"] = [
            {"sample_name": "123.mp4", "field_count": 2, "field_index": 0},
        ]
    else:
        naming["builder"]["blocks"][1]["text"] = "/"
    client = authenticated_client(create_app(tmp_path))
    payload = {"directory_a": str(a), "directory_b": str(b), "naming": naming}
    for endpoint in ("preview", ""):
        response = client.post("/api/v1/tools/folder-concat" + (f"/{endpoint}" if endpoint else ""), json=payload)
        assert response.status_code == 422
    assert not list(tmp_path.glob("**/config.snapshot.yaml"))
