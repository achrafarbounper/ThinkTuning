"""Validation and execution boundary for agent tools.

The planner produces untrusted JSON-like arguments.  This module keeps the
registry focused on discovery while making validation, invocation and logging
one explicit command boundary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.domain.ports import ToolRegistryPort

logger = logging.getLogger("thinktuning.agent.tool_router")


class ToolValidationError(ValueError):
    """Raised when a tool call does not satisfy its declared contract."""


def _matches_type(value: Any, expected: str) -> bool:
    checks = {
        "string": lambda item: isinstance(item, str),
        "number": lambda item: isinstance(item, (int, float)) and not isinstance(item, bool),
        "integer": lambda item: isinstance(item, int) and not isinstance(item, bool),
        "boolean": lambda item: isinstance(item, bool),
        "object": lambda item: isinstance(item, dict),
        "array": lambda item: isinstance(item, list),
    }
    checker = checks.get(expected)
    return checker(value) if checker else True


def validate_args(schema: dict[str, Any] | None, args: dict[str, Any]) -> None:
    """Validate required fields, declared types and enums.

    ``schema`` accepts the registry's historical metadata format
    (``required_args`` and ``parameters``).  Missing or malformed optional
    metadata is intentionally permissive for legacy tools; declared
    constraints are always enforced.
    """
    if not isinstance(args, dict):
        raise ToolValidationError("les arguments doivent être un objet JSON")
    if not schema:
        return

    parameters = schema.get("parameters") or {}
    required = set(schema.get("required_args") or [])
    required.update(
        name for name, spec in parameters.items()
        if isinstance(spec, dict) and spec.get("required") is True
    )
    missing = sorted(name for name in required if name not in args)
    if missing:
        raise ToolValidationError(f"arguments requis manquants : {', '.join(missing)}")

    unknown = sorted(set(args) - set(parameters)) if parameters else []
    if unknown:
        raise ToolValidationError(f"arguments inconnus : {', '.join(unknown)}")

    for name, value in args.items():
        spec = parameters.get(name)
        if not isinstance(spec, dict):
            continue
        expected = spec.get("type")
        if isinstance(expected, str) and not _matches_type(value, expected):
            raise ToolValidationError(
                f"argument « {name} » doit être de type {expected}"
            )
        enum = spec.get("enum")
        if isinstance(enum, list) and value not in enum:
            raise ToolValidationError(
                f"argument « {name} » doit être l'une des valeurs autorisées"
            )


class ToolRouter:
    """Command router that validates and invokes tools from a registry."""

    def __init__(
        self,
        registry: ToolRegistryPort,
        *,
        on_call: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        self._registry = registry
        self._on_call = on_call

    def execute(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        arguments = dict(args or {})
        function = self._registry.get(tool)
        if function is None:
            raise ToolValidationError(f"outil inconnu : {tool}")
        validate_args(self._registry.meta(tool), arguments)
        event = {"tool": tool, "args": arguments}
        if self._on_call is not None:
            self._on_call(event)
        logger.info("tool_call tool=%s args=%s", tool, arguments)
        return function(**arguments)
