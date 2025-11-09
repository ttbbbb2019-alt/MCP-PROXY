"""
Minimal but fully functional MCP server for local testing and demos.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Dict, Optional


def _read_message() -> Optional[dict]:
    headers: Dict[str, str] = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            return None
        stripped = line.strip()
        if not stripped:
            break
        name, value = stripped.decode("ascii").split(":", 1)
        headers[name.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    payload = sys.stdin.buffer.read(length)
    return json.loads(payload.decode("utf-8"))


def _send_message(message: dict) -> None:
    payload = json.dumps(message, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
    sys.stdout.buffer.write(header + payload)
    sys.stdout.buffer.flush()


def _tools_list_payload(name: str) -> dict:
    return {
        "tools": [
            {
                "name": f"{name}-echo",
                "description": f"Echo tool from {name}",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
            {
                "name": f"{name}-upper",
                "description": f"Uppercases text via {name}",
                "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}},
            },
        ]
    }


def _resources_payload(name: str) -> dict:
    return {
        "resources": [
            {
                "uri": f"file://{name}/data.txt",
                "name": f"{name}-data",
                "description": "Fake resource",
                "mimeType": "text/plain",
            }
        ]
    }


def _prompts_payload(name: str) -> dict:
    return {
        "prompts": [
            {
                "name": f"{name}-prompt",
                "description": "Sample prompt",
            }
        ]
    }


def _prompt_messages(name: str) -> dict:
    return {
        "messages": [
            {
                "role": "system",
                "content": [{"type": "text", "text": f"You are {name} test server."}],
            }
        ]
    }


def _handle_call(method: str, params: Dict[str, Any], name: str) -> dict:
    if method == "tools/call":
        tool_name = params.get("name")
        text = params.get("arguments", {}).get("text", "")
        if tool_name == f"{name}-upper":
            text = text.upper()
        return {
            "content": [
                {"type": "text", "text": f"{name} handled {tool_name} with {text}"},
            ]
        }
    if method == "resources/read":
        uri = params.get("uri", "")
        return {
            "contents": [
                {
                    "uri": uri,
                    "text": f"payload from {name} ({uri})",
                }
            ]
        }
    if method == "prompts/get":
        return _prompt_messages(name)
    raise ValueError(f"Unsupported method {method}")


def main() -> None:
    if len(sys.argv) < 2:
        print("Usage: minimal_server.py <name>", file=sys.stderr)
        sys.exit(1)
    name = sys.argv[1]
    while True:
        message = _read_message()
        if message is None:
            break
        method = message.get("method")
        message_id = message.get("id")
        params = message.get("params") or {}

        if method == "initialize":
            result = {
                "serverInfo": {"name": f"minimal-{name}", "version": "0.0.1"},
                "capabilities": {
                    "tools": {"list": True, "call": True},
                    "resources": {"list": True, "read": True, "templates": {"list": True}},
                    "prompts": {"list": True, "get": True},
                    "logging": {"setLevel": True},
                },
            }
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": result})
        elif method == "shutdown":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": {}})
            break
        elif method == "tools/list":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": _tools_list_payload(name)})
        elif method == "tools/call":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": _handle_call("tools/call", params, name)})
        elif method == "resources/list":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": _resources_payload(name)})
        elif method == "resources/read":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": _handle_call("resources/read", params, name)})
        elif method == "resources/templates/list":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": {"resourceTemplates": []}})
        elif method == "prompts/list":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": _prompts_payload(name)})
        elif method == "prompts/get":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": _handle_call("prompts/get", params, name)})
        elif method == "logging/setLevel":
            _send_message({"jsonrpc": "2.0", "id": message_id, "result": {"level": params.get("level")}})
        else:
            _send_message(
                {
                    "jsonrpc": "2.0",
                    "id": message_id,
                    "error": {"code": -32601, "message": f"{name} does not implement {method}"},
                }
            )


if __name__ == "__main__":
    main()

