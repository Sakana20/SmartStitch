"""Fetch pinned CoreML release assets for inclusion in SmartStitch.app.

The builder needs network access once; installed applications do not.
Set SMARTSTITCH_UPSCALE_ARCHIVE_DIR to a directory of verified ZIPs for an
offline build. The extracted build output is ignored by Git.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from smartstitch.video_upscale import (  # noqa: E402
    MODEL_CONFIGS, MODEL_RELEASE_URL, _sha256, model_asset_name, model_status, prepare_model,
)


def archive_for(model_name: str, cache: Path) -> Path:
    filename = f"{model_asset_name(model_name)}.zip"
    supplied = os.environ.get("SMARTSTITCH_UPSCALE_ARCHIVE_DIR", "").strip()
    archive = Path(supplied).expanduser() / filename if supplied else cache / filename
    expected = MODEL_CONFIGS[model_name]["sha256"]
    if archive.is_file() and _sha256(archive) == expected:
        return archive
    if supplied:
        raise RuntimeError(f"离线模型压缩包缺失或 SHA-256 不匹配：{archive}")
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f"{model_name}-", suffix=".zip", dir=cache, delete=False) as file:
        temporary = Path(file.name)
    try:
        print(f"Downloading verified {model_name} CoreML model...", flush=True)
        with urlopen(f"{MODEL_RELEASE_URL}/{filename}", timeout=60) as response, temporary.open("wb") as output:
            shutil.copyfileobj(response, output)
        if _sha256(temporary) != expected:
            raise RuntimeError(f"{model_name} 模型压缩包 SHA-256 不匹配")
        os.replace(temporary, archive)
        return archive
    finally:
        temporary.unlink(missing_ok=True)


def main() -> None:
    destination = ROOT / "build" / "upscale-models"
    cache = ROOT / "build" / "upscale-model-cache"
    for model_name in MODEL_CONFIGS:
        archive = archive_for(model_name, cache)
        name = model_asset_name(model_name)
        target = destination / "models" / f"{name}.mlpackage"
        if target.exists():
            shutil.rmtree(target)
        (target.parent / f"{name}.sha256").unlink(missing_ok=True)
        status = prepare_model(destination, archive, model_name=model_name)
        if not status["available"]:
            raise RuntimeError(f"{model_name} 模型提取后校验失败")
        print(f"Ready for app bundle: {model_name}", flush=True)


if __name__ == "__main__":
    main()
