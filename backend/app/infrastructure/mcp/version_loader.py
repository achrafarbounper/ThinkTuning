# project/app/infrastructure/mcp/version_loader.py
"""Chargement de la version MCP depuis ``pyproject.toml`` (``[tool.mcp]``).

Adapter d'infrastructure : TOUTE I/O fichier est confinée ici (règle
hexagonale), le parsing pur restant dans le domaine
(``MCPVersion.from_toml_source``).

Résolution (aucun chemin explicite) : racine du package backend (déduite de
``__file__``, robuste au CWD), puis répertoire courant. Le premier fichier
EXISTANT gagne.

Tolérance : par défaut, toute anomalie (fichier absent, table ``[tool.mcp]``
manquante, version invalide) logge un warning et retombe sur
``DEFAULT_MCP_VERSION`` — le serveur MCP (tâche 2) doit démarrer même sans
``pyproject.toml`` (ex. image Docker allégée). Le mode ``strict=True``
propage l'erreur (outillage CI/CD, tests de contrat).
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.domain.entities.mcp import DEFAULT_MCP_VERSION, MCPVersion

logger = logging.getLogger("thinktuning.mcp.version")

_PYPROJECT_FILENAME = "pyproject.toml"


def _default_candidates() -> list[Path]:
    """Chemins de ``pyproject.toml`` sondés quand aucun chemin explicite.

    ``Path(__file__).resolve().parents[3]`` remonte de
    ``app/infrastructure/mcp/version_loader.py`` à la racine ``backend/``.
    """
    package_root = Path(__file__).resolve().parents[3]
    candidates = [package_root / _PYPROJECT_FILENAME]
    cwd_candidate = Path.cwd() / _PYPROJECT_FILENAME
    if cwd_candidate not in candidates:
        candidates.append(cwd_candidate)
    return candidates


def load_mcp_version(
    pyproject_path: str | Path | None = None, *, strict: bool = False
) -> MCPVersion:
    """Lit ``[tool.mcp] version`` et retourne une ``MCPVersion``.

    Args:
        pyproject_path: chemin explicite vers un ``pyproject.toml`` ; si
            ``None``, sondage racine du package puis répertoire courant.
        strict: ``True`` → propage les erreurs (fichier absent, table absente,
            version invalide) ; ``False`` (défaut) → warning + fallback sur
            ``DEFAULT_MCP_VERSION``.

    Returns:
        La version MCP parsée, ou ``DEFAULT_MCP_VERSION`` en mode tolérant.
    """
    if pyproject_path is not None:
        path = Path(pyproject_path)
        if not path.is_file():
            return _fallback(f"pyproject.toml introuvable : {path}", strict)
    else:
        found = next((c for c in _default_candidates() if c.is_file()), None)
        if found is None:
            return _fallback(
                "pyproject.toml introuvable (racine package et CWD sondés)", strict
            )
        path = found
    try:
        return MCPVersion.from_toml_source(path.read_text(encoding="utf-8"))
    except ValueError as exc:  # TOML invalide, table/clé absente, format X.Y.Z
        return _fallback(f"{path} : {exc}", strict)
    except OSError as exc:  # lecture impossible (permissions, disque...)
        return _fallback(f"{path} illisible : {exc}", strict)


def _fallback(reason: str, strict: bool) -> MCPVersion:
    """Logge le repli (mode tolérant) ou propage l'erreur (mode strict)."""
    if strict:
        raise ValueError(f"Version MCP non résolue : {reason}")
    logger.warning("Version MCP : %s — fallback sur %s", reason, DEFAULT_MCP_VERSION)
    return DEFAULT_MCP_VERSION
