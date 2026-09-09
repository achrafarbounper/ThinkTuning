# project/tests/test_mcp_version.py
"""Tests de l'entité MCPVersion (app/domain/entities/mcp.py) et du loader
infrastructure (app/infrastructure/mcp/version_loader.py) — Tâche 1 (S1)."""

import logging
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.domain.entities.mcp import DEFAULT_MCP_VERSION, MCPVersion
from app.infrastructure.mcp.version_loader import load_mcp_version

_LOADER_LOGGER = "thinktuning.mcp.version"


# --- Construction / parse ----------------------------------------------------


def test_parse_valid_round_trip() -> None:
    version = MCPVersion.parse("0.1.0")
    assert version == MCPVersion(major=0, minor=1, patch=0)
    assert str(version) == "0.1.0"
    assert version.as_tuple() == (0, 1, 0)


@pytest.mark.parametrize("raw", ["0.1.0", " 1.0.0 ", "10.20.30"])
def test_parse_accepts(raw: str) -> None:
    assert str(MCPVersion.parse(raw)) == raw.strip()


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "1.2",  # 2 composants
        "1.2.3.4",  # 4 composants
        "v1.0.0",  # préfixe non numérique
        "1.0.0-beta",  # pré-release : hors périmètre (roadmap = versions stables)
        "1.0.0+build",  # métadonnée de build : hors périmètre
        "01.2.3",  # zéro initial (semver §2)
        "-1.0.0",  # négatif
    ],
)
def test_parse_rejects(raw: str) -> None:
    with pytest.raises(ValueError, match="Version MCP invalide"):
        MCPVersion.parse(raw)


def test_parse_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="attendu 'X.Y.Z'"):
        MCPVersion.parse(1)  # type: ignore[arg-type]


def test_constructor_rejects_negative_components() -> None:
    with pytest.raises(ValidationError):
        MCPVersion(major=-1, minor=0, patch=0)


def test_frozen() -> None:
    version = MCPVersion.parse("0.1.0")
    with pytest.raises(ValidationError):
        version.minor = 2  # type: ignore[misc]


# --- Ordre / comparaison (jalons de la roadmap MCP) ---------------------------


def test_ordering_matches_roadmap_milestones() -> None:
    versions = [
        MCPVersion.parse(s) for s in ("0.1.0", "1.0.0", "1.1.0", "2.0.0", "3.0.0")
    ]
    v010, v100, v110, v200, v300 = versions
    assert v010 < v100 < v110 < v200 < v300
    assert v300 >= v010
    assert v100 <= v110
    assert v200 > v100
    assert v100 == MCPVersion.parse("1.0.0")
    assert v100 != v110


# --- Parsing TOML pur (domaine, sans I/O) -------------------------------------

_TOML_VALID = """
[project]
name = "thinktuning"
version = "1.0.0"

[tool.mcp]
version = "2.0.0"
"""


def test_from_toml_source_reads_tool_mcp_version() -> None:
    assert MCPVersion.from_toml_source(_TOML_VALID) == MCPVersion.parse("2.0.0")


def test_from_toml_source_missing_table() -> None:
    with pytest.raises(ValueError, match=r"\[tool\.mcp\]"):
        MCPVersion.from_toml_source('[project]\nname = "x"\n')


def test_from_toml_source_missing_key() -> None:
    with pytest.raises(ValueError, match="version"):
        MCPVersion.from_toml_source("[tool.mcp]\n")


def test_from_toml_source_invalid_toml() -> None:
    with pytest.raises(ValueError, match="TOML invalide"):
        MCPVersion.from_toml_source("[tool.mcp")


def test_from_toml_source_invalid_version() -> None:
    with pytest.raises(ValueError, match="Version MCP invalide"):
        MCPVersion.from_toml_source('[tool.mcp]\nversion = "1.2"\n')


def test_from_toml_source_non_string_version() -> None:
    with pytest.raises(ValueError, match="doit être une chaîne"):
        MCPVersion.from_toml_source("[tool.mcp]\nversion = 1\n")


# --- Contrat de wiring : le pyproject du repo déclare la version S1 -----------


def test_backend_pyproject_declares_mcp_version() -> None:
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    assert pyproject.is_file()
    source = pyproject.read_text(encoding="utf-8")
    assert MCPVersion.from_toml_source(source) == MCPVersion.parse("2.0.0")


def test_load_default_discovery_reads_backend_pyproject() -> None:
    """Le wiring réel : découverte racine package → backend/pyproject.toml."""
    assert load_mcp_version() == MCPVersion.parse("2.0.0")


# --- Loader infrastructure (I/O + fallback) ------------------------------------


def _pyproject(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "pyproject.toml"
    path.write_text(body, encoding="utf-8")
    return path


def test_load_from_explicit_path(tmp_path: Path) -> None:
    path = _pyproject(tmp_path, '[tool.mcp]\nversion = "1.0.0"\n')
    assert load_mcp_version(path) == MCPVersion.parse("1.0.0")


def test_load_accepts_str_path(tmp_path: Path) -> None:
    path = _pyproject(tmp_path, '[tool.mcp]\nversion = "1.1.0"\n')
    assert load_mcp_version(str(path)) == MCPVersion.parse("1.1.0")


def test_load_missing_file_falls_back_with_warning(tmp_path: Path, caplog) -> None:
    with caplog.at_level(logging.WARNING, logger=_LOADER_LOGGER):
        version = load_mcp_version(tmp_path / "absent.toml")
    assert version == DEFAULT_MCP_VERSION
    assert any("introuvable" in record.getMessage() for record in caplog.records)


def test_load_missing_file_strict_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="introuvable"):
        load_mcp_version(tmp_path / "absent.toml", strict=True)


def test_load_missing_section_falls_back(tmp_path: Path, caplog) -> None:
    path = _pyproject(tmp_path, '[project]\nname = "x"\n')
    with caplog.at_level(logging.WARNING, logger=_LOADER_LOGGER):
        version = load_mcp_version(path)
    assert version == DEFAULT_MCP_VERSION
    assert any("tool.mcp" in record.getMessage() for record in caplog.records)


def test_load_invalid_version_falls_back(tmp_path: Path) -> None:
    path = _pyproject(tmp_path, '[tool.mcp]\nversion = "not-a-version"\n')
    assert load_mcp_version(path) == DEFAULT_MCP_VERSION


def test_load_invalid_version_strict_raises(tmp_path: Path) -> None:
    path = _pyproject(tmp_path, '[tool.mcp]\nversion = "not-a-version"\n')
    with pytest.raises(ValueError, match="not-a-version"):
        load_mcp_version(path, strict=True)


def test_default_constant_is_s1_bootstrap_version() -> None:
    """Le fallback porte bien la version du livrable S6 (v2.0.0)."""
    assert DEFAULT_MCP_VERSION == MCPVersion.parse("2.0.0")


def test_comparison_with_other_type_raises() -> None:
    with pytest.raises(TypeError):
        _ = MCPVersion.parse("1.0.0") < "1.0.0"  # type: ignore[operator]


def test_hashable_for_feature_gates() -> None:
    assert len({MCPVersion.parse("1.0.0"), MCPVersion.parse("1.0.0")}) == 1
