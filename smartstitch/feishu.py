from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml

from .database import SQLiteStore


FEISHU_API_BASE = "https://open.feishu.cn/open-apis"
SYNC_FIELDS = [
    "_smartstitch_key",
    "同步时间",
    "批次 ID",
    "批次短 ID",
    "项目 ID",
    "项目名称",
    "任务序号",
    "状态",
    "输出文件名",
    "输出路径",
    "产品",
    "利益点",
    "达人",
    "限制日期",
    "素材组合",
    "预计时长",
    "实际时长",
    "尝试次数",
    "错误",
    "任务开始时间",
    "任务结束时间",
]


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class FeishuError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        code: int | str | None = None,
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.retryable = retryable


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
    if "base" not in parts or parts.index("base") + 1 >= len(parts):
        raise FeishuError("请使用包含 /base/ 的飞书多维表格直链")
    token = parts[parts.index("base") + 1]
    table_id = urllib.parse.parse_qs(parsed.query).get("table", [None])[0]
    return token, table_id


class FeishuBaseClient:
    def __init__(
        self,
        app_id: str,
        app_secret: str,
        *,
        opener: Callable[..., Any] | None = None,
        timeout: float = 15,
    ):
        self.app_id = app_id
        self.app_secret = app_secret
        self.opener = opener or urllib.request.urlopen
        self.timeout = timeout
        self._token = ""
        self._token_expires_at = 0.0
        self._lock = threading.RLock()

    def list_tables(self, base_token: str) -> list[dict[str, object]]:
        items = self._list_all(f"/base/v3/bases/{base_token}/tables", 300)
        return [
            {
                "table_id": str(item.get("table_id") or item.get("id") or ""),
                "name": str(item.get("name") or ""),
            }
            for item in items
        ]

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

    def _list_all(self, path: str, page_size: int) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        offset = 0
        while True:
            query = urllib.parse.urlencode({"limit": page_size, "offset": offset})
            data = self._request("GET", f"{path}?{query}")
            page_items = data.get("items") or []
            if not isinstance(page_items, list):
                raise FeishuError("飞书多维表格返回的数据格式不正确")
            items.extend(item for item in page_items if isinstance(item, dict))
            offset += len(page_items)
            total = data.get("total")
            if not page_items or len(page_items) < page_size:
                return items
            if isinstance(total, int) and offset >= total:
                return items

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
        try:
            response = self.opener(request, timeout=self.timeout)
            with response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            try:
                error_payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                error_payload = {}
            message = error_payload.get("msg") or error_payload.get("message") or str(exc)
            raise FeishuError(
                f"飞书请求失败: {message}",
                code=error_payload.get("code"),
                retryable=exc.code == 429 or exc.code >= 500,
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
            raise FeishuError(
                f"飞书请求失败: {message}",
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
        self.running: set[str] = set()
        self.retry_timers: dict[str, threading.Timer] = {}
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
            thread.start()
            return record

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
            base_token, linked_table_id = parse_base_url(target["base_url"])
            table_id = str(target.get("table_id") or linked_table_id or "")
            tables = client.list_tables(base_token)
            selected = next(
                (table for table in tables if table["table_id"] == table_id), None
            )
            if selected is None:
                raise FeishuError("配置的飞书多维表格数据表不存在，请重新测试连接")
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

    def _run_scheduled_retry(self, job_id: str) -> None:
        with self.lock:
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
        rows = [self._build_fields(job, item) for item in items]
        client.ensure_text_fields(base_token, table_id, SYNC_FIELDS)
        existing = client.list_records(base_token, table_id)
        key_records = {
            self._cell_text((record.get("fields") or {}).get("_smartstitch_key")): str(
                record.get("record_id") or ""
            )
            for record in existing
            if isinstance(record.get("fields"), dict)
            and self._cell_text((record.get("fields") or {}).get("_smartstitch_key"))
        }
        updates: list[tuple[str, dict[str, Any]]] = []
        additions: list[dict[str, Any]] = []
        for fields in rows:
            record_id = key_records.get(str(fields["_smartstitch_key"]))
            if record_id:
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
    def _build_fields(job: dict[str, Any], item: dict[str, Any]) -> dict[str, str]:
        naming = item.get("naming") or {}
        selections = []
        labels = job.get("pool_labels") or {}
        for category in job.get("timeline") or item.get("selections", {}).keys():
            asset = (item.get("selections") or {}).get(category)
            if asset:
                selections.append(f"{labels.get(category, category)}：{asset.get('name') or Path(asset['path']).name}")
        values = [
            f"{job['id']}:{item['index']}", _now(), job["id"],
            job.get("short_id", ""), job.get("config_id", ""),
            job.get("config_name", ""), item.get("index"), item.get("status", ""),
            item.get("output_name", ""), item.get("output_path", ""),
            naming.get("product", ""), naming.get("benefit", ""),
            "+".join(naming.get("talents") or []),
            naming.get("restriction_date", ""), " | ".join(selections),
            item.get("estimated_duration"), item.get("actual_duration"),
            item.get("attempts", 0), item.get("error") or "",
            job.get("started_at") or "", job.get("finished_at") or "",
        ]
        return {
            field_name: "" if value is None else str(value)
            for field_name, value in zip(SYNC_FIELDS, values, strict=True)
        }

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
