"""Tests du standard de définition de tools ``thinktuning.tool/v1`` (SCRUM-99).

Validation (fail-closed), dérivations de sûreté, conversions round-trip
(``to_meta_format`` / ``from_meta_format``), export JSON Schema LLM et
validation DÉTERMINISTE des arguments d'appel. Lance :
pytest tests/test_tool_schema.py -v
"""

import pytest

from ia.tools.tool_schema import (
    DEFAULT_SAFETY,
    approval_from_safety,
    check_args_against_definition,
    from_meta_format,
    is_valid_tool_name,
    to_json_schema,
    to_meta_format,
    validate_tool_definition,
)

MINIMAL = {
    "name": "get_weather",
    "description": "Météo d'une ville.",
    "required_args": ["city"],
    "parameters": {
        "city": {"type": "string", "required": True, "description": "Ville."},
        "unit": {"type": "string", "required": False, "enum": ["c", "f"]},
    },
}


# --- validate_tool_definition ------------------------------------------------

def test_minimal_definition_is_valid():
    ok, errors = validate_tool_definition(MINIMAL)
    assert ok, errors


def test_missing_name_or_description_rejected():
    no_name = {k: v for k, v in MINIMAL.items() if k != "name"}
    ok, errors = validate_tool_definition(no_name)
    assert not ok and any("name" in e for e in errors)

    no_desc = {k: v for k, v in MINIMAL.items() if k != "description"}
    ok, errors = validate_tool_definition(no_desc)
    assert not ok and any("description" in e for e in errors)


@pytest.mark.parametrize("bad", ["GetWeather", "1abc", "a", "x" * 65, "a-b", "a b", ""])
def test_invalid_tool_name_rejected(bad):
    ok, errors = validate_tool_definition({**MINIMAL, "name": bad})
    assert not ok and any("name" in e for e in errors)


def test_minimal_and_maximal_name_lengths_accepted():
    assert validate_tool_definition({**MINIMAL, "name": "ab"})[0]
    assert validate_tool_definition({**MINIMAL, "name": "x" * 64})[0]


def test_unknown_parameter_type_rejected():
    bad = {
        **MINIMAL,
        "parameters": {"city": {"type": "liste", "required": True}},
    }
    ok, errors = validate_tool_definition(bad)
    assert not ok and any("type" in e for e in errors)


def test_unknown_required_arg_rejected():
    ok, errors = validate_tool_definition({**MINIMAL, "required_args": ["nope"]})
    assert not ok and any("required_args" in e for e in errors)


def test_safety_and_approval_shapes():
    ok, errors = validate_tool_definition(
        {**MINIMAL, "safety": {"level": "nope"}}
    )
    assert not ok and any("safety" in e for e in errors)

    ok, errors = validate_tool_definition({**MINIMAL, "approval": "maybe"})
    assert not ok and any("approval" in e for e in errors)

    ok, _ = validate_tool_definition(
        {**MINIMAL, "safety": {"level": "restricted", "requires_approval": True}}
    )
    assert ok


def test_bad_version_and_schema():
    ok, errors = validate_tool_definition({**MINIMAL, "version": ""})
    assert not ok and any("version" in e for e in errors)
    ok, errors = validate_tool_definition({**MINIMAL, "version": 12})
    assert not ok and any("version" in e for e in errors)
    ok, errors = validate_tool_definition({**MINIMAL, "$schema": "other/v9"})
    assert not ok and any("$schema" in e for e in errors)
    ok, _ = validate_tool_definition({**MINIMAL, "version": "v1"})
    assert ok  # version libre : chaîne non vide


# --- Dérivations de sûreté -----------------------------------------------------

@pytest.mark.parametrize(
    "safety, expected",
    [
        ({"level": "safe", "requires_approval": False}, "auto"),
        ({"level": "safe"}, "manual"),  # défaut fail-closed : requires_approval=True
        ({"level": "restricted"}, "manual"),
        ({"level": "dangerous"}, "blocked"),
        (None, None),
    ],
)
def test_approval_from_safety(safety, expected):
    assert approval_from_safety(safety) == expected


@pytest.mark.parametrize(
    "name, expected",
    [
        ("get_weather", True),
        ("ab", True),
        ("GetWeather", False),
        ("9tool", False),
        ("a", False),
        ("tool-name", False),
        ("x" * 65, False),
    ],
)
def test_is_valid_tool_name(name, expected):
    assert is_valid_tool_name(name) is expected


def test_non_dict_input_rejected():
    ok, errors = validate_tool_definition("pas un dict")  # type: ignore[arg-type]
    assert not ok and errors


# --- Conversions round-trip ----------------------------------------------------

def test_meta_roundtrip_preserves_content():
    definition = {
        **MINIMAL,
        "version": "1.2",
        "category": "api",
        "safety": {"level": "restricted", "requires_approval": True},
    }
    meta = to_meta_format(definition)
    assert meta["name"] == "get_weather"
    assert meta["required_args"] == ["city"]
    assert meta["parameters"]["unit"]["enum"] == ["c", "f"]
    # approval dérivé de safety quand il n'est pas déclaré :
    assert meta["approval"] == "manual"

    back = from_meta_format("get_weather", meta)
    ok, errors = validate_tool_definition(back)
    assert ok, errors
    assert back["required_args"] == definition["required_args"]
    assert back["parameters"]["city"]["type"] == "string"
    assert back["safety"] == definition["safety"]


def test_from_meta_fills_defaults():
    definition = from_meta_format("my_tool", {"description": "desc"})
    assert definition["$schema"] == "thinktuning.tool/v1"
    assert definition["version"] == "1.0"
    assert definition["category"] == "builtin"
    assert definition["required_args"] == []
    ok, errors = validate_tool_definition(definition)
    assert ok, errors


def test_default_safety_shape():
    assert DEFAULT_SAFETY["level"] == "restricted"
    assert DEFAULT_SAFETY["requires_approval"] is True


# --- Export JSON Schema (function-calling) --------------------------------------

def test_to_json_schema_exposes_parameters_only():
    definition = {
        **MINIMAL,
        "safety": {"level": "restricted"},
        "allowed_binaries": ["git"],
    }
    js = to_json_schema(definition)
    assert js["type"] == "function"
    fn = js["function"]
    assert fn["name"] == "get_weather"
    assert fn["parameters"]["required"] == ["city"]
    assert set(fn["parameters"]["properties"]) == {"city", "unit"}
    assert fn["parameters"]["properties"]["unit"]["enum"] == ["c", "f"]
    # Design-time : rien de sécurité ne fuit vers le LLM.
    assert "safety" not in fn and "allowed_binaries" not in fn


# --- check_args_against_definition ----------------------------------------------

def test_args_ok():
    ok, errors = check_args_against_definition(
        MINIMAL, {"city": "Paris", "unit": "c"},
    )
    assert ok and not errors


def test_missing_required_arg():
    ok, errors = check_args_against_definition(MINIMAL, {"unit": "c"})
    assert not ok and any("city" in e for e in errors)


def test_wrong_type_and_enum():
    ok, errors = check_args_against_definition(
        MINIMAL, {"city": 12, "unit": "kelvin"},
    )
    assert not ok
    assert any("city" in e for e in errors)
    assert any("unit" in e for e in errors)


def test_undeclared_args_tolerated():
    ok, errors = check_args_against_definition(
        MINIMAL, {"city": "Paris", "extra": True},
    )
    assert ok and not errors


def test_non_dict_args_rejected():
    ok, errors = check_args_against_definition(MINIMAL, ["city"])
    assert not ok and errors
