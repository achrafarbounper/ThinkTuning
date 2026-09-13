"""Garde CI : plus aucun NOUVEAU code ne doit dépendre directement de ``app.agent.legacy``.

Contexte (réduction de la compatibilité legacy — S1) : le runtime agentique v1
vit dans ``backend/app/agent/legacy/``. Il a été absorbé depuis ``ia/agent``
sans réécriture (strangler). La migration cible le noyau v2
(``app.agent.core`` / ``app.agent.factory``) ou les use-cases
``app.application.*`` ; tant qu'il existe, le SEUL point d'entrée production
autorisé vers le v1 est la façade strangler ``app.application.agent_cache``
(résolution paresseuse des symboles, aucun hack ``sys.path`` — cf.
``test_sys_path_guard.py``).

Ce test verrouille l'invariant :

    1. statique (AST) : aucun ``import`` / ``from ... import`` de
       ``app.agent.legacy.*`` dans ``app/**.py`` (hors paquet v1 lui-même), à
       part l'adaptateur sanctionné ;
    2. statique (AST) : aucun contournement par
       ``importlib.import_module("app.agent.legacy…")`` / ``__import__("…")``
       hors de l'adaptateur autorisé ;
    3. la liste des importeurs autorisés doit se RÉDUIRE dans le temps —
       tout ajout est un NON (la cible finale est la liste vide + suppression
       du paquet).

Comment lancer : pytest tests/test_no_direct_legacy_imports.py -v
"""

from __future__ import annotations

import ast
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"

LEGACY_PACKAGE = "app.agent.legacy"

#: Imports directs du runtime v1 autorisés en production (façade strangler).
#: Le jour où ``agent_cache`` n'existe plus, cette liste doit être VIDE.
ALLOWED_LEGACY_IMPORTERS = frozenset({"app.application.agent_cache"})

#: Justification par module autorisé — documentée pour éviter une dérive silencieuse.
_ALLOWED_REASONS = {
    "app.application.agent_cache": (
        "façade strangler unique : résout paresseusement les symboles v1 "
        "(AgentCore / AgentRunner / MultiAgentCoordinator) au premier usage."
    ),
}


def _module_name(path: Path) -> str:
    """Nom de module Python déduit du chemin sous la racine backend/."""
    rel = path.relative_to(PROJECT_ROOT).with_suffix("")
    return ".".join(rel.parts)


def _is_legacy_target(value: str) -> bool:
    """Vrai si ``value`` nomme le paquet legacy ou l'un de ses modules."""
    return value == LEGACY_PACKAGE or value.startswith(LEGACY_PACKAGE + ".")


def _legacy_violations(tree: ast.AST) -> list[str]:
    """Liste les accès à ``app.agent.legacy`` statiquement résolus (AST).

    Couvre : ``import app.agent.legacy.x``, ``from app.agent.legacy.x import …``
    et les imports dynamiques (`importlib.import_module` / ``__import__`` avec
    un littéral de chaîne legacy).
    """
    hits: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and _is_legacy_target(node.module):
            hits.append(f"l.{node.lineno} from {node.module} import …")
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if _is_legacy_target(alias.name):
                    hits.append(f"l.{node.lineno} import {alias.name}")
        elif isinstance(node, ast.Call):
            func = node.func
            is_importlib_module = isinstance(func, ast.Attribute) and func.attr == "import_module"
            is_dunder_import = isinstance(func, ast.Name) and func.id == "__import__"
            if (is_importlib_module or is_dunder_import) and node.args:
                first = node.args[0]
                if (
                    isinstance(first, ast.Constant)
                    and isinstance(first.value, str)
                    and _is_legacy_target(first.value)
                ):
                    hits.append(f"l.{node.lineno} import dynamique de {first.value!r}")
    return hits


def test_no_direct_legacy_import_in_production() -> None:
    """Aucun import direct de ``app.agent.legacy.*`` hors de la façade strangler."""
    offenders: list[str] = []
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        module = _module_name(path)
        if _is_legacy_target(module):
            continue  # le paquet v1 peut s'importer lui-même (runtime interne)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        hits = _legacy_violations(tree)
        if hits and module not in ALLOWED_LEGACY_IMPORTERS:
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}: " + "; ".join(hits))
    assert not offenders, (
        "Import direct de app.agent.legacy détecté hors de la façade strangler.\n"
        "Aucun nouveau code ne doit dépendre directement du runtime v1 :\n"
        "  - migrer vers le noyau v2 (app.agent.core / app.agent.factory) ;\n"
        "  - sinon, passer par app.application.agent_cache (seul module autorisé).\n\n"
        + "\n".join(offenders)
    )


def test_allowed_legacy_importers_only_shrinks() -> None:
    """La liste des importeurs autorisés doit rester {@agent_cache} jusqu'à disparition."""
    assert ALLOWED_LEGACY_IMPORTERS == {"app.application.agent_cache"}, (
        "La liste des importeurs autorisés a changé : la migration est censée la "
        "RÉDUIRE jusqu'à la supprimer (retrait de app/agent/legacy). Migrer le "
        "nouveau consommateur vers le noyau v2 — ne PAS l'ajouter ici."
    )


def test_scanner_is_not_vacuous() -> None:
    """Le scanner détecte bien chaque forme interdite (anti-régression du garde)."""
    samples = [
        "from app.agent.legacy.orchestrator import MultiAgentCoordinator\n",
        "import app.agent.legacy.runner\n",
        "importlib.import_module('app.agent.legacy.agent_core')\n",
        "__import__('app.agent.legacy.prompts')\n",
    ]
    for sample in samples:
        hits = _legacy_violations(ast.parse(sample, filename="<échantillon>"))
        assert hits, f"Le scanner n'a pas détecté l'échantillon : {sample.strip()!r}"
