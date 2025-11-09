from __future__ import annotations

import json
import sys
from typing import Dict, Optional

CLIENT_ID = "roots"


def _read_message() -> Optional[dict]:
    headers: Dict[str, str] = {}
    while True:
        line = sys.stdin.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            break
        name, value = stripped.split(":", 1)
        headers[name.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    payload = sys.stdin.read(length)
    return json.loads(payload)


def _send(message: dict) -> None:
    data = json.dumps(message).encode()
    header = f"Content-Length: {len(data)}\r\n\r\n".encode()
    sys.stdout.buffer.write(header + data)
    sys.stdout.buffer.flush()


def main() -> None:
    initialized = False
    while True:
        message = _read_message()
        if message is None:
            break
        method = message.get("method")
        msg_id = message.get("id")
        params = message.get("params") or {}
        if method == "initialize":
            result = {
                "serverInfo": {"name": "roots-server", "version": "0.0.1"},
                "capabilities": {
                    "tools": {"list": True, "call": True},
                    "roots": {"listChanged": True},
                    "prompts": {"list": True, "get": True},
                },
            }
            _send({"jsonrpc": "2.0", "id": msg_id, "result": result})
            if not initialized:
                initialized = True
                _send(
                    {
                        "jsonrpc": "2.0",
                        "id": 100,
                        "method": "roots/list",
                    }
                )
        elif method == "tools/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"tools": [{"name": "noop", "inputSchema": {"type": "object"}}]},
                }
            )
        elif method == "tools/call":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"content": [{"type": "text", "text": "noop"}]},
                }
            )
        elif method == "resources/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"resources": []},
                }
            )
        elif method == "resources/read":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"contents": []},
                }
            )
        elif method == "prompts/list":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"prompts": []},
                }
            )
        elif method == "prompts/get":
            _send(
                {
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {"messages": []},
                }
            )
        elif method == "shutdown":
            _send({"jsonrpc": "2.0", "id": msg_id, "result": {}})
            break


if __name__ == "__main__":
    main()
