"""Persistent local codec preferences, snapshotted when a task is created."""
from __future__ import annotations

import os
import threading
import uuid
from pathlib import Path

from pydantic import BaseModel, ConfigDict, StrictBool


class CodecSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    software_codec_enabled: StrictBool = False


class CodecSettingsStore:
    def __init__(self, data_directory: Path):
        self.path = data_directory / "codec_settings.json"
        self.lock = threading.RLock()

    def get(self) -> CodecSettings:
        with self.lock:
            if not self.path.exists():
                return CodecSettings()
            return CodecSettings.model_validate_json(self.path.read_text(encoding="utf-8"))

    def save(self, settings: CodecSettings) -> CodecSettings:
        with self.lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
            try:
                temporary.write_text(settings.model_dump_json(indent=2), encoding="utf-8")
                os.replace(temporary, self.path)
            finally:
                temporary.unlink(missing_ok=True)
            return settings
