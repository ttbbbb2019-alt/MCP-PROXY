from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class ServerConfig:
    """Structured settings describing how to launch a downstream MCP server."""

    id: str
    command: List[str]
    env: Dict[str, str] = field(default_factory=dict)
    startup_timeout: float = 15.0
    shutdown_grace: float = 2.0
    stdio_mode: str = "content-length"


@dataclass
class ProxyConfig:
    """Top-level configuration containing every upstream server and proxy defaults."""

    servers: List[ServerConfig]
    log_level: str = "INFO"
    response_timeout: float = 30.0
    auth_token: Optional[str] = None
    rate_limit_per_minute: Optional[int] = None
    structured_logging: bool = False
    healthcheck_interval: Optional[float] = None
    healthcheck_timeout: Optional[float] = None


def load_config(path: str | Path) -> ProxyConfig:
    """Parse the JSON config on disk and materialize it into dataclass instances."""

    file_path = Path(path)
    data = json.loads(file_path.read_text(encoding="utf-8"))
    servers: List[ServerConfig] = []
    for raw in data.get("servers", []):
        if "id" not in raw or "command" not in raw:
            raise ValueError("Each server entry requires both 'id' and 'command'.")
        mode = str(raw.get("stdio_mode", "content-length")).lower()
        if mode not in {"content-length", "newline"}:
            raise ValueError(f"Invalid stdio_mode '{mode}' for server {raw.get('id')}.")
        servers.append(
            ServerConfig(
                id=raw["id"],
                command=list(raw["command"]),
                env=dict(raw.get("env", {})),
                startup_timeout=float(raw.get("startup_timeout", 15.0)),
                shutdown_grace=float(raw.get("shutdown_grace", 2.0)),
                stdio_mode=mode,
            )
        )
    if not servers:
        raise ValueError("At least one downstream server must be configured.")
    return ProxyConfig(
        servers=servers,
        log_level=str(data.get("log_level", "INFO")).upper(),
        response_timeout=float(data.get("response_timeout", 30.0)),
        auth_token=data.get("auth_token"),
        rate_limit_per_minute=data.get("rate_limit_per_minute"),
        structured_logging=bool(data.get("structured_logging", False)),
        healthcheck_interval=_get_optional_float(data.get("healthcheck_interval")),
        healthcheck_timeout=_get_optional_float(data.get("healthcheck_timeout")),
    )


def _get_optional_float(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValueError("healthcheck values must be numeric if provided")
