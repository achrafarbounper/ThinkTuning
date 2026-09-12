from __future__ import annotations

import pytest

from app.agent.tool_router import ToolRouter, ToolValidationError, validate_args


def test_validate_args_enforces_required_types_and_enum() -> None:
    schema = {
        "required_args": ["city"],
        "parameters": {
            "city": {"type": "string"},
            "unit": {"type": "string", "enum": ["c", "f"]},
        },
    }

    validate_args(schema, {"city": "Paris", "unit": "c"})

    with pytest.raises(ToolValidationError, match="requis"):
        validate_args(schema, {})
    with pytest.raises(ToolValidationError, match="type"):
        validate_args(schema, {"city": 42})
    with pytest.raises(ToolValidationError, match="valeurs"):
        validate_args(schema, {"city": "Paris", "unit": "k"})


def test_router_validates_before_invoking_tool() -> None:
    calls: list[dict] = []

    class Registry:
        def get(self, tool):
            return lambda **args: calls.append(args)

        def meta(self, tool):
            return {"parameters": {"path": {"type": "string", "required": True}}}

    router = ToolRouter(Registry())
    with pytest.raises(ToolValidationError):
        router.execute("write_file", {"path": 123})
    assert calls == []


def test_router_emits_call_event_and_returns_value() -> None:
    events: list[dict] = []

    class Registry:
        def get(self, tool):
            return lambda value="": value.upper()

        def meta(self, tool):
            return {"parameters": {"value": {"type": "string"}}}

    router = ToolRouter(Registry(), on_call=events.append)
    assert router.execute("echo", {"value": "ok"}) == "OK"
    assert events == [{"tool": "echo", "args": {"value": "ok"}}]
