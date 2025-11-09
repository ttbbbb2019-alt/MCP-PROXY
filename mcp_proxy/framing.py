from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Optional

_LOGGER = logging.getLogger(__name__)


class JsonRpcStream:
    """
    Handles JSON-RPC messages transported over LSP-style Content-Length frames.
    """

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, name: str, prefer_newline: bool = False):
        self._reader = reader
        self._writer = writer
        self._name = name
        self._write_lock = asyncio.Lock()
        self._use_newline_protocol = prefer_newline

    async def read_message(self) -> Optional[dict]:
        """
        Read the next JSON-RPC message. Supports both Content-Length framing
        and newline-delimited JSON used by some MCP clients.
        """

        try:
            while True:
                first_line = await self._reader.readline()
                if not first_line:
                    return None
                if not first_line.strip():
                    # Skip stray blank lines between frames.
                    continue
                stripped = first_line.lstrip()
                if stripped.startswith(b"{") or stripped.startswith(b"["):
                    # Newline-delimited JSON payload.
                    self._use_newline_protocol = True
                    return json.loads(first_line.decode("utf-8"))
                self._use_newline_protocol = False
                headers = await self._read_headers(first_line)
                if headers is None:
                    return None
                length = int(headers.get("content-length", "0"))
                payload = await self._reader.readexactly(length)
                return json.loads(payload.decode("utf-8"))
        except asyncio.IncompleteReadError:
            _LOGGER.debug("%s stream closed while reading payload", self._name)
            return None

    async def send_message(self, message: dict) -> None:
        """
        Serialize and send a JSON-RPC message to the writer.
        """

        data = json.dumps(message, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if self._use_newline_protocol:
            payload = data + b"\n"
        else:
            header = f"Content-Length: {len(data)}\r\n\r\n".encode("ascii")
            payload = header + data
        async with self._write_lock:
            self._writer.write(payload)
            await self._writer.drain()

    async def _read_headers(self, first_line: bytes) -> Optional[dict]:
        """
        Parse Content-Length style headers from the stream.
        """

        headers = {}
        line = first_line
        while True:
            stripped = line.strip()
            if not stripped:
                break
            try:
                name, value = stripped.decode("ascii").split(":", 1)
            except ValueError:
                _LOGGER.warning("Malformed header line from %s: %r", self._name, stripped)
                continue
            headers[name.lower()] = value.strip()
            line = await self._reader.readline()
            if not line:
                # EOF before blank line terminator.
                return None
        return headers
