from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Dict, Optional

from .config import ServerConfig
from .framing import JsonRpcStream
from .jsonrpc import JsonRpcError, is_request, is_response

_LOGGER = logging.getLogger(__name__)


class UpstreamServer:
    """Encapsulates a single downstream MCP server process and its request lifecycle."""

    def __init__(self, config: ServerConfig, proxy: "ProxyRouter"):
        self.config = config
        self.proxy = proxy
        self._process: Optional[asyncio.subprocess.Process] = None
        self._stream: Optional[JsonRpcStream] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._listen_task: Optional[asyncio.Task] = None
        self._health_task: Optional[asyncio.Task] = None
        self._restart_lock = asyncio.Lock()
        self._healthy = True
        self._id_counter = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._initialized = False
        self.initialize_result: Optional[dict] = None
        self._last_init_params: Optional[dict] = None

    @property
    def alias(self) -> str:
        return self.config.id

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def ensure_started(self) -> None:
        """Make sure the subprocess has been spawned and wiring to stdio is ready."""
        if self.running:
            return
        env = os.environ.copy()
        env.update(self.config.env)
        self._process = await asyncio.create_subprocess_exec(
            *self.config.command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        assert self._process.stdout and self._process.stdin
        prefer_newline = self.config.stdio_mode == "newline"
        self._stream = JsonRpcStream(self._process.stdout, self._process.stdin, self.alias, prefer_newline=prefer_newline)
        self._stderr_task = asyncio.create_task(self._pump_stderr(), name=f"{self.alias}-stderr")
        self._listen_task = asyncio.create_task(self._listen_loop(), name=f"{self.alias}-listen")
        self._start_healthcheck()
        _LOGGER.info("Started upstream server %s with pid %s", self.alias, self._process.pid)

    async def initialize(self, params: dict) -> dict:
        """Send the MCP initialize handshake to the server and memoize the response."""
        await self.ensure_started()
        if self._initialized and self.initialize_result:
            return self.initialize_result
        payload = dict(params)
        self._last_init_params = payload
        client_info = payload.get("clientInfo", {})
        payload["clientInfo"] = {
            "name": f"{client_info.get('name', 'mcp-client')}-through-proxy",
            "version": client_info.get("version", "0.0"),
        }
        _LOGGER.debug("Initializing upstream %s with payload %s", self.alias, payload)
        result = await self.request("initialize", payload, timeout=self.config.startup_timeout)
        self.initialize_result = result
        self._initialized = True
        try:
            await self.notify("notifications/initialized")
        except Exception as exc:  # pragma: no cover - best effort
            _LOGGER.debug("Upstream %s notifications/initialized failed: %s", self.alias, exc)
        return result

    async def request(self, method: str, params: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        """Send a JSON-RPC request to the server and await the result or propagated error."""
        if not self.running:
            await self.ensure_started()
        assert self._stream is not None
        loop = asyncio.get_running_loop()
        self._id_counter += 1
        request_id = self._id_counter
        future: asyncio.Future = loop.create_future()
        self._pending[request_id] = future
        message = {"jsonrpc": "2.0", "id": request_id, "method": method}
        if params is not None:
            message["params"] = params
        await self._stream.send_message(message)
        try:
            response = await asyncio.wait_for(future, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)
        if "error" in response:
            err = response["error"]
            raise JsonRpcError(err.get("code", -32000), err.get("message", "Upstream error"), err.get("data"))
        return response.get("result")

    async def notify(self, method: str, params: Optional[dict] = None) -> None:
        """Fire-and-forget helper for upstream notifications (no response expected)."""
        if not self.running:
            await self.ensure_started()
        assert self._stream is not None
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        await self._stream.send_message(message)

    async def send_raw(self, payload: dict) -> None:
        """
        Send a pre-built JSON-RPC message to the upstream server (used for responses).
        """

        if not self.running:
            raise RuntimeError(f"Server {self.alias} is not running.")
        assert self._stream is not None
        await self._stream.send_message(payload)

    async def shutdown(self) -> None:
        """Attempt a graceful shutdown of the subprocess before forcing termination."""
        if not self.running:
            return
        try:
            await self.request("shutdown", timeout=self.config.shutdown_grace)
        except Exception as exc:  # pragma: no cover - best effort shutdown
            _LOGGER.warning("Failed graceful shutdown for %s: %s", self.alias, exc)
        if self._process:
            self._process.terminate()
            try:
                await asyncio.wait_for(self._process.wait(), timeout=2)
            except asyncio.TimeoutError:
                _LOGGER.warning("Killing stalled server %s", self.alias)
                self._process.kill()
        if self._listen_task:
            self._listen_task.cancel()
        if self._stderr_task:
            self._stderr_task.cancel()
        if self._health_task:
            self._health_task.cancel()
        self._process = None
        self._stream = None
        self._initialized = False
        self._healthy = False

    async def _pump_stderr(self) -> None:
        assert self._process and self._process.stderr
        while True:
            line = await self._process.stderr.readline()
            if not line:
                return
            _LOGGER.debug("[%s STDERR] %s", self.alias, line.decode("utf-8", errors="ignore").rstrip())

    async def _listen_loop(self) -> None:
        assert self._stream is not None
        while True:
            message = await self._stream.read_message()
            if message is None:
                _LOGGER.info("Upstream server %s closed its stream", self.alias)
                await self._handle_unhealthy()
                break
            if is_response(message):
                pending = self._pending.get(message["id"])
                if pending and not pending.done():
                    pending.set_result(message)
                else:
                    _LOGGER.debug("Unexpected response id %s from %s", message.get("id"), self.alias)
            elif is_request(message):
                await self.proxy.forward_server_request(self, message)
            else:
                await self.proxy.forward_server_notification(self, message)

    def _start_healthcheck(self) -> None:
        if self._health_task:
            self._health_task.cancel()
        interval = getattr(self.proxy.config, "healthcheck_interval", None)
        timeout = getattr(self.proxy.config, "healthcheck_timeout", None)
        if not interval or not timeout:
            return
        self._health_task = asyncio.create_task(self._health_loop(interval, timeout), name=f"{self.alias}-health")

    async def _health_loop(self, interval: float, timeout: float) -> None:
        while True:
            await asyncio.sleep(interval)
            if not self.running:
                continue
            try:
                await asyncio.wait_for(self.request("ping"), timeout=timeout)
                if not self._healthy:
                    _LOGGER.info("Upstream %s recovered", self.alias)
                    self._healthy = True
            except Exception as exc:
                _LOGGER.warning("Health check failed for %s: %s", self.alias, exc)
                await self._handle_unhealthy()

    async def _handle_unhealthy(self) -> None:
        self._healthy = False
        interval = getattr(self.proxy.config, "healthcheck_interval", None)
        if not interval:
            return
        if self._restart_lock.locked():
            return
        async with self._restart_lock:
            try:
                await self.shutdown()
            except Exception as exc:
                _LOGGER.error("Failed to shut down unhealthy server %s: %s", self.alias, exc)
            backoff = 1.0
            for attempt in range(5):
                try:
                    _LOGGER.info("Attempting restart for %s (attempt %s)", self.alias, attempt + 1)
                    await self.ensure_started()
                    await self.initialize(self._last_init_params or {})
                    self._healthy = True
                    _LOGGER.info("Restarted server %s", self.alias)
                    return
                except Exception as exc:
                    _LOGGER.error("Restart attempt %s failed for %s: %s", attempt + 1, self.alias, exc)
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
            _LOGGER.error("Exceeded restart attempts for %s", self.alias)
