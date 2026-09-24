from __future__ import annotations

import json
import os
import re
import ssl
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml
import truststore

from .database import SQLiteStore


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
MEDIA_UPLOAD_DIRECT_LIMIT = 20 * 1024 * 1024
MEDIA_UPLOAD_PART_DELAY = 0.22
SYNC_FIELDS = [
    "日期",
    "单选",
    "文本",
    "文件",
    "命名",
    "当日文件夹路径",
    "利益点",
    "达人",
    "限制日期",
    "_smartstitch_key",
]
SMARTSTITCH_TEXT_FIELDS = ["利益点", "达人", "限制日期", "_smartstitch_key"]
USER_FIELD_TYPES: dict[str, set[int | str]] = {
    "日期": {5, "date", "datetime"},
    "单选": {3, "select", "single_select"},
    "文本": {1, "text"},
    "文件": {17, "attachment"},
    "命名": {1, "text"},
    "当日文件夹路径": {1, "text"},
}
TAOBAO_FLASH_MATERIAL_FIELD = "素材（命名：利益点-形式-日期-剪辑-特殊标识-序列）"
TAOBAO_FLASH_SYNC_FIELDS = [
    "出片日期",
    "剪辑",
    "视频",
    TAOBAO_FLASH_MATERIAL_FIELD,
    "审核",
    "_smartstitch_key",
]
TAOBAO_FLASH_FIELD_TYPES: dict[str, set[int | str]] = {
    "出片日期": {5, "date", "datetime"},
    "剪辑": {1, "text"},
    "视频": {17, "attachment"},
    TAOBAO_FLASH_MATERIAL_FIELD: {1, "text"},
    "审核": {3, "select", "single_select"},
    "_smartstitch_key": {1, "text"},
}


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class FeishuError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        retryable: bool = False,
        required_scopes: list[str] | None = None,
        console_url: str | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.required_scopes = required_scopes or []
        self.console_url = console_url


def _feishu_error_details(message: str) -> tuple[str, list[str], str | None]:
    scopes = sorted(
        set(re.findall(r"\b(?:base|wiki|bitable|docs|drive):[a-z0-9:._-]+\b", message))
    )
    url_match = re.search(r"https://open\.feishu\.cn/[^\s]+", message)
    console_url = url_match.group(0).rstrip("。；;,)") if url_match else None
    if not scopes:
        return f"飞书请求失败: {message}", [], console_url
    labels = {
        "base:table:read": "读取多维表格数据表",
        "base:field:delete": "删除多维表格字段",
        "base:record:delete": "删除多维表格记录",
        "docs:document.media:upload": "上传云文档附件",
        "wiki:wiki:readonly": "读取知识库节点",
    }
    scope_text = "、".join(f"{labels.get(scope, '所需权限')}（{scope}）" for scope in scopes)
    friendly = f"飞书应用缺少权限：{scope_text}。请在开放平台开通并发布后重新测试连接。"
    return friendly, scopes, console_url


class FeishuSettingsStore:
    """Local secret storage. Public responses never expose app_secret."""

    def __init__(self, data_directory: Path):
        self.path = data_directory / "integrations.yaml"
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> dict[str, str]:
        with self.lock:
            if not self.path.exists():
                return {"app_id": "", "app_secret": ""}
            try:
                payload = yaml.safe_load(self.path.read_text("utf-8")) or {}
            except (OSError, yaml.YAMLError) as exc:
                raise FeishuError(f"无法读取飞书集成配置: {exc}") from exc
            feishu = payload.get("feishu") if isinstance(payload, dict) else None
            if not isinstance(feishu, dict):
                return {"app_id": "", "app_secret": ""}
            return {
                "app_id": str(feishu.get("app_id") or "").strip(),
                "app_secret": str(feishu.get("app_secret") or "").strip(),
            }

    def public(self) -> dict[str, object]:
        settings = self.load()
        return {
            "app_id": settings["app_id"],
            "app_secret_configured": bool(settings["app_secret"]),
        }

    def update(self, app_id: str, app_secret: str | None = None) -> dict[str, object]:
        with self.lock:
            current = self.load()
            secret = current["app_secret"] if app_secret in {None, ""} else app_secret
            payload = {
                "schema_version": 1,
                "feishu": {"app_id": app_id.strip(), "app_secret": secret},
            }
            self._atomic_write(yaml.safe_dump(payload, allow_unicode=True, sort_keys=False))
        return self.public()

    def credentials(
        self, app_id: str | None = None, app_secret: str | None = None
    ) -> tuple[str, str]:
        current = self.load()
        resolved_id = (app_id or current["app_id"]).strip()
        resolved_secret = (app_secret or current["app_secret"]).strip()
        if not resolved_id or not resolved_secret:
            raise FeishuError("请先配置飞书 App ID 和 App Secret")
        return resolved_id, resolved_secret

    def _atomic_write(self, text: str) -> None:
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".integrations-", suffix=".yaml.tmp", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(self.path)
            self.path.chmod(0o600)
        except Exception:
            temporary.unlink(missing_ok=True)
            raise


def parse_base_url(url: str) -> tuple[str, str | None]:
    try:
        parsed = urllib.parse.urlparse(url.strip())
    except ValueError as exc:
        raise FeishuError("飞书多维表格链接格式不正确") from exc
    if parsed.scheme not in {"http", "https"}:
        raise FeishuError("请填写完整的飞书多维表格链接")
    parts = [part for part in parsed.path.split("/") if part]
    marker = next((value for value in ("base", "wiki") if value in parts), None)
    if marker is None or parts.index(marker) + 1 >= len(parts):
        raise FeishuError("链接不是飞书多维表格链接")
    token = parts[parts.index(marker) + 1]
    table_id = urllib.parse.parse_qs(parsed.query).get("table", [None])[0]
    return token, table_id


class FeishuBaseClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        opener: Callable[..., Any] | None = None,
        ssl_context: ssl.SSLContext | None = None,
        timeout: float = 15,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.opener = opener or urllib.request.urlopen
        self.ssl_context = ssl_context or truststore.SSLContext(
            ssl.PROTOCOL_TLS_CLIENT
        )
        self.timeout = timeout
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.RLock()

    def resolve_base_url(self, url: str) -> tuple[str, str | None]:
        token, table_id = parse_base_url(url)
        parts = [part for part in urllib.parse.urlparse(url).path.split("/") if part]
        if "wiki" not in parts:
            return token, table_id
        query = urllib.parse.urlencode({"token": token})
        data = self._request("GET", f"/wiki/v2/spaces/node_by_token?{query}")
        node = data.get("node") if isinstance(data.get("node"), dict) else data
        obj_type = str(node.get("obj_type") or "")
        base_token = str(node.get("obj_token") or "")
        if obj_type not in {"bitable", "base"} or not base_token:
            raise FeishuError("这个知识库链接指向的不是飞书多维表格")
        return base_token, table_id

    def list_tables(self, base_token: str) -> list[dict[str, object]]:
        items = self._list_all(f"/base/v3/bases/{base_token}/tables", 300)
        return [self._normalize_table(item) for item in items]

    def get_table(self, base_token: str, table_id: str) -> dict[str, object]:
        data = self._request(
            "GET", f"/base/v3/bases/{base_token}/tables/{table_id}"
        )
        item = data.get("table") if isinstance(data.get("table"), dict) else data
        table = self._normalize_table(item)
        if not table["table_id"]:
            table["table_id"] = table_id
        return table

    @staticmethod
    def _normalize_table(item: dict[str, Any]) -> dict[str, object]:
        return {
            "table_id": str(item.get("table_id") or item.get("id") or ""),
            "name": str(item.get("name") or item.get("title") or ""),
        }

    def list_fields(self, base_token: str, table_id: str) -> list[dict[str, object]]:
        return self._list_all(
            f"/base/v3/bases/{base_token}/tables/{table_id}/fields", 300
        )

    def ensure_text_fields(
        self, base_token: str, table_id: str, field_names: list[str]
    ) -> None:
        existing = {
            str(field.get("name") or field.get("field_name") or ""): field
            for field in self.list_fields(base_token, table_id)
        }
        for field_name in field_names:
            if field_name in existing:
                field_type = existing[field_name].get("type")
                if field_type not in {None, "text", 1}:
                    raise FeishuError(
                        f"目标数据表字段“{field_name}”已存在，但不是文本字段"
                    )
                continue
            self._request(
                "POST",
                f"/base/v3/bases/{base_token}/tables/{table_id}/fields",
                {"name": field_name, "type": "text"},
            )
            existing[field_name] = {"name": field_name, "type": "text"}

    def ensure_sync_schema(self, base_token: str, table_id: str) -> None:
        self._ensure_field_types(base_token, table_id, USER_FIELD_TYPES)
        self.ensure_text_fields(base_token, table_id, SMARTSTITCH_TEXT_FIELDS)

    def ensure_taobao_flash_sync_schema(
        self, base_token: str, table_id: str
    ) -> None:
        self._ensure_field_types(base_token, table_id, TAOBAO_FLASH_FIELD_TYPES)

    def _ensure_field_types(
        self,
        base_token: str,
        table_id: str,
        required_types: dict[str, set[int | str]],
    ) -> None:
        fields = {
            str(field.get("name") or field.get("field_name") or ""): field
            for field in self.list_fields(base_token, table_id)
        }
        missing = [name for name in required_types if name not in fields]
        if missing:
            raise FeishuError(
                "目标数据表缺少需要由用户预先创建的字段：" + "、".join(missing)
            )
        for name, accepted_types in required_types.items():
            field_type = fields[name].get("type")
            normalized = field_type.casefold() if isinstance(field_type, str) else field_type
            if normalized not in accepted_types:
                raise FeishuError(f"目标数据表字段“{name}”的类型不正确")

    def list_records(self, base_token: str, table_id: str) -> list[dict[str, Any]]:
        return self._list_all(
            f"/base/v3/bases/{base_token}/tables/{table_id}/records", 500
        )

    def batch_create_records(
        self, base_token: str, table_id: str, records: list[dict[str, Any]]
    ) -> None:
        for offset in range(0, len(records), 200):
            self._request(
                "POST",
                f"/base/v3/bases/{base_token}/tables/{table_id}/records/batch_create",
                {"create_records": records[offset : offset + 200]},
            )

    def batch_update_records(
        self,
        base_token: str,
        table_id: str,
        records: list[tuple[str, dict[str, Any]]],
    ) -> None:
        for offset in range(0, len(records), 200):
            chunk = records[offset : offset + 200]
            self._request(
                "POST",
                f"/base/v3/bases/{base_token}/tables/{table_id}/records/batch_update",
                {
                    "update_records": {
                        record_id: fields for record_id, fields in chunk
                    }
                },
            )

    def delete_field(self, base_token: str, table_id: str, field_id: str) -> None:
        self._request(
            "DELETE",
            f"/bitable/v1/apps/{base_token}/tables/{table_id}/fields/{field_id}",
        )

    def batch_delete_records(
        self, base_token: str, table_id: str, record_ids: list[str]
    ) -> None:
        for offset in range(0, len(record_ids), 500):
            self._request(
                "POST",
                f"/bitable/v1/apps/{base_token}/tables/{table_id}/records/batch_delete",
                {"records": record_ids[offset : offset + 500]},
            )

    def upload_attachment(self, base_token: str, file_path: str | Path) -> str:
        path = Path(file_path)
        if not path.is_file():
            raise FeishuError(f"待上传的成片不存在：{path}")
        size = path.stat().st_size
        common: dict[str, object] = {
            "file_name": path.name,
            "parent_type": "bitable_file",
            "parent_node": base_token,
            "size": size,
            "extra": json.dumps(
                {"drive_route_token": base_token}, ensure_ascii=False
            ),
        }
        if size <= MEDIA_UPLOAD_DIRECT_LIMIT:
            data = self._multipart_request(
                "/drive/v1/medias/upload_all", common, path.read_bytes()
            )
        else:
            prepared = self._request(
                "POST", "/drive/v1/medias/upload_prepare", common
            )
            upload_id = str(prepared.get("upload_id") or "")
            block_size = int(prepared.get("block_size") or 0)
            block_num = int(prepared.get("block_num") or 0)
            if not upload_id or block_size <= 0 or block_num <= 0:
                raise FeishuError("飞书未返回有效的分片上传策略")
            with path.open("rb") as handle:
                for seq in range(block_num):
                    chunk = handle.read(block_size)
                    if not chunk:
                        raise FeishuError("读取成片分片时提前结束")
                    self._multipart_request(
                        "/drive/v1/medias/upload_part",
                        {
                            "upload_id": upload_id,
                            "seq": seq,
                            "size": len(chunk),
                        },
                        chunk,
                    )
                    if seq + 1 < block_num:
                        time.sleep(MEDIA_UPLOAD_PART_DELAY)
            data = self._request(
                "POST",
                "/drive/v1/medias/upload_finish",
                {"upload_id": upload_id, "block_num": block_num},
            )
        file_token = str(data.get("file_token") or "")
        if not file_token:
            raise FeishuError("飞书附件上传成功但未返回 file_token")
        return file_token

    def _multipart_request(
        self, path: str, fields: dict[str, object], file_bytes: bytes
    ) -> dict[str, Any]:
        boundary = f"----SmartStitch{uuid.uuid4().hex}"
        body = bytearray()
        for name, value in fields.items():
            body.extend(f"--{boundary}\r\n".encode())
            body.extend(
                f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode()
            )
            body.extend(str(value).encode("utf-8"))
            body.extend(b"\r\n")
        body.extend(f"--{boundary}\r\n".encode())
        body.extend(b'Content-Disposition: form-data; name="file"; filename="blob"\r\n')
        body.extend(b"Content-Type: application/octet-stream\r\n\r\n")
        body.extend(file_bytes)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        request = urllib.request.Request(
            f"{FEISHU_API_BASE}{path}",
            data=bytes(body),
            headers={
                "Authorization": f"Bearer {self._tenant_token()}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        return self._open_json(request)

    def _list_all(self, path: str, page_size: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = urllib.parse.urlencode({"limit": page_size, "offset": offset})
            data = self._request("GET", f"{path}?{query}")
            if all(
                isinstance(data.get(key), list)
                for key in ("data", "fields", "record_id_list")
            ):
                page_items = self._columnar_records(data)
            else:
                page_items = (
                    data.get("items")
                    or data.get("tables")
                    or data.get("fields")
                    or data.get("records")
                    or data.get("blocks")
                    or []
                )
            if not isinstance(page_items, list):
                raise FeishuError("飞书多维表格返回的数据格式不正确")
            items.extend(item for item in page_items if isinstance(item, dict))
            offset += len(page_items)
            total = data.get("total")
            if not page_items or len(page_items) < page_size:
                return items
            if isinstance(total, int) and offset >= total:
                return items

    @staticmethod
    def _columnar_records(data: dict[str, Any]) -> list[dict[str, Any]]:
        """Normalize Base v3's fields + record_id_list + data response."""
        rows = data.get("data")
        fields = data.get("fields")
        record_ids = data.get("record_id_list")
        if not all(isinstance(value, list) for value in (rows, fields, record_ids)):
            return []
        if len(rows) != len(record_ids):
            raise FeishuError("飞书多维表格返回的记录行数不一致")
        field_names = [
            str(field.get("name") or field.get("field_name") or "")
            if isinstance(field, dict)
            else str(field)
            for field in fields
        ]
        records: list[dict[str, Any]] = []
        for record_id, row in zip(record_ids, rows, strict=True):
            if not isinstance(row, list):
                raise FeishuError("飞书多维表格返回的记录格式不正确")
            records.append(
                {
                    "record_id": str(record_id),
                    "fields": {
                        field_name: value
                        for field_name, value in zip(field_names, row)
                        if field_name
                    },
                }
            )
        return records

    def _tenant_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._token_expires_at:
                return self._token
            response = self._raw_request(
                "POST",
                "/auth/v3/tenant_access_token/internal",
                {"app_id": self.app_id, "app_secret": self.app_secret},
                authenticated=False,
            )
            token = str(response.get("tenant_access_token") or "")
            if not token:
                raise FeishuError("飞书鉴权成功但未返回 tenant_access_token")
            expires = max(60, int(response.get("expire") or 7200))
            self._token = token
            self._token_expires_at = time.monotonic() + expires - 60
            return token

    def _request(
        self, method: str, path: str, payload: dict[str, object] | None = None
    ) -> dict[str, Any]:
        return self._raw_request(method, path, payload, authenticated=True)

    def _raw_request(
        self,
        method: str,
        path: str,
        payload: dict[str, object] | None,
        *,
        authenticated: bool,
    ) -> dict[str, Any]:
        headers = {"Content-Type": "application/json; charset=utf-8"}
        if authenticated:
            headers["Authorization"] = f"Bearer {self._tenant_token()}"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload else None
        request = urllib.request.Request(
            f"{FEISHU_API_BASE}{path}", data=body, headers=headers, method=method
        )
        return self._open_json(request)

    def _open_json(self, request: urllib.request.Request) -> dict[str, Any]:
        try:
            response = self.opener(
                request,
                timeout=self.timeout,
                context=self.ssl_context,
            )
            with response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                error_payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            message = error_payload.get("msg") or error_payload.get("message") or str(exc)
            friendly, scopes, console_url = _feishu_error_details(str(message))
            raise FeishuError(
                friendly,
                code=error_payload.get("code"),
                retryable=exc.code == 429 or exc.code >= 500,
                required_scopes=scopes,
                console_url=console_url,
            ) from exc
        except (OSError, TimeoutError) as exc:
            raise FeishuError(f"无法连接飞书: {exc}", retryable=True) from exc
        try:
            response_payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FeishuError("飞书返回了无法解析的响应") from exc
        code = response_payload.get("code", 0)
        if code != 0:
            message = str(response_payload.get("msg") or "未知错误")
            normalized_message = message.casefold()
            friendly, scopes, console_url = _feishu_error_details(message)
            raise FeishuError(
                friendly,
                code=code,
                retryable=(
                    code in {1254290, 1254291, 99991400}
                    or "rate limit" in normalized_message
                    or "too many requests" in normalized_message
                    or "toomanyrequest" in normalized_message
                    or "concurrent" in normalized_message
                    or "field not found" in normalized_message
                    or "fieldnotfound" in normalized_message
                    or "频率" in message
                    or "字段不存在" in message
                ),
                required_scopes=scopes,
                console_url=console_url,
            )
        data = response_payload.get("data")
        return data if isinstance(data, dict) else response_payload


class FeishuSyncManager:
    def __init__(
        self,
        database: SQLiteStore,
        settings: FeishuSettingsStore,
        job_getter: Callable[[str], dict[str, Any]],
        *,
        client_factory: Callable[[str, str], FeishuBaseClient] = FeishuBaseClient,
        retry_delays: tuple[float, ...] = (5, 30, 120, 600),
    ):
        self.database = database
        self.settings = settings
        self.job_getter = job_getter
        self.client_factory = client_factory
        self.retry_delays = retry_delays
        self.lock = threading.RLock()
        self.sync_lock = threading.Lock()
        self.running: set[str] = set()
        self.threads: dict[str, threading.Thread] = {}
        self.retry_timers: dict[str, threading.Timer] = {}
        self.stopping = False
        self.database.ensure_job_table("output_sync_jobs")
        self.database.mark_active_interrupted("output_sync_jobs")

    def get(self, job_id: str) -> dict[str, Any] | None:
        return self.database.get("output_sync_jobs", self._record_id(job_id))

    def start(self, job_id: str) -> dict[str, Any]:
        job = self.job_getter(job_id)
        if job.get("status") not in {
            "completed", "partial_failed", "failed", "cancelled", "interrupted"
        }:
            raise FeishuError("只有已结束的成片任务才能同步")
        target = job.get("feishu_base_sync") or {}
        if not target.get("enabled"):
            raise FeishuError("该任务创建时未启用飞书多维表格同步")
        with self.lock:
            if self.stopping:
                raise FeishuError("SmartStitch 正在退出，不能启动同步")
            if job_id in self.running:
                return self.get(job_id) or self._new_record(job_id, target)
            timer = self.retry_timers.pop(job_id, None)
            if timer is not None:
                timer.cancel()
            record = self._new_record(job_id, target)
            self.database.save("output_sync_jobs", record)
            self.running.add(job_id)
            thread = threading.Thread(
                target=self._run, args=(job_id,), daemon=True, name=f"feishu-{job_id[:6]}"
            )
            self.threads[job_id] = thread
            thread.start()
            return record

    def active_count(self) -> int:
        with self.lock:
            return len(self.running) + len(self.retry_timers)

    def shutdown(self, timeout: float = 8.0) -> None:
        """Cancel scheduled retries and briefly wait for in-flight requests."""

        with self.lock:
            self.stopping = True
            timers = list(self.retry_timers.values())
            self.retry_timers.clear()
            threads = list(self.threads.values())
        for timer in timers:
            timer.cancel()
        deadline = time.monotonic() + max(0.0, timeout)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))

    def enqueue_if_enabled(self, job: dict[str, Any]) -> None:
        if (job.get("feishu_base_sync") or {}).get("enabled"):
            self.start(str(job["id"]))

    def resume_interrupted(self) -> None:
        """Resume sync records that were active when the process stopped."""
        for record in self.database.list("output_sync_jobs"):
            if record.get("status") != "interrupted":
                continue
            try:
                job = self.job_getter(str(record["job_id"]))
            except KeyError:
                continue
            if (job.get("feishu_base_sync") or {}).get("enabled"):
                self.start(str(record["job_id"]))

    def _run(self, job_id: str) -> None:
        record = self.get(job_id)
        if record is None:
            with self.lock:
                self.running.discard(job_id)
            return
        try:
            record.update(status="running", attempts=int(record.get("attempts", 0)) + 1,
                          started_at=_now(), finished_at=None, error=None)
            self.database.save("output_sync_jobs", record)
            job = self.job_getter(job_id)
            target = job["feishu_base_sync"]
            app_id, app_secret = self.settings.credentials()
            client = self.client_factory(app_id, app_secret)
            base_token, linked_table_id = client.resolve_base_url(target["base_url"])
            table_id = str(target.get("table_id") or linked_table_id or "")
            tables = client.list_tables(base_token)
            selected = next(
                (table for table in tables if table["table_id"] == table_id), None
            )
            if selected is None and table_id:
                selected = client.get_table(base_token, table_id)
            if selected is None:
                raise FeishuError("配置的飞书多维表格数据表不存在，请重新测试连接")
            with self.sync_lock:
                counts = self._sync_records(client, base_token, table_id, job, target)
            record.update(
                status="succeeded",
                finished_at=_now(),
                error=None,
                base_url=target["base_url"],
                table_id=table_id,
                table_name=selected["name"],
                **counts,
            )
            self.database.save("output_sync_jobs", record)
        except Exception as exc:
            retryable = isinstance(exc, FeishuError) and exc.retryable
            attempt = int(record.get("attempts", 0))
            with self.lock:
                retryable = retryable and not self.stopping
            if retryable and attempt <= len(self.retry_delays):
                delay = self.retry_delays[attempt - 1]
                next_retry = datetime.now().astimezone() + timedelta(seconds=delay)
                record.update(
                    status="pending",
                    finished_at=None,
                    error=str(exc),
                    next_retry_at=next_retry.isoformat(timespec="seconds"),
                )
                self.database.save("output_sync_jobs", record)
                timer = threading.Timer(delay, self._run_scheduled_retry, args=(job_id,))
                timer.daemon = True
                with self.lock:
                    self.retry_timers[job_id] = timer
                timer.start()
                return
            record.update(
                status="failed",
                finished_at=_now(),
                error=str(exc),
                next_retry_at=None,
            )
            self.database.save("output_sync_jobs", record)
        finally:
            with self.lock:
                self.running.discard(job_id)
                self.threads.pop(job_id, None)

    def _run_scheduled_retry(self, job_id: str) -> None:
        with self.lock:
            if self.stopping:
                self.retry_timers.pop(job_id, None)
                return
            if job_id in self.running:
                timer = threading.Timer(0.05, self._run_scheduled_retry, args=(job_id,))
                timer.daemon = True
                self.retry_timers[job_id] = timer
                timer.start()
                return
            self.retry_timers.pop(job_id, None)
        try:
            self.start(job_id)
        except (FeishuError, KeyError):
            return

    def _sync_records(
        self,
        client: FeishuBaseClient,
        base_token: str,
        table_id: str,
        job: dict[str, Any],
        target: dict[str, Any],
    ) -> dict[str, int]:
        items = list(job.get("items") or [])
        if target.get("row_scope") == "succeeded_only":
            items = [item for item in items if item.get("status") == "succeeded"]
        sync_config = job.get("feishu_base_sync") or {}
        field_schema = sync_config.get("field_schema", "auto")
        if field_schema == "auto":
            field_schema = job.get("workflow_type", "generic")
        is_taobao_flash = field_schema == "taobao_flash"
        if is_taobao_flash:
            client.ensure_taobao_flash_sync_schema(base_token, table_id)
        else:
            client.ensure_sync_schema(base_token, table_id)
        existing = client.list_records(base_token, table_id)
        key_records = {
            self._cell_text((record.get("fields") or {}).get("_smartstitch_key")): record
            for record in existing
            if isinstance(record.get("fields"), dict)
            and self._cell_text((record.get("fields") or {}).get("_smartstitch_key"))
        }
        updates: list[tuple[str, dict[str, Any]]] = []
        additions: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        daily_sequences = self._daily_sequence_counters(existing)
        for item in items:
            smartstitch_key = f"{job['id']}:{item['index']}"
            existing_record = key_records.get(smartstitch_key)
            existing_fields = (
                existing_record.get("fields")
                if isinstance(existing_record, dict)
                and isinstance(existing_record.get("fields"), dict)
                else {}
            )
            fields = (
                self._build_taobao_flash_fields(
                    job, item, existing_fields, daily_sequences
                )
                if is_taobao_flash
                else self._build_fields(job, item)
            )
            attachment_field = "视频" if is_taobao_flash else "文件"
            if not self._attachment_tokens(existing_fields.get(attachment_field)):
                output_path = str(item.get("output_path") or "")
                if item.get("status") == "succeeded" and output_path:
                    file_token = client.upload_attachment(base_token, output_path)
                    fields[attachment_field] = [{"file_token": file_token}]
            rows.append(fields)
            if existing_record:
                record_id = str(existing_record.get("record_id") or "")
                updates.append((record_id, fields))
            else:
                additions.append(fields)
        client.batch_update_records(base_token, table_id, updates)
        client.batch_create_records(base_token, table_id, additions)
        verified = client.list_records(base_token, table_id)
        verified_keys = {
            self._cell_text((record.get("fields") or {}).get("_smartstitch_key"))
            for record in verified
            if isinstance(record.get("fields"), dict)
        }
        expected_keys = {str(fields["_smartstitch_key"]) for fields in rows}
        missing = expected_keys - verified_keys
        if missing:
            raise FeishuError(
                f"飞书写入后回读缺少 {len(missing)} 条记录",
                retryable=True,
            )
        return {
            "expected_count": len(rows),
            "inserted_count": len(additions),
            "updated_count": len(updates),
            "verified_count": len(expected_keys),
        }

    @staticmethod
    def _build_fields(job: dict[str, Any], item: dict[str, Any]) -> dict[str, Any]:
        naming = item.get("naming") or {}
        output_path = str(item.get("output_path") or "")
        completed_at = item.get("finished_at") or job.get("finished_at") or _now()
        return {
            "日期": FeishuSyncManager._timestamp_ms(completed_at),
            "单选": "待审核",
            "文本": str(naming.get("product") or ""),
            "命名": str(item.get("output_name") or Path(output_path).name),
            "当日文件夹路径": str(Path(output_path).parent) if output_path else "",
            "利益点": str(naming.get("benefit") or ""),
            "达人": "+".join(str(value) for value in naming.get("talents") or []),
            "限制日期": str(naming.get("restriction_date") or ""),
            "_smartstitch_key": f"{job['id']}:{item['index']}",
        }

    @classmethod
    def _build_taobao_flash_fields(
        cls,
        job: dict[str, Any],
        item: dict[str, Any],
        existing_fields: dict[str, Any],
        daily_sequences: dict[str, int],
    ) -> dict[str, Any]:
        completed_at = item.get("finished_at") or job.get("finished_at") or _now()
        completed = cls._datetime_value(completed_at)
        day_key = completed.strftime("%Y%m%d")
        material_name = cls._cell_text(
            existing_fields.get(TAOBAO_FLASH_MATERIAL_FIELD)
        )
        if not material_name:
            next_sequence = daily_sequences.get(day_key, 0) + 1
            daily_sequences[day_key] = next_sequence
            material_name = f"一口价二剪-{completed:%m%d}-{next_sequence}"

        fields: dict[str, Any] = {
            "出片日期": int(completed.timestamp() * 1000),
            "剪辑": "",
            TAOBAO_FLASH_MATERIAL_FIELD: material_name,
            "审核": "待审核",
            "_smartstitch_key": f"{job['id']}:{item['index']}",
        }
        for name in ("剪辑", "审核"):
            if existing_fields.get(name) not in (None, "", []):
                fields[name] = existing_fields[name]
        return fields

    @classmethod
    def _daily_sequence_counters(
        cls, records: list[dict[str, Any]]
    ) -> dict[str, int]:
        counters: dict[str, int] = {}
        counts: dict[str, int] = {}
        for record in records:
            fields = record.get("fields") if isinstance(record, dict) else None
            if not isinstance(fields, dict):
                continue
            date_value = fields.get("出片日期")
            if date_value in (None, ""):
                continue
            completed = cls._datetime_value(date_value)
            day_key = completed.strftime("%Y%m%d")
            counts[day_key] = counts.get(day_key, 0) + 1
            material_name = cls._cell_text(
                fields.get(TAOBAO_FLASH_MATERIAL_FIELD)
            )
            match = re.search(r"-(\d+)$", material_name)
            if match:
                counters[day_key] = max(counters.get(day_key, 0), int(match.group(1)))
        for day_key, count in counts.items():
            counters[day_key] = max(counters.get(day_key, 0), count)
        return counters

    @staticmethod
    def _datetime_value(value: Any) -> datetime:
        if isinstance(value, (int, float)):
            number = float(value)
            timestamp = number / 1000 if number >= 10_000_000_000 else number
            return datetime.fromtimestamp(timestamp).astimezone()
        text = str(value or "").strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = datetime.now().astimezone()
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed

    @staticmethod
    def _timestamp_ms(value: Any) -> int:
        if isinstance(value, (int, float)):
            number = float(value)
            return int(number if number >= 10_000_000_000 else number * 1000)
        text = str(value or "").strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = datetime.now().astimezone()
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return int(parsed.timestamp() * 1000)

    @staticmethod
    def _attachment_tokens(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [
            str(item.get("file_token") or "")
            for item in value
            if isinstance(item, dict) and item.get("file_token")
        ]

    @staticmethod
    def _cell_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, list):
            return "".join(
                str(part.get("text") or part.get("name") or "")
                if isinstance(part, dict)
                else str(part)
                for part in value
            )
        return str(value)

    @staticmethod
    def _record_id(job_id: str) -> str:
        return f"feishu:{job_id}"

    def _new_record(self, job_id: str, target: dict[str, Any]) -> dict[str, Any]:
        previous = self.get(job_id) or {}
        return {
            "id": self._record_id(job_id),
            "job_id": job_id,
            "provider": "feishu_base",
            "status": "pending",
            "attempts": int(previous.get("attempts", 0)),
            "created_at": previous.get("created_at") or _now(),
            "started_at": None,
            "finished_at": None,
            "error": None,
            "next_retry_at": None,
            "base_url": target.get("base_url", ""),
            "table_id": target.get("table_id", ""),
            "table_name": previous.get("table_name", ""),
            "expected_count": 0,
            "inserted_count": 0,
            "updated_count": 0,
            "verified_count": 0,
        }
