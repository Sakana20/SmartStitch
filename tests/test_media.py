from __future__ import annotations

import asyncio
import io

import pytest

import smartstitch.media as media_module
from smartstitch.media import DisconnectSafeFileResponse


class TrackingFile(io.BytesIO):
    def __init__(self, value: bytes):
        super().__init__(value)
        self.was_closed = False

    def close(self) -> None:
        self.was_closed = True
        super().close()


@pytest.mark.parametrize(
    "headers",
    [
        [],
        [(b"range", b"bytes=4-11")],
        [(b"range", b"bytes=0-3,8-11")],
    ],
)
def test_media_response_closes_file_when_client_cancels(tmp_path, monkeypatch, headers):
    source = tmp_path / "preview.mp4"
    source.write_bytes(b"0123456789abcdef")
    opened = TrackingFile(source.read_bytes())
    monkeypatch.setattr(media_module, "_open_binary_file", lambda _path: opened)
    response = DisconnectSafeFileResponse(source)
    response.chunk_size = 4

    async def disconnect_on_body(message):
        if message["type"] == "http.response.body" and message.get("body"):
            raise asyncio.CancelledError

    async def run_response():
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/preview.mp4",
            "headers": headers,
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        }
        with pytest.raises(asyncio.CancelledError):
            await response(scope, None, disconnect_on_body)

    asyncio.run(run_response())

    assert opened.was_closed is True


def test_media_response_preserves_single_range_behavior(tmp_path):
    source = tmp_path / "preview.mp4"
    source.write_bytes(b"0123456789abcdef")
    response = DisconnectSafeFileResponse(source)
    messages = []

    async def collect(message):
        messages.append(message)

    async def run_response():
        scope = {
            "type": "http",
            "method": "GET",
            "path": "/preview.mp4",
            "headers": [(b"range", b"bytes=4-7")],
            "asgi": {"version": "3.0", "spec_version": "2.4"},
        }
        await response(scope, None, collect)

    asyncio.run(run_response())

    start = messages[0]
    headers = dict(start["headers"])
    assert start["status"] == 206
    assert headers[b"content-range"] == b"bytes 4-7/16"
    assert b"".join(message.get("body", b"") for message in messages[1:]) == b"4567"
