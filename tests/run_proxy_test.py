"""
Integration test that spawns the MCP proxy alongside two fake upstream servers.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "configs" / "config.test.json"


def _send(proc: subprocess.Popen, message: dict) -> None:
    payload = json.dumps(message).encode("utf-8")
    header = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
    assert proc.stdin is not None
    proc.stdin.write(header + payload)
    proc.stdin.flush()
    _debug(f"-> proxy: {message}")


def _read(proc: subprocess.Popen, timeout: float = 5) -> Optional[dict]:
    assert proc.stdout is not None
    start = time.time()
    headers: Dict[str, str] = {}
    buffer = b""
    while True:
        if time.time() - start > timeout:
            raise TimeoutError("Timed out waiting for response headers.")
        chunk = proc.stdout.readline()
        if not chunk:
            return None
        stripped = chunk.strip()
        if not stripped:
            break
        name, value = stripped.decode("ascii").split(":", 1)
        headers[name.lower()] = value.strip()
    length = int(headers.get("content-length", "0"))
    payload = proc.stdout.read(length)
    message = json.loads(payload.decode("utf-8"))
    _debug(f"<- proxy: {message}")
    if message.get("method") and message.get("id"):
        _handle_proxy_request(proc, message)
        return _read(proc, timeout)
    return message


def _handle_proxy_request(proc: subprocess.Popen, message: dict) -> None:
    method = message.get("method")
    msg_id = message.get("id")
    _debug(f"Handling proxy request {method} id={msg_id}")
    if method == "roots/list":
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "result": {"roots": []},
            },
        )
    else:
        _send(
            proc,
            {
                "jsonrpc": "2.0",
                "id": msg_id,
                "error": {"code": -32601, "message": f"Test harness does not handle {method}"},
            },
        )


def _rpc(proc: subprocess.Popen, message_id: int, method: str, params: Optional[dict] = None) -> dict:
    payload = {"jsonrpc": "2.0", "id": message_id, "method": method}
    if params is not None:
        payload["params"] = params
    _send(proc, payload)
    response = _read(proc)
    assert response and response.get("id") == message_id, f"Unexpected response: {response}"
    if "error" in response:
        raise RuntimeError(response["error"])
    return response["result"]


def main() -> None:
    cmd = [sys.executable, "-m", "mcp_proxy.main", "--config", str(CONFIG)]
    proc = subprocess.Popen(
        cmd,
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=False,
    )
    try:
        init = _rpc(proc, 1, "initialize", {"clientInfo": {"name": "test-client", "version": "1.0"}})
        assert init["serverInfo"]["name"] == "mcp-proxy"

        tools = _rpc(proc, 2, "tools/list", {})
        tool_names = sorted(tool["name"] for tool in tools["tools"])
        assert tool_names == [
            "alpha__alpha-echo",
            "alpha__alpha-upper",
            "beta__beta-echo",
            "beta__beta-upper",
            "roots__noop",
        ]

        call = _rpc(
            proc,
            3,
            "tools/call",
            {"name": "beta__beta-upper", "arguments": {"text": "hello"}},
        )
        assert "HELLO" in json.dumps(call)

        resources = _rpc(proc, 4, "resources/list", {})
        for res in resources["resources"]:
            assert res["uri"].startswith("proxy://resource/")

        read = _rpc(proc, 5, "resources/read", {"uri": resources["resources"][0]["uri"]})
        assert "payload from" in json.dumps(read)

        prompts = _rpc(proc, 6, "prompts/list", {})
        assert any(prompt["name"].startswith("alpha__") for prompt in prompts["prompts"])

        prompt_get = _rpc(proc, 7, "prompts/get", {"name": prompts["prompts"][0]["name"]})
        assert "You are" in json.dumps(prompt_get)

        _rpc(proc, 8, "shutdown", {})
        # Ensure the roots server's request was routed and proxy forwarded response
        # by confirming the proxy emitted restart logs if necessary (implicit via no exception)
    finally:
        proc.kill()
        out, err = proc.communicate(timeout=1)
        if err:
            sys.stderr.write(err.decode("utf-8", errors="ignore"))


def _debug(msg: str) -> None:
    sys.stderr.write(f"[test] {msg}\n")


if __name__ == "__main__":
    main()
