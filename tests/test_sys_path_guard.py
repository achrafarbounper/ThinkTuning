# project/tests/test_sys_path_guard.py
"""Garde-fous CI contre les hacks ``sys.path`` (roadmap 🔥#3).

Contexte : le legacy vit avec une double identité d'import (``ia.tools`` /
``tools``, ``ia.agent`` / ``agent``) maintenue par des inserts de ``ia/``
dans ``sys.path`` (``api/main.py``, ``core/agent_cache.py``, conftest, tests).
Le nouveau noyau (``app/``) ne doit JAMAIS dépendre de ces hacks.

Trois gardes :
    1. statique : aucun ``sys.path`` manipulé dans ``app/**.py`` ;
    2. statique : aucune identité legacy nue (``import tools``, ``from agent…``)
       dans ``app/**.py`` — uniquement des imports de paquets réels
       (``ia.agent.context``, ``core.run_store``…) ;
    3. dynamique : tous les modules du noyau v2 s'importent dans un
       sous-processus dont le ``sys.path`` ne contient que la racine du
       projet (PYTHONPATH purgé), et aucun import n'ajoute ``ia/`` au chemin.

Le garde 3 est le test de garde à proprement parler : si quelqu'un réintroduit
un hack nécessaire à l'import du noyau v2, la CI échoue ici. Un quatrième test
marque la dette restante (identité ``tools`` nue) à retourner en Phase 2.

Comment lancer : pytest tests/test_sys_path_guard.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
APP_DIR = PROJECT_ROOT / "app"

# Modules du noyau v2 (aucun ne doit nécessiter le hack ia/).
CORE_V2_MODULES = [
    "app.config.settings",
    "app.domain.errors",
    "app.domain.entities.plan",
    "app.domain.ports.ports",
    "app.agent.core",
    "app.agent.factory",
    "app.application.run_lifecycle",
    "app.application.session_memory",
    "app.application.ask_usecase",
    "app.infrastructure.legacy_registry",
    "app.infrastructure.legacy_approval_store",
]

# Identités d'import legacy « nues » (requièrent le hack sys.path) —
# interdites dans la couche app : le noyau v2 passe par les paquets réels
# (``ia.…``, ``core.…``) ou par les ports du domaine.
_BARE_LEGACY_ROOTS = ("tools", "agent", "copilot", "logging_setup")

_SUBPROCESS_SCRIPT = """
import importlib
import os
import sys

ROOT = sys.argv[1]
sys.path.insert(0, ROOT)

for name in sys.argv[2:]:
    importlib.import_module(name)

# Aucun module importé ne doit avoir inséré le dossier ia/ dans le chemin.
hacks = [
    p for p in sys.path
    if os.path.basename(os.path.normpath(p or ".")) == "ia"
]
if hacks:
    print("SYS_PATH_HACK:" + repr(hacks))
    sys.exit(3)
print("OK")
"""


def _app_python_files():
    for path in APP_DIR.rglob("*.py"):
        if "__pycache__" not in path.parts:
            yield path


def _ast_violations(path: Path, mode: str) -> list[str]:
    """Trouve les violations dans l'AST d'un fichier.

    ``mode="sys_path"``  : accès à l'attribut ``sys.path`` ;
    ``mode="legacy"``    : import des identités legacy nues
    (``tools``, ``agent``, ``copilot``, ``logging_setup``).
    L'AST élimine les faux positifs (docstrings, commentaires).
    """
    import ast

    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits: list[str] = []
    for node in ast.walk(tree):
        if mode == "sys_path":
            if (
                isinstance(node, ast.Attribute)
                and node.attr == "path"
                and isinstance(node.value, ast.Name)
                and node.value.id == "sys"
            ):
                hits.append(f"ligne {node.lineno}")
        elif mode == "legacy":
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                root = name.split(".")[0]
                if root in _BARE_LEGACY_ROOTS:
                    hits.append(f"ligne {node.lineno}: {name}")
    return hits


def test_app_layer_never_touches_sys_path() -> None:
    """Garde statique : la couche app ne manipule jamais ``sys.path``."""
    offenders = []
    for path in _app_python_files():
        for hit in _ast_violations(path, "sys_path"):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{hit}")
    assert not offenders, (
        f"Hack sys.path détecté dans le noyau v2 (à remplacer par un port ou "
        f"un adaptateur injecté) : {offenders}"
    )


def test_app_layer_never_uses_bare_legacy_identities() -> None:
    """Garde statique : pas d'``import tools`` / ``from agent …`` nus dans app/.

    Ces identités n'existent que grâce aux inserts de ``ia/`` dans
    ``sys.path`` ; le noyau v2 importe les paquets réels (``ia.tools…``,
    ``ia.agent…``) ou dépend des ports du domaine.
    """
    offenders = []
    for path in _app_python_files():
        for hit in _ast_violations(path, "legacy"):
            offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{hit}")
    assert not offenders, f"Identité d'import legacy nue dans app/ : {offenders}"


def test_core_v2_imports_without_ia_sys_path_hack() -> None:
    """Garde dynamique (sous-processus) : les modules du noyau v2 s'importent
    avec un ``sys.path`` vierge de tout hack ``ia/``.

    Si ce test échoue avec SYS_PATH_HACK, un import du noyau dépend
    implicitement du dossier ``ia/`` ajouté au chemin : passer par un port /
    un import de paquet réel (``ia.…``) au lieu d'un insert de sys.path.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = ""  # purge : aucune contamination externe
    result = subprocess.run(
        [
            sys.executable, "-c", _SUBPROCESS_SCRIPT,
            str(PROJECT_ROOT), *CORE_V2_MODULES,
        ],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, (
        f"Import du noyau v2 sans hack sys.path impossible.\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr[-2000:]}"
    )
    assert result.stdout.strip().endswith("OK")


def test_bare_legacy_identities_are_gone_without_the_hack() -> None:
    """Garde dynamique (Phase 2 réalisée) : les identités legacy nues
    (``tools``, ``agent``, ``copilot``, ``logging_setup``) ne doivent plus
    exister — seuls les paquets réels ``ia.tools``, ``ia.agent``, ``ia.copilot``
    et ``ia.logging_setup`` sont importables, sans aucun hack ``sys.path``.

    Historique : avant la Phase 2, ``import tools`` ne réussissait qu'avec
    ``ia/`` ajouté au chemin (par ``api/main.py``, ``core/agent_cache.py``,
    conftest ou les tests). Ce test verrouille la suppression du hack :
    toute réintroduction d'une identité nue (nouveau module racine, shim)
    fera échouer la CI ici.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = ""
    script = (
        "import sys; sys.path.insert(0, sys.argv[1])\n"
        "ok = []\n"
        "for name in ('tools', 'agent', 'copilot', 'logging_setup'):\n"
        "    try:\n"
        "        __import__(name)\n"
        "        ok.append(name)\n"
        "    except ImportError:\n"
        "        pass\n"
        "print('BARE_IDENTITIES:' + ','.join(ok) if ok else 'BARE_IDENTITIES:none')\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script, str(PROJECT_ROOT)],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr[-1000:]
    assert "BARE_IDENTITIES:none" in result.stdout, (
        "Identité d'import legacy nue détectée sans hack sys.path : "
        f"{result.stdout.strip()}. Utiliser le paquet réel « ia.* » — toute "
        "nouvelle identité racine recrée la double identité d'import "
        "(deux instances de module pour le même fichier)."
    )

