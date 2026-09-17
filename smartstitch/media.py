from __future__ import annotations

from pathlib import Path
from secrets import token_hex
from typing import BinaryIO

import anyio
from starlette.datastructures import MutableHeaders
from starlette.responses import FileResponse
from starlette.types import Send


def _open_binary_file(path: str | Path) -> BinaryIO:
    return Path(path).open("rb")


class DisconnectSafeFileResponse(FileResponse):
    """A range-capable file response that closes files on client cancellation.

    Browser video seeks routinely cancel an in-flight range response.  Closing via
    an async context manager can itself be cancelled, leaving the underlying file
    descriptor open.  Reads remain off the event loop, while close is deliberately
    synchronous and unconditional.
    """

    async def _open_file(self) -> BinaryIO:
        return await anyio.to_thread.run_sync(_open_binary_file, self.path)

    @staticmethod
    async def _seek(file: BinaryIO, offset: int) -> None:
        await anyio.to_thread.run_sync(file.seek, offset)

    @staticmethod
    async def _read(file: BinaryIO, size: int) -> bytes:
        return await anyio.to_thread.run_sync(file.read, size)

    async def _handle_simple(
        self,
        send: Send,
        send_header_only: bool,
        send_pathsend: bool,
    ) -> None:
        del send_pathsend
        await send(
            {
                "type": "http.response.start",
                "status": self.status_code,
                "headers": self.raw_headers,
            }
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        file = await self._open_file()
        try:
            more_body = True
            while more_body:
                chunk = await self._read(file, self.chunk_size)
                more_body = len(chunk) == self.chunk_size
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": more_body,
                    }
                )
        finally:
            file.close()

    async def _handle_single_range(
        self,
        send: Send,
        start: int,
        end: int,
        file_size: int,
        send_header_only: bool,
    ) -> None:
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-range"] = f"bytes {start}-{end - 1}/{file_size}"
        headers["content-length"] = str(end - start)
        await send(
            {
                "type": "http.response.start",
                "status": 206,
                "headers": headers.raw,
            }
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        file = await self._open_file()
        try:
            await self._seek(file, start)
            more_body = True
            while more_body:
                chunk = await self._read(file, min(self.chunk_size, end - start))
                start += len(chunk)
                more_body = len(chunk) == self.chunk_size and start < end
                await send(
                    {
                        "type": "http.response.body",
                        "body": chunk,
                        "more_body": more_body,
                    }
                )
        finally:
            file.close()

    async def _handle_multiple_ranges(
        self,
        send: Send,
        ranges: list[tuple[int, int]],
        file_size: int,
        send_header_only: bool,
    ) -> None:
        boundary = token_hex(13)
        content_length, header_generator = self.generate_multipart(
            ranges, boundary, file_size, self.headers["content-type"]
        )
        headers = MutableHeaders(raw=list(self.raw_headers))
        headers["content-type"] = f"multipart/byteranges; boundary={boundary}"
        headers["content-length"] = str(content_length)
        await send(
            {
                "type": "http.response.start",
                "status": 206,
                "headers": headers.raw,
            }
        )
        if send_header_only:
            await send({"type": "http.response.body", "body": b"", "more_body": False})
            return

        file = await self._open_file()
        try:
            for start, end in ranges:
                await send(
                    {
                        "type": "http.response.body",
                        "body": header_generator(start, end),
                        "more_body": True,
                    }
                )
                await self._seek(file, start)
                while start < end:
                    chunk = await self._read(file, min(self.chunk_size, end - start))
                    start += len(chunk)
                    await send(
                        {
                            "type": "http.response.body",
                            "body": chunk,
                            "more_body": True,
                        }
                    )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b"\r\n",
                        "more_body": True,
                    }
                )
            await send(
                {
                    "type": "http.response.body",
                    "body": f"--{boundary}--".encode("latin-1"),
                    "more_body": False,
                }
            )
        finally:
            file.close()
