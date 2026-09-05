"""Tests des tools personnalisés d'exemple (SCRUM-99) : run_shell / call_api.

Comportements vérifiés : normalisation sûre des commandes (jamais de shell),
allowlist des binaires, dry_run déterministe, gardes de call_api, validité
des définitions standard v1 et cohérence avec tools_config.json / TOOL_META.
Lance : pytest tests/test_custom_tools.py -v
"""

import json

import pytest

from ia.tools.custom_tools import EXAMPLE_TOOL_DEFINITIONS, call_api, run_shell
from ia.tools.tool_schema import validate_tool_definition
from ia.tools.tool_registry import REQUIRED_ARGS, TOOL_META


# --- run_shell -----------------------------------------------------------------

def test_run_shell_dry_run_returns_preview():
    result = run_shell(["git", "--version"], dry_run=True)
    assert result["dry_run"] is True
    assert result["would_run"] == ["git", "--version"]
    assert result["allowed"] is True


def test_run_shell_dry_run_rejects_disallowed_binary():
    result = run_shell(["rm", "-rf", "/"], dry_run=True)
    assert result["allowed"] is False
    assert result["dry_run"] is True


def test_run_shell_string_is_split_via_shlex():
    result = run_shell("git --version", dry_run=True)
    assert result["would_run"] == ["git", "--version"]


def test_run_shell_rejects_bad_command_shapes():
    with pytest.raises(ValueError):
        run_shell([], dry_run=True)
    with pytest.raises(ValueError):
        run_shell([1, 2], dry_run=True)  # type: ignore[list-item]
    with pytest.raises(ValueError):
        run_shell("git 'non-termine", dry_run=True)  # shlex ValueError


# --- call_api ------------------------------------------------------------------

def test_call_api_get_rejects_body():
    with pytest.raises(ValueError):
        call_api("http://example.com", method="GET", body="x")
    with pytest.raises(ValueError):
        call_api("http://example.com", method="GET", json_payload={"a": 1})


def test_call_api_rejects_unknown_method():
    with pytest.raises(ValueError):
        call_api("http://example.com", method="DELETE")


def test_call_api_rejects_bad_scheme():
    with pytest.raises(ValueError):
        call_api("ftp://example.com")


# --- Définitions standard v1 -----------------------------------------------------

@pytest.mark.parametrize("name", sorted(EXAMPLE_TOOL_DEFINITIONS))
def test_example_definitions_are_valid(name):
    ok, errors = validate_tool_definition(EXAMPLE_TOOL_DEFINITIONS[name])
    assert ok, errors


def test_example_definitions_consistent_with_static_registry():
    """Anti-divergence : definitions d'exemple == registre statique.

    Le libellé exact des descriptions peut être reformulé entre l'exemple
    standard et tools_config.json : on vérifie la PRÉSENCE (non vide) et
    l'alignement structurel (required_args, nom).
    """
    for name, definition in EXAMPLE_TOOL_DEFINITIONS.items():
        assert name in TOOL_META, f"{name} absent de TOOL_META"
        assert name in REQUIRED_ARGS, f"{name} absent de REQUIRED_ARGS"
        assert definition["required_args"] == REQUIRED_ARGS[name]
        assert str(TOOL_META[name].get("description", "")).strip()
