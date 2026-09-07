# project/tests/test_legacy_registry_adapter.py
"""Tests de l'adaptateur registre legacy -> ToolRegistryPort."""

from app.domain.ports import ToolRegistryPort
from app.infrastructure.legacy_registry import LegacyToolRegistryAdapter, build_default_registry


def test_adapter_satisfies_port() -> None:
    adapter = build_default_registry()
    assert isinstance(adapter, ToolRegistryPort)


def test_tool_names_from_legacy_manifest() -> None:
    names = build_default_registry().tool_names()
    # Outils documentés du registre legacy (tools_config.json)
    assert "read_file" in names
    assert "web_search" in names
    assert "predict_sentiment" in names
    assert "sqlite_query" in names
    # Trié, déterministe, sans doublons
    assert names == sorted(set(names))


def test_get_returns_callable_and_none_for_unknown() -> None:
    adapter = build_default_registry()
    assert callable(adapter.get("read_file"))
    assert adapter.get("outil_inexistant") is None


def test_meta_from_tools_config_json() -> None:
    adapter = build_default_registry()
    meta = adapter.meta("read_file")
    assert meta is not None
    assert "description" in meta
    assert adapter.meta("outil_inexistant") is None


def test_exclude_removes_high_risk_tools() -> None:
    adapter = LegacyToolRegistryAdapter(exclude={"run_command"})
    assert "run_command" not in adapter.tool_names()
    assert adapter.get("run_command") is None
    assert adapter.meta("run_command") is None
    # Les autres outils restent disponibles
    assert adapter.get("read_file") is not None
