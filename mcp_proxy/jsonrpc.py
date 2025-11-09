from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional


@dataclass
class JsonRpcError(Exception):
    code: int
    message: str
    data: Optional[Any] = None

    def to_response(self, message_id: Any) -> dict:
        payload = {"jsonrpc": "2.0", "id": message_id, "error": {"code": self.code, "message": self.message}}
        if self.data is not None:
            payload["error"]["data"] = self.data
        return payload


def make_error_response(message_id: Any, code: int, message: str, data: Optional[Any] = None) -> dict:
    err = {"jsonrpc": "2.0", "id": message_id, "error": {"code": code, "message": message}}
    if data is not None:
        err["error"]["data"] = data
    return err


def make_result_response(message_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": message_id, "result": result}


def is_response(message: dict) -> bool:
    return "id" in message and ("result" in message or "error" in message)


def is_request(message: dict) -> bool:
    return "method" in message and "id" in message and "jsonrpc" in message


def is_notification(message: dict) -> bool:
    return "method" in message and "id" not in message

