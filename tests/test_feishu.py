from __future__ import annotations

import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from smartstitch.api import create_app
from smartstitch.database import SQLiteStore
from smartstitch.feishu import (
    FeishuBaseClient,
    FeishuError,
    FeishuSettingsStore,
    FeishuSyncManager,
    SYNC_FIELDS,
    parse_base_url,
)
from smartstitch.models import FeishuBaseSyncConfig


class FakeFeishuBaseClient:
    def __init__(self):
        self.fields: set[str] = set()
        self.records: list[dict[str, object]] = []

    def list_tables(self, _token):
        return [{"table_id": "tbl123", "name": "成片记录"}]

    def resolve_base_url(self, url):
        return parse_base_url(url)

    def ensure_text_fields(self, _token, _table_id, field_names):
        self.fields.update(field_names)

    def list_records(self, _token, _table_id):
        return self.records

    def batch_create_records(self, _token, _table_id, records):
        for fields in records:
            self.records.append(
                {"record_id": f"rec{len(self.records) + 1}", "fields": dict(fields)}
            )

    def batch_update_records(self, _token, _table_id, records):
        by_id = {str(record["record_id"]): record for record in self.records}
        for record_id, fields in records:
            by_id[record_id]["fields"] = dict(fields)


def make_job() -> dict[str, object]:
    return {
        "id": "a" * 32,
        "short_id": "aaaaaaaa",
        "config_id": "demo",
        "config_name": "测试项目",
        "status": "completed",
        "timeline": ["pool_1"],
        "pool_labels": {"pool_1": "剧情"},
        "started_at": "2026-09-18T10:00:00+08:00",
        "finished_at": "2026-09-18T10:01:00+08:00",
        "feishu_base_sync": {
            "enabled": True,
            "base_url": "https://example.feishu.cn/base/bascDemo?table=tbl123",
            "table_id": "tbl123",
            "trigger": "job_terminal",
            "row_scope": "all_items",
            "write_mode": "upsert",
        },
        "items": [
            {
                "index": 1,
                "status": "succeeded",
                "output_name": "demo-1.mp4",
                "output_path": "/output/demo-1.mp4",
                "estimated_duration": 12.5,
                "actual_duration": 12.4,
                "attempts": 1,
                "error": None,
                "naming": {
                    "product": "红果短剧",
                    "benefit": "功能综述",
                    "talents": ["张三"],
                    "restriction_date": "2026-10-01",
                },
                "selections": {
                    "pool_1": {"name": "a.mp4", "path": "/assets/a.mp4"}
                },
            },
            {
                "index": 2,
                "status": "failed",
                "output_name": "demo-2.mp4",
                "output_path": "/output/demo-2.mp4",
                "estimated_duration": 13.0,
                "actual_duration": None,
                "attempts": 2,
                "error": "ffmpeg failed",
                "naming": None,
                "selections": {
                    "pool_1": {"name": "b.mp4", "path": "/assets/b.mp4"}
                },
            },
        ],
    }


def test_parse_base_url_extracts_base_and_table():
    assert parse_base_url(
        "https://example.feishu.cn/base/bascAbc123?table=tbl001"
    ) == ("bascAbc123", "tbl001")
    assert parse_base_url("https://example.feishu.cn/base/bascAbc123") == (
        "bascAbc123",
        None,
    )
    assert parse_base_url(
        "https://example.feishu.cn/wiki/wikcn123?table=tbl001&view=vew001"
    ) == ("wikcn123", "tbl001")
    with pytest.raises(FeishuError, match="不是飞书多维表格"):
        parse_base_url("https://example.feishu.cn/sheets/sht123")


def test_enabled_sync_config_requires_a_direct_base_link():
    with pytest.raises(ValueError, match="/base/ 或 /wiki/"):
        FeishuBaseSyncConfig(
            enabled=True,
            base_url="javascript:alert(1)",
            table_id="tbl123",
        )


def test_base_client_uses_v3_field_and_record_contracts(monkeypatch):
    client = FeishuBaseClient("cli_demo", "secret")
    calls = []

    def request(method, path, payload=None):
        calls.append((method, path, payload))
        if method == "GET":
            return {"items": [], "total": 0}
        return {}

    monkeypatch.setattr(client, "_request", request)
    assert client.list_tables("basc123") == []
    client.ensure_text_fields("basc123", "tbl123", ["输出文件名"])
    client.batch_create_records("basc123", "tbl123", [{"输出文件名": "a.mp4"}])
    client.batch_update_records(
        "basc123", "tbl123", [("rec123", {"输出文件名": "b.mp4"})]
    )

    assert calls[0][1].startswith("/base/v3/bases/basc123/tables?")
    assert calls[2] == (
        "POST",
        "/base/v3/bases/basc123/tables/tbl123/fields",
        {"name": "输出文件名", "type": "text"},
    )
    assert calls[3][2] == {"create_records": [{"输出文件名": "a.mp4"}]}
    assert calls[4][2] == {
        "update_records": {"rec123": {"输出文件名": "b.mp4"}}
    }


def test_base_client_resolves_wiki_wrapper_to_base_token(monkeypatch):
    client = FeishuBaseClient("cli_demo", "secret")

    def request(method, path, payload=None):
        assert method == "GET"
        assert path == "/wiki/v2/spaces/node_by_token?token=wikcn123"
        assert payload is None
        return {"node": {"obj_type": "bitable", "obj_token": "basc123"}}

    monkeypatch.setattr(client, "_request", request)
    assert client.resolve_base_url(
        "https://example.feishu.cn/wiki/wikcn123?table=tbl123&view=vew123"
    ) == ("basc123", "tbl123")


def test_settings_store_never_returns_secret_and_uses_private_permissions(tmp_path):
    store = FeishuSettingsStore(tmp_path)
    public = store.update("cli_demo", "secret-value")

    assert public == {"app_id": "cli_demo", "app_secret_configured": True}
    assert "secret-value" not in str(public)
    assert store.credentials() == ("cli_demo", "secret-value")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600

    store.update("cli_updated", "")
    assert store.credentials() == ("cli_updated", "secret-value")


def test_sync_manager_upserts_and_verifies_rows(tmp_path):
    job = make_job()
    fake_client = FakeFeishuBaseClient()
    settings = FeishuSettingsStore(tmp_path)
    settings.update("cli_demo", "secret-value")
    manager = FeishuSyncManager(
        SQLiteStore(tmp_path / "smartstitch.db"),
        settings,
        lambda _job_id: job,
        client_factory=lambda _app_id, _app_secret: fake_client,
    )

    manager.start(str(job["id"]))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = manager.get(str(job["id"]))
        if record and record["status"] in {"succeeded", "failed"}:
            break
        time.sleep(0.01)

    assert record["status"] == "succeeded"
    assert record["inserted_count"] == 2
    assert fake_client.fields == set(SYNC_FIELDS)
    assert len(fake_client.records) == 2

    job["items"][0]["actual_duration"] = 15.0
    manager.start(str(job["id"]))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = manager.get(str(job["id"]))
        if record and record["status"] == "succeeded" and record["attempts"] == 2:
            break
        time.sleep(0.01)

    assert record["updated_count"] == 2
    assert record["inserted_count"] == 0
    assert len(fake_client.records) == 2
    assert fake_client.records[0]["fields"]["实际时长"] == "15.0"


def test_sync_manager_retries_transient_errors(tmp_path):
    job = make_job()
    fake_client = FakeFeishuBaseClient()
    calls = 0

    class FlakyClient:
        def resolve_base_url(self, url):
            return fake_client.resolve_base_url(url)

        def list_tables(self, token):
            nonlocal calls
            calls += 1
            if calls == 1:
                raise FeishuError("temporary outage", retryable=True)
            return fake_client.list_tables(token)

        def ensure_text_fields(self, *args):
            return fake_client.ensure_text_fields(*args)

        def list_records(self, *args):
            return fake_client.list_records(*args)

        def batch_create_records(self, *args):
            return fake_client.batch_create_records(*args)

        def batch_update_records(self, *args):
            return fake_client.batch_update_records(*args)

    settings = FeishuSettingsStore(tmp_path)
    settings.update("cli_demo", "secret-value")
    manager = FeishuSyncManager(
        SQLiteStore(tmp_path / "retry.db"),
        settings,
        lambda _job_id: job,
        client_factory=lambda _app_id, _app_secret: FlakyClient(),
        retry_delays=(0.01,),
    )

    manager.start(str(job["id"]))
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        record = manager.get(str(job["id"]))
        if record and record["status"] == "succeeded":
            break
        time.sleep(0.01)

    assert record["status"] == "succeeded"
    assert record["attempts"] == 2
    assert calls == 2


def test_feishu_settings_and_connection_api_are_secret_safe(tmp_path):
    (tmp_path / "config").mkdir()
    app = create_app(tmp_path)
    fake_client = FakeFeishuBaseClient()
    app.state.feishu_client_factory = lambda _app_id, _app_secret: fake_client
    client = TestClient(app)

    saved = client.put(
        "/api/v1/integrations/feishu/settings",
        json={"app_id": "cli_demo", "app_secret": "secret-value"},
    )
    assert saved.status_code == 200
    assert saved.json() == {"app_id": "cli_demo", "app_secret_configured": True}
    assert "secret-value" not in client.get(
        "/api/v1/integrations/feishu/settings"
    ).text

    tested = client.post(
        "/api/v1/integrations/feishu/test",
        json={
            "base_url": "https://example.feishu.cn/base/bascDemo?table=tbl123"
        },
    )
    assert tested.status_code == 200
    assert tested.json()["selected_table_id"] == "tbl123"
    assert tested.json()["tables"][0]["name"] == "成片记录"
