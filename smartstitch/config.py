from __future__ import annotations

import hashlib
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path

import yaml

from .models import AppConfig, AssetItemConfig, ConfigUpdateRequest, WeightUpdate


class ConfigError(ValueError):
    pass


class ConfigStore:
    def __init__(self, directory: Path):
        self.directory = directory
        self.backup_directory = directory / "backups"
        self.lock = threading.RLock()
        self.directory.mkdir(parents=True, exist_ok=True)

    def list(self) -> list[dict[str, object]]:
        with self.lock:
            configs = []
            for path in sorted(self.directory.glob("*.yaml")):
                if path.name.startswith("template"):
                    continue
                try:
                    config = self.load_path(path)
                    configs.append(
                        {
                            "id": config.id,
                            "name": config.name,
                            "enabled": config.enabled,
                            "description": config.description,
                            "path": str(path),
                            "content_hash": self.content_hash(config.id),
                            "updated_at": path.stat().st_mtime,
                            "valid": True,
                        }
                    )
                except Exception as exc:
                    configs.append(
                        {
                            "id": path.stem,
                            "name": path.stem,
                            "enabled": False,
                            "description": "",
                            "path": str(path),
                            "updated_at": path.stat().st_mtime,
                            "valid": False,
                            "error": str(exc),
                        }
                    )
            return configs

    def path_for(self, config_id: str) -> Path:
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", config_id):
            raise ConfigError("配置 ID 格式不正确")
        return self.directory / f"{config_id}.yaml"

    def load(self, config_id: str) -> AppConfig:
        with self.lock:
            path = self.path_for(config_id)
            if not path.exists():
                raise FileNotFoundError(f"配置不存在: {config_id}")
            return self.load_path(path)

    def load_path(self, path: Path) -> AppConfig:
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ConfigError("配置根节点必须是对象")
            return AppConfig.model_validate(data)
        except (yaml.YAMLError, ValueError) as exc:
            raise ConfigError(str(exc)) from exc

    def raw(self, config_id: str) -> str:
        with self.lock:
            return self.path_for(config_id).read_text(encoding="utf-8")

    def content_hash(self, config_id: str) -> str:
        with self.lock:
            content = self.path_for(config_id).read_bytes()
        return hashlib.sha256(content).hexdigest()

    def validate_text(self, text: str) -> AppConfig:
        try:
            data = yaml.safe_load(text)
            if not isinstance(data, dict):
                raise ConfigError("配置根节点必须是对象")
            return AppConfig.model_validate(data)
        except (yaml.YAMLError, ValueError) as exc:
            raise ConfigError(str(exc)) from exc

    def save_text(self, config_id: str, request: ConfigUpdateRequest) -> AppConfig:
        with self.lock:
            try:
                source_data = yaml.safe_load(request.yaml_text)
            except yaml.YAMLError as exc:
                raise ConfigError(str(exc)) from exc
            config = self.validate_text(request.yaml_text)
            if config.id != config_id:
                raise ConfigError(
                    f"YAML 内 id={config.id!r} 与目标配置 {config_id!r} 不一致"
                )
            text = request.yaml_text
            if (
                not isinstance(source_data, dict)
                or source_data.get("schema_version", 1) != 2
            ):
                text = yaml.safe_dump(
                    config.model_dump(mode="json"), allow_unicode=True, sort_keys=False
                )
            self._atomic_save(self.path_for(config_id), text)
            return config

    def save_config(self, config_id: str, config: AppConfig) -> AppConfig:
        with self.lock:
            if config.id != config_id:
                raise ConfigError(
                    f"配置内 id={config.id!r} 与目标配置 {config_id!r} 不一致"
                )
            text = yaml.safe_dump(
                config.model_dump(mode="json"), allow_unicode=True, sort_keys=False
            )
            self._atomic_save(self.path_for(config_id), text)
            return config

    def clone(self, source_id: str, new_id: str, new_name: str) -> AppConfig:
        with self.lock:
            destination = self.path_for(new_id)
            if destination.exists():
                raise ConfigError(f"配置已存在: {new_id}")
            data = yaml.safe_load(self.raw(source_id))
            data["id"] = new_id
            data["name"] = new_name
            config = self.validate_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False))
            text = yaml.safe_dump(
                config.model_dump(mode="json"), allow_unicode=True, sort_keys=False
            )
            self._atomic_save(destination, text, backup=False)
            return config

    def create(self, new_id: str, new_name: str) -> AppConfig:
        with self.lock:
            new_name = new_name.strip()
            if not new_name:
                raise ConfigError("配置名称不能为空")
            destination = self.path_for(new_id)
            if destination.exists():
                raise ConfigError(f"配置已存在: {new_id}")
            template = self.directory / "template.commented.yaml"
            if not template.exists():
                raise ConfigError("缺少配置模板 template.commented.yaml")
            data = yaml.safe_load(template.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ConfigError("配置模板格式不正确")
            data["id"] = new_id
            data["name"] = new_name
            text = yaml.safe_dump(data, allow_unicode=True, sort_keys=False)
            config = self.validate_text(text)
            self._atomic_save(destination, text, backup=False)
            return config

    def create_config(self, config: AppConfig) -> AppConfig:
        """Persist a validated config only when its ID does not already exist."""
        with self.lock:
            destination = self.path_for(config.id)
            if destination.exists():
                raise ConfigError(f"配置已存在: {config.id}")
            text = yaml.safe_dump(
                config.model_dump(mode="json"), allow_unicode=True, sort_keys=False
            )
            self._atomic_save(destination, text, backup=False)
            return config

    def delete(self, config_id: str) -> Path:
        with self.lock:
            path = self.path_for(config_id)
            if not path.exists():
                raise FileNotFoundError(f"配置不存在: {config_id}")
            self.backup_directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
            backup = self.backup_directory / f"{path.stem}-{stamp}.deleted.yaml"
            path.replace(backup)
            return backup

    def update_weights(self, config_id: str, updates: list[WeightUpdate]) -> AppConfig:
        with self.lock:
            config = self.load(config_id)
            grouped: dict[str, list[WeightUpdate]] = {}
            for update in updates:
                grouped.setdefault(update.category, []).append(update)

            for category, category_updates in grouped.items():
                if category == "benefit_overlay":
                    # 风险提示语图片是配置级唯一固定图片，不参与素材权重更新。
                    continue
                elif category in config.sources:
                    group = config.sources[category]
                else:
                    raise ConfigError(f"未知素材类别: {category}")
                group.items = [
                    AssetItemConfig(
                        path=item.path,
                        enabled=item.enabled,
                        weight=item.weight,
                        tags=item.tags,
                    )
                    for item in category_updates
                ]

            text = yaml.safe_dump(
                config.model_dump(mode="json"), allow_unicode=True, sort_keys=False
            )
            self._atomic_save(self.path_for(config_id), text)
            return config

    def _atomic_save(self, path: Path, text: str, backup: bool = True) -> None:
        with self.lock:
            if backup and path.exists():
                self.backup_directory.mkdir(parents=True, exist_ok=True)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                shutil.copy2(path, self.backup_directory / f"{path.stem}-{stamp}.yaml")
            temp = path.with_suffix(".yaml.tmp")
            temp.write_text(text, encoding="utf-8")
            temp.replace(path)
