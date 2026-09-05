"""Tests de la ``ToolRegistry`` — source de vérité unique (SCRUM-99).

Contrats vérifiés : hydratation des natifs, enregistrement dynamique avec
PROJECTION dans TOOLS/TOOL_META/REQUIRED_ARGS, fail-closed (approval forcé
manual, natifs intouchables, plafond dynamique), retrait propre, état runtime
et anti-divergence des trois dicts. Lance : pytest tests/test_tool_registry.py -v
"""

import pytest

from ia.tools.registry import (
    RegisteredTool,
    ToolRegistry,
    ToolRegistryError,
    get_global_registry,
)
from ia.tools.tool_registry import REQUIRED_ARGS, TOOL_META, TOOLS

DEF = {
    "name": "test_helper_tool",
    "description": "Tool de test.",
    "required_args": ["x"],
    "parameters": {"x": {"type": "string", "required": True}},
    "safety": {"level": "safe", "requires_approval": False},
}


def _func(**_kwargs):
    return "ok"


@pytest.fixture()
def registry():
    """Registry fraîche + nettoyage de la projection globale après test."""
    yield ToolRegistry()
    TOOLS.pop("test_helper_tool", None)
    TOOL_META.pop("test_helper_tool", None)
    REQUIRED_ARGS.pop("test_helper_tool", None)


# --- Hydratation ------------------------------------------------------------

def test_bootstraps_native_tools(registry):
    assert len(registry) >= 55
    assert not registry.dynamic_tool_names()
    assert registry.has_native("run_command")
    assert registry.get_tool("run_command").dynamic is False


def test_singleton_identity():
    assert get_global_registry() is get_global_registry()


# --- add_tool / projections ---------------------------------------------------

def test_add_tool_projects_into_static_dicts(registry):
    registered = registry.add_tool(_func, DEF, owner="tests")
    assert isinstance(registered, RegisteredTool)
    assert registered.dynamic and registered.approval == "manual"  # fail-closed
    assert TOOLS["test_helper_tool"] is _func
    assert REQUIRED_ARGS["test_helper_tool"] == ["x"]
    assert TOOL_META["test_helper_tool"]["required_args"] == ["x"]
    assert TOOL_META["test_helper_tool"]["approval"] == "manual"
    assert registry.dynamic_tool_names() == ["test_helper_tool"]


def test_allow_auto_approval_honors_safety(registry):
    registered = registry.add_tool(_func, DEF, allow_auto_approval=True)
    assert registered.approval == "auto"


def test_dangerous_safety_maps_to_blocked(registry):
    dangerous = {
        **DEF,
        "safety": {"level": "dangerous", "requires_approval": True},
    }
    registered = registry.add_tool(_func, dangerous, allow_auto_approval=True)
    assert registered.approval == "blocked"


def test_add_tool_from_scattered_fields(registry):
    registered = registry.add_tool(
        _func,
        name="test_helper_tool",
        description="Tool de test.",
        parameters={"x": {"type": "string", "required": True}},
    )
    assert registered.required_args == ["x"]  # dérivées des paramètres requis
    assert registered.approval == "manual"


def test_invalid_definition_rejected(registry):
    with pytest.raises(ToolRegistryError):
        registry.add_tool(_func, {**DEF, "name": "BadName"})
    assert "test_helper_tool" not in TOOLS


def test_non_callable_rejected(registry):
    with pytest.raises(ToolRegistryError):
        registry.add_tool("pas une fonction", DEF)  # type: ignore[arg-type]


# --- Conflits et plafond ------------------------------------------------------

def test_native_tool_never_overwritten(registry):
    with pytest.raises(ToolRegistryError):
        registry.add_tool(_func, {**DEF, "name": "run_command"})


def test_duplicate_needs_overwrite(registry):
    registry.add_tool(_func, DEF)

    def _other(**_kwargs):
        return "other"

    with pytest.raises(ToolRegistryError):
        registry.add_tool(_other, DEF)
    replaced = registry.add_tool(_other, DEF, overwrite=True)
    assert TOOLS["test_helper_tool"] is _other
    assert replaced.func is _other


def test_dynamic_cap(registry):
    capped = ToolRegistry(max_dynamic_tools=1)
    capped.add_tool(_func, DEF)
    with pytest.raises(ToolRegistryError):
        capped.add_tool(
            _func, {**DEF, "name": "test_second_tool"},
        )
    TOOLS.pop("test_second_tool", None)


# --- remove_tool / état runtime ------------------------------------------------

def test_remove_tool_cleans_projection(registry):
    registry.add_tool(_func, DEF)
    assert registry.remove_tool("test_helper_tool") is True
    assert "test_helper_tool" not in TOOLS
    assert "test_helper_tool" not in TOOL_META
    assert "test_helper_tool" not in REQUIRED_ARGS
    assert registry.remove_tool("test_helper_tool") is False


def test_native_tool_never_removed(registry):
    with pytest.raises(ToolRegistryError):
        registry.remove_tool("run_command")


def test_set_runtime_state(registry):
    registry.add_tool(_func, DEF)
    tool = registry.set_runtime_state("test_helper_tool", enabled=False)
    assert tool.enabled is False
    tool = registry.set_runtime_state("test_helper_tool", experimental=True)
    assert tool.experimental is True
    with pytest.raises(ToolRegistryError):
        registry.set_runtime_state("inconnu_inexistant", enabled=True)


def test_merged_registry_and_version(registry):
    registry.add_tool(_func, DEF)
    tools, required = registry.merged_registry()
    assert tools["test_helper_tool"] is _func
    assert required["test_helper_tool"] == ["x"]
    version_before = registry.version
    registry.add_tool(_func, DEF, overwrite=True)
    assert registry.version > version_before


# --- Anti-divergence globale ----------------------------------------------------

def test_anti_divergence_after_operations(registry):
    registry.add_tool(_func, DEF)
    assert set(TOOLS) == set(TOOL_META) == set(REQUIRED_ARGS)
    registry.remove_tool("test_helper_tool")
    assert set(TOOLS) == set(TOOL_META) == set(REQUIRED_ARGS)


def test_registry_error_is_value_error():
    assert issubclass(ToolRegistryError, ValueError)
