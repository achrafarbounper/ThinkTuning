"""Declarative outbound host configuration loader."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from .connection_manager import MCPServerConfig

logger = logging.getLogger("thinktuning.mcp.host.config")

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[4] / "configs" / "mcp_host.yaml"


def load_server_configs(path: str | Path | None = None) -> list[MCPServerConfig]:
    """Load explicit server entries; disabled host yields an empty list."""
    if os.getenv("AGENT_MCP_HOST", "").strip().lower() not in {"1", "true", "yes", "on"}:
        return []
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        logger.warning("MCP host config missing: %s", config_path)
        return []
    try:
        import yaml  # type: ignore[import-not-found]
        document: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except ImportError as exc:
        raise RuntimeError("PyYAML is required only when AGENT_MCP_HOST is enabled") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid MCP host config: {config_path}") from exc
    if not document.get("enabled", False):
        return []
    configs: list[MCPServerConfig] = []
    for name, raw in (document.get("servers") or {}).items():
        if not isinstance(raw, dict):
            raise ValueError(f"invalid MCP server config: {name}")
        raw_command = raw.get("command", "")
        if isinstance(raw_command, list):
            command = [str(item) for item in raw_command]
        else:
            command = [str(raw_command)]
        command.extend(str(x) for x in raw.get("args", []))
        if not command or not command[0] or command[0] == "[]":
            logger.warning("Skipping MCP server %s: command not configured", name)
            continue
        configs.append(MCPServerConfig(
            name=name, command=command, env={str(k):
                                             str(v) for k, v in (raw.get("env") or {}).items()},
            token_env=raw.get("token_env"), timeout=float(raw.get("timeout_seconds", 10)),
            max_restarts=int(raw.get("max_restarts", 2)), enabled=bool(raw.get("enabled", True)),
        ))
    return configs


def load_capability_routes(path: str | Path | None = None) -> dict[str, dict[str, Any]]:
    """Load stable local capability mappings without starting any process."""
    config_path = Path(path or DEFAULT_CONFIG_PATH)
    if not config_path.exists():
        return {}
    try:
        import yaml  # type: ignore[import-not-found]

        document: dict[str, Any] = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except ImportError as exc:
        raise RuntimeError("PyYAML is required to load MCP host routes") from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"invalid MCP host config: {config_path}") from exc
    raw_tools = document.get("tools") or {}
    if not isinstance(raw_tools, dict):
        raise ValueError("MCP host tools must be a mapping")
    routes: dict[str, dict[str, Any]] = {}
    for name, raw in raw_tools.items():
        if not isinstance(raw, dict) or not raw.get("server") or not raw.get("remote"):
            raise ValueError(f"invalid MCP capability route: {name}")
        routes[str(name)] = {
            "server": str(raw["server"]),
            "method": str(raw.get("method", "tools/call")),
            "remote": str(raw["remote"]),
            "read_only": bool(raw.get("read_only", True)),
        }
    return routes
