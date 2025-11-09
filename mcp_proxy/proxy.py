from __future__ import annotations

import asyncio
import base64
import json
import logging
from copy import deepcopy
from typing import Any, Dict, List, Optional, Tuple

from .config import ProxyConfig
from .framing import JsonRpcStream
from .jsonrpc import JsonRpcError, is_notification, is_request, is_response, make_error_response, make_result_response
from .upstream import UpstreamServer
from .auth import AuthManager
from .ratelimit import RateLimiter

PROXY_NAME = "mcp-proxy"
PROXY_VERSION = "0.1.0"
TOOL_NAME_SEPARATOR = "::"
SAFE_TOOL_SEPARATOR = "__"
RESOURCE_SCHEME = "proxy://resource/"

_LOGGER = logging.getLogger(__name__)


class ProxyRouter:
    """
    Orchestrates all traffic between a single MCP client and multiple downstream servers.
    """

    def __init__(self, config: ProxyConfig, client_stream: JsonRpcStream):
        self.config = config
        self.client_stream = client_stream
        self.servers: Dict[str, UpstreamServer] = {
            server_cfg.id: UpstreamServer(server_cfg, proxy=self) for server_cfg in config.servers
        }
        self.auth_manager = AuthManager(config.auth_token)
        self.rate_limiter = RateLimiter(config.rate_limit_per_minute)
        self.tool_registry: Dict[str, Tuple[str, str]] = {}
        self.prompt_registry: Dict[str, Tuple[str, str]] = {}
        self.resource_registry: Dict[str, Tuple[str, str]] = {}
        self.server_request_router: Dict[str, Tuple[str, int]] = {}
        self._client_request_counter = 0
        self._initialized = False

    async def serve(self) -> None:
        """
        Main receive loop for the upstream client connection.
        """

        while True:
            message = await self.client_stream.read_message()
            if message is None:
                _LOGGER.info("Client connection closed")
                await self._shutdown_servers()
                return
            await self._handle_client_message(message)

    async def _handle_client_message(self, message: dict) -> None:
        _LOGGER.debug("Received client message: %s", message)
        if is_request(message):
            if not await self._ensure_authorized(message):
                return
            method = message["method"]
            handler = {
                "initialize": self._handle_initialize,
                "shutdown": self._handle_shutdown,
                "ping": self._handle_ping,
                "tools/list": self._handle_tools_list,
                "tools/call": self._handle_tools_call,
                "resources/list": self._handle_resources_list,
                "resources/read": self._handle_resources_read,
                "resources/templates/list": self._handle_resource_templates_list,
                "prompts/list": self._handle_prompts_list,
                "prompts/get": self._handle_prompts_get,
                "logging/setLevel": self._handle_logging_set_level,
            }.get(method)
            if handler:
                await handler(message)
            else:
                await self._send_error(
                    message["id"],
                    code=-32601,
                    text=f"Method {method} is not supported by {PROXY_NAME}",
                )
        elif is_response(message):
            await self._forward_client_response(message)
        elif is_notification(message):
            await self._handle_client_notification(message)
        else:
            _LOGGER.debug("Ignoring unknown client payload: %s", message)

    async def forward_server_notification(self, server: UpstreamServer, message: dict) -> None:
        """Relay a notification originating from an upstream server to the client."""
        payload = deepcopy(message)
        params = payload.get("params") or {}
        params = dict(params)
        params.setdefault("proxy", {})["server"] = server.alias
        payload["params"] = params
        await self.client_stream.send_message(payload)

    async def forward_server_request(self, server: UpstreamServer, message: dict) -> None:
        """Assign a proxy-scoped id to the upstream request and forward it to the client."""
        method = message.get("method")
        if not self._initialized and method == "roots/list":
            _LOGGER.debug("Serving %s pre-initialize roots/list with empty result", server.alias)
            await server.send_raw(make_result_response(message["id"], {"roots": []}))
            return
        self._client_request_counter += 1
        client_id = f"{server.alias}:{self._client_request_counter}"
        upstream_id = message["id"]
        self.server_request_router[client_id] = (server.alias, upstream_id)
        _LOGGER.debug("Forwarding %s request %s to client as %s", server.alias, message.get("method"), client_id)
        payload = deepcopy(message)
        payload["id"] = client_id
        params = payload.get("params") or {}
        params = dict(params)
        params.setdefault("proxy", {})["server"] = server.alias
        payload["params"] = params
        await self.client_stream.send_message(payload)

    async def _forward_client_response(self, message: dict) -> None:
        route = self.server_request_router.pop(message["id"], None)
        if not route:
            _LOGGER.debug("Received client response for unknown request id %s", message["id"])
            return
        alias, upstream_id = route
        _LOGGER.debug("Routing client response %s back to %s upstream id %s", message["id"], alias, upstream_id)
        server = self.servers.get(alias)
        if not server:
            _LOGGER.warning("Server %s no longer registered for response routing", alias)
            return
        outbound = deepcopy(message)
        outbound["id"] = upstream_id
        await server.send_raw(outbound)

    async def _handle_client_notification(self, message: dict) -> None:
        method = message["method"]
        params = message.get("params")
        # Broadcast client notifications to all downstream servers to keep them informed.
        await asyncio.gather(
            *[
                server.send_raw({"jsonrpc": "2.0", "method": method, "params": params})
                for server in self.servers.values()
            ],
            return_exceptions=True,
        )

    async def _handle_initialize(self, message: dict) -> None:
        params = message.get("params") or {}
        await asyncio.gather(*(server.ensure_started() for server in self.servers.values()))
        await asyncio.gather(*(server.initialize(params) for server in self.servers.values()))
        _LOGGER.debug("All upstream servers initialized")
        self._initialized = True
        capabilities = self._aggregate_capabilities()
        protocol_version = params.get("protocolVersion", "2025-06-18")
        result = {
            "serverInfo": {"name": PROXY_NAME, "version": PROXY_VERSION},
            "capabilities": capabilities,
            "protocolVersion": protocol_version,
        }
        await self.client_stream.send_message(make_result_response(message["id"], result))
        _LOGGER.debug("Sent initialize response to client")

    async def _handle_shutdown(self, message: dict) -> None:
        await self._shutdown_servers()
        await self.client_stream.send_message(make_result_response(message["id"], {}))

    async def _handle_ping(self, message: dict) -> None:
        await self.client_stream.send_message(make_result_response(message["id"], {"ok": True}))

    async def _handle_logging_set_level(self, message: dict) -> None:
        params = message.get("params") or {}
        level = params.get("level") or params.get("logLevel")
        if level:
            logging.getLogger().setLevel(level.upper())
        await self.client_stream.send_message(make_result_response(message["id"], {}))

    async def _handle_tools_list(self, message: dict) -> None:
        params = message.get("params") or {}
        aggregated: List[dict] = []
        for server in self.servers.values():
            try:
                result = await server.request("tools/list", params, timeout=self.config.response_timeout)
            except JsonRpcError as exc:
                _LOGGER.warning("tools/list failed for %s: %s", server.alias, exc)
                continue
            for tool in self._extract_sequence(result, "tools"):
                aggregated.append(self._wrap_tool_descriptor(server.alias, tool))
        page, next_cursor = self._apply_cursor(aggregated, params)
        response = {"tools": page}
        if next_cursor is not None:
            response["nextCursor"] = next_cursor
        await self.client_stream.send_message(make_result_response(message["id"], response))

    async def _handle_tools_call(self, message: dict) -> None:
        params = message.get("params") or {}
        tool_name = params.get("name") or params.get("toolName")
        if not isinstance(tool_name, str):
            await self._send_error(message["id"], -32602, "tools/call requires a tool name.")
            return
        try:
            alias, raw_name = self._resolve_tool_name(tool_name)
        except JsonRpcError as exc:
            await self._send_error(message["id"], exc.code, exc.message, exc.data)
            return
        server = self.servers.get(alias)
        if not server:
            await self._send_error(message["id"], -32602, f"Unknown tool namespace {alias}.")
            return
        forward_params = dict(params)
        forward_params["name"] = raw_name
        try:
            result = await server.request("tools/call", forward_params, timeout=self.config.response_timeout)
        except JsonRpcError as exc:
            await self._send_error(message["id"], exc.code, exc.message, exc.data)
            return
        await self.client_stream.send_message(make_result_response(message["id"], result))

    async def _handle_resources_list(self, message: dict) -> None:
        params = message.get("params") or {}
        aggregated: List[dict] = []
        for server in self.servers.values():
            try:
                result = await server.request("resources/list", params, timeout=self.config.response_timeout)
            except JsonRpcError as exc:
                _LOGGER.warning("resources/list failed for %s: %s", server.alias, exc)
                continue
            for resource in self._extract_sequence(result, "resources"):
                aggregated.append(self._wrap_resource_descriptor(server.alias, resource))
        page, next_cursor = self._apply_cursor(aggregated, params)
        response = {"resources": page}
        if next_cursor is not None:
            response["nextCursor"] = next_cursor
        await self.client_stream.send_message(make_result_response(message["id"], response))

    async def _handle_resources_read(self, message: dict) -> None:
        params = message.get("params") or {}
        uri = params.get("uri")
        if not isinstance(uri, str):
            await self._send_error(message["id"], -32602, "resources/read requires a uri.")
            return
        try:
            alias, upstream_uri = self._resolve_resource_uri(uri)
        except JsonRpcError as exc:
            await self._send_error(message["id"], exc.code, exc.message, exc.data)
            return
        server = self.servers.get(alias)
        if not server:
            await self._send_error(message["id"], -32602, f"Resource belongs to unknown server {alias}.")
            return
        forward_params = dict(params)
        forward_params["uri"] = upstream_uri
        try:
            result = await server.request("resources/read", forward_params, timeout=self.config.response_timeout)
        except JsonRpcError as exc:
            await self._send_error(message["id"], exc.code, exc.message, exc.data)
            return
        await self.client_stream.send_message(make_result_response(message["id"], result))

    async def _handle_resource_templates_list(self, message: dict) -> None:
        params = message.get("params") or {}
        aggregated: List[dict] = []
        for server in self.servers.values():
            try:
                result = await server.request(
                    "resources/templates/list", params, timeout=self.config.response_timeout
                )
            except JsonRpcError as exc:
                _LOGGER.warning("resources/templates/list failed for %s: %s", server.alias, exc)
                continue
            for template in self._extract_sequence(result, "resourceTemplates"):
                aggregated.append(self._wrap_resource_template(server.alias, template))
        page, next_cursor = self._apply_cursor(aggregated, params)
        response = {"resourceTemplates": page}
        if next_cursor is not None:
            response["nextCursor"] = next_cursor
        await self.client_stream.send_message(make_result_response(message["id"], response))

    async def _handle_prompts_list(self, message: dict) -> None:
        params = message.get("params") or {}
        aggregated: List[dict] = []
        for server in self.servers.values():
            try:
                result = await server.request("prompts/list", params, timeout=self.config.response_timeout)
            except JsonRpcError as exc:
                _LOGGER.warning("prompts/list failed for %s: %s", server.alias, exc)
                continue
            for prompt in self._extract_sequence(result, "prompts"):
                aggregated.append(self._wrap_prompt_descriptor(server.alias, prompt))
        page, next_cursor = self._apply_cursor(aggregated, params)
        response = {"prompts": page}
        if next_cursor is not None:
            response["nextCursor"] = next_cursor
        await self.client_stream.send_message(make_result_response(message["id"], response))

    async def _handle_prompts_get(self, message: dict) -> None:
        params = message.get("params") or {}
        prompt_name = params.get("name") or params.get("promptName")
        if not isinstance(prompt_name, str):
            await self._send_error(message["id"], -32602, "prompts/get requires a prompt name.")
            return
        try:
            alias, raw_name = self._resolve_prompt_name(prompt_name)
        except JsonRpcError as exc:
            await self._send_error(message["id"], exc.code, exc.message, exc.data)
            return
        server = self.servers.get(alias)
        if not server:
            await self._send_error(message["id"], -32602, f"Unknown prompt namespace {alias}.")
            return
        forward_params = dict(params)
        forward_params["name"] = raw_name
        try:
            result = await server.request("prompts/get", forward_params, timeout=self.config.response_timeout)
        except JsonRpcError as exc:
            await self._send_error(message["id"], exc.code, exc.message, exc.data)
            return
        await self.client_stream.send_message(make_result_response(message["id"], result))

    async def _send_error(self, message_id: Any, code: int, text: str, data: Optional[Any] = None) -> None:
        await self.client_stream.send_message(make_error_response(message_id, code, text, data))

    def _aggregate_capabilities(self) -> dict:
        capabilities: Dict[str, Any] = {}
        if any((server.initialize_result or {}).get("capabilities", {}).get("tools") for server in self.servers.values()):
            capabilities["tools"] = {"list": True, "call": True}
        if any(
            (server.initialize_result or {}).get("capabilities", {}).get("resources")
            for server in self.servers.values()
        ):
            capabilities["resources"] = {"list": True, "read": True, "templates": {"list": True}}
        if any(
            (server.initialize_result or {}).get("capabilities", {}).get("prompts")
            for server in self.servers.values()
        ):
            capabilities["prompts"] = {"list": True, "get": True}
        capabilities.setdefault("logging", {"setLevel": True})
        return capabilities

    async def _shutdown_servers(self) -> None:
        await asyncio.gather(*(server.shutdown() for server in self.servers.values()), return_exceptions=True)

    def _extract_sequence(self, result: Any, key: str) -> List[dict]:
        if not result:
            return []
        if isinstance(result, dict):
            if key in result and isinstance(result[key], list):
                return [deepcopy(item) for item in result[key]]
            if "data" in result and isinstance(result["data"], list):
                return [deepcopy(item) for item in result["data"]]
        if isinstance(result, list):
            return [deepcopy(item) for item in result]
        return []

    def _apply_cursor(self, items: List[dict], params: dict) -> Tuple[List[dict], Optional[str]]:
        limit = params.get("limit")
        try:
            page_size = max(1, int(limit)) if limit is not None else len(items)
        except (TypeError, ValueError):
            page_size = len(items)
        cursor = params.get("cursor")
        offset = self._decode_cursor(cursor) if cursor else 0
        sliced = items[offset : offset + page_size]
        next_offset = offset + len(sliced)
        next_cursor = self._encode_cursor(next_offset) if next_offset < len(items) else None
        return sliced, next_cursor

    async def _ensure_authorized(self, message: dict) -> bool:
        params = self._coerce_params_dict(message.get("params"))
        token = None
        proxy_meta = params.get("proxy")
        if isinstance(proxy_meta, dict):
            token = proxy_meta.get("authToken")
        if not self.auth_manager.validate(token):
            await self._send_error(message["id"], -32001, "Unauthorized")
            return False
        key = token or "anonymous"
        if not self.rate_limiter.allow(key):
            await self._send_error(message["id"], -32002, "Rate limit exceeded")
            return False
        if isinstance(proxy_meta, dict):
            proxy_meta.pop("authToken", None)
        return True

    @staticmethod
    def _coerce_params_dict(params: Any) -> Dict[str, Any]:
        return params if isinstance(params, dict) else {}

    def _wrap_tool_descriptor(self, alias: str, tool: dict) -> dict:
        result = deepcopy(tool)
        original_name = str(result.get("name"))
        synthetic_name = f"{alias}{SAFE_TOOL_SEPARATOR}{original_name}"
        result["name"] = synthetic_name
        metadata = result.setdefault("metadata", {})
        metadata["proxy"] = {"server": alias, "originalName": original_name}
        self.tool_registry[synthetic_name] = (alias, original_name)
        return result

    def _wrap_prompt_descriptor(self, alias: str, prompt: dict) -> dict:
        result = deepcopy(prompt)
        original_name = str(result.get("name"))
        synthetic_name = f"{alias}{SAFE_TOOL_SEPARATOR}{original_name}"
        result["name"] = synthetic_name
        metadata = result.setdefault("metadata", {})
        metadata["proxy"] = {"server": alias, "originalName": original_name}
        self.prompt_registry[synthetic_name] = (alias, original_name)
        return result

    def _encode_resource_uri(self, alias: str, uri: str) -> str:
        payload = json.dumps({"server": alias, "uri": uri}).encode("utf-8")
        token = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
        return f"{RESOURCE_SCHEME}{token}"

    def _wrap_resource_descriptor(self, alias: str, resource: dict) -> dict:
        result = deepcopy(resource)
        original_uri = str(result.get("uri"))
        synthetic_uri = self._encode_resource_uri(alias, original_uri)
        result["uri"] = synthetic_uri
        metadata = result.setdefault("metadata", {})
        metadata["proxy"] = {"server": alias, "originalUri": original_uri}
        self.resource_registry[synthetic_uri] = (alias, original_uri)
        return result

    def _wrap_resource_template(self, alias: str, template: dict) -> dict:
        result = deepcopy(template)
        metadata = result.setdefault("metadata", {})
        metadata["proxy"] = {"server": alias}
        return result

    def _resolve_tool_name(self, synthetic_name: str) -> Tuple[str, str]:
        if synthetic_name in self.tool_registry:
            return self.tool_registry[synthetic_name]
        if SAFE_TOOL_SEPARATOR in synthetic_name:
            alias, name = synthetic_name.split(SAFE_TOOL_SEPARATOR, 1)
            return alias, name
        raise JsonRpcError(-32602, f"Unknown tool {synthetic_name}")

    def _resolve_prompt_name(self, synthetic_name: str) -> Tuple[str, str]:
        if synthetic_name in self.prompt_registry:
            return self.prompt_registry[synthetic_name]
        if SAFE_TOOL_SEPARATOR in synthetic_name:
            alias, name = synthetic_name.split(SAFE_TOOL_SEPARATOR, 1)
            return alias, name
        raise JsonRpcError(-32602, f"Unknown prompt {synthetic_name}")

    def _resolve_resource_uri(self, uri: str) -> Tuple[str, str]:
        if uri in self.resource_registry:
            return self.resource_registry[uri]
        if uri.startswith(RESOURCE_SCHEME):
            token = uri[len(RESOURCE_SCHEME) :]
            padding = "=" * (-len(token) % 4)
            raw = base64.urlsafe_b64decode(token + padding).decode("utf-8")
            payload = json.loads(raw)
            return payload["server"], payload["uri"]
        raise JsonRpcError(-32602, f"Unknown resource uri {uri}")

    def _decode_cursor(self, cursor: str) -> int:
        try:
            payload = json.loads(base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4)).decode("utf-8"))
        except Exception:
            return 0
        return int(payload.get("offset", 0))

    def _encode_cursor(self, offset: int) -> str:
        payload = json.dumps({"offset": offset}).encode("utf-8")
        return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
