# project/tests/test_application_purity.py
"""Garde d'architecture : ``application/`` n'importe pas ``infrastructure/`` (B-3).

Règle d'or hexagonale (``ARCHITECTURE.md``, ADR-0003) : la couche application
ne dépend que du domaine (ports). Ce test scanne AST tous les modules de
``app/application`` (top-level ET lazy — ``ast.walk`` couvre les imports dans
les fonctions) et interdit tout ``import app.infrastructure``.

Processus strangler (ADR-0003 §1 + §4) :
  - les exceptions restantes sont DÉCLARÉES et DATÉES (échéance de
    purification) — ``test_exceptions_not_expired`` échoue dès qu'une
    échéance est atteinte (ratchet inversé : la liste ne peut que rétrécir) ;
  - ``test_exception_count_never_grows`` fige le plafond courant (14) — toute
    NOUVELLE dépendance ``application → infrastructure`` doit passer par un
    port du domaine, jamais par un ajout à la liste ;
  - ``test_purified_modules_stay_pure`` verrouille les modules purifiés en
    B-3 (``model_activation`` et ``model_sanity`` via le port
    ``ModelActivationPort`` ; ``model_signing`` requalifié en
    ``infrastructure/security/``) : ils ne doivent jamais y revenir.
"""

from __future__ import annotations

import ast
from datetime import date
from pathlib import Path

import pytest

APPLICATION_DIR = Path(__file__).resolve().parents[1] / "app" / "application"

# Échéance commune de la tranche B-3 suite (documentée dans BACKLOG.md) :
# les modules ci-dessous passent par des ports du domaine à l'occasion de
# B-5 (absorption routes/agent) et des ratchets suivants.
EXPIRY = date(2026, 10, 15)

# Exceptions temporaires et datées (ADR-0003 §4). Clé : chemin relatif à
# ``app/application``. Valeur : (échéance, levier de purification prévu).
TEMPORARY_EXCEPTIONS: dict[str, tuple[date, str]] = {
    "agent_cache.py": (EXPIRY, "B-5 : LLMClient/settings/stores/tools -> ports (use cases v1)"),
    "agent_settings_usecase.py": (
        EXPIRY,
        "SETTING_KEYS/env_and_defaults -> extension AgentSettingsPort",
    ),
    "classifier_registry.py": (EXPIRY, "BaseClassifier ABC -> contrat domaine (port classifieur)"),
    "cycle_runner.py": (
        EXPIRY,
        "get_annotation_store/job_store/list_model_versions -> ports providers",
    ),
    "explain_agent.py": (EXPIRY, "HttpLLMClient -> LLMClientPort (injection)"),
    "intent_trainer.py": (EXPIRY, "intent_store/job_store -> ports providers"),
    "pipeline_runner.py": (EXPIRY, "job_store/resolve_model_path -> ports"),
    "predictor_cache.py": (
        EXPIRY,
        "Predictor/resolve_model_dir -> PredictionPort + ModelActivationPort",
    ),
    "run_lifecycle.py": (EXPIRY, "APPROVED -> constante de domaine (approval statuses)"),
    "scheduler.py": (EXPIRY, "get_job_store -> port provider"),
    "session_memory.py": (EXPIRY, "context/session_store -> ports (note de migration en tête)"),
    "copilot/suggestions.py": (EXPIRY, "feedback_store/tool_discovery/tool_registry -> ports"),
    "trainer_runner.py": (EXPIRY, "dataset/model/trainer + job_store -> TrainingRunnerPort"),
    "model_warmup.py": (EXPIRY, "BaseClassifier -> port classifieur"),
}

# Modules purifiés en B-3 — verrouillés hors exceptions.
PURIFIED_MODULES = ("model_activation.py", "model_sanity.py")

# Plafond courant du nombre d'exceptions (ratchet inversé, BACKLOG B-3).
MAX_EXCEPTIONS = 14


def _iter_infrastructure_imports(path: Path) -> list[str]:
    """Tous les imports ``app.infrastructure.*`` d'un fichier (top + lazy)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.infrastructure"):
                    found.append(f"ligne {node.lineno}: import {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("app.infrastructure"):
                found.append(f"ligne {node.lineno}: from {node.module} import ...")
    return found


def test_no_infrastructure_import_in_application() -> None:
    """0 import ``app.infrastructure.*`` dans ``application/`` hors exceptions datées."""
    violations: list[str] = []
    for py in sorted(APPLICATION_DIR.rglob("*.py")):
        if "__pycache__" in py.parts:
            continue
        rel = py.relative_to(APPLICATION_DIR).as_posix()
        imports = _iter_infrastructure_imports(py)
        if not imports:
            continue
        if rel in TEMPORARY_EXCEPTIONS:
            continue
        violations.append(f"{rel}: " + "; ".join(imports))
    assert not violations, (
        "Imports app.infrastructure interdits dans application/ (ADR-0003) :\n"
        + "\n".join(violations)
        + "\n→ injecter un port du domaine (ou requalifier le module en "
        "infrastructure s'il est technique) — ne PAS ajouter d'exception "
        "sans échéance datée."
    )


def test_purified_modules_stay_pure() -> None:
    """Les modules purifiés en B-3 ne reviennent JAMAIS dans les exceptions."""
    for module in PURIFIED_MODULES:
        assert module not in TEMPORARY_EXCEPTIONS, (
            f"{module} a été purifié (B-3) : il ne doit pas être ré-exceptionné"
        )
        assert _iter_infrastructure_imports(APPLICATION_DIR / module) == [], (
            f"{module} a réintroduit une dépendance infrastructure"
        )


def test_exceptions_not_expired() -> None:
    """Toute exception doit avoir une échéance strictement future."""
    today = date.today()
    expired = [m for m, (due, _) in TEMPORARY_EXCEPTIONS.items() if due <= today]
    assert not expired, (
        f"Exceptions périmées ({today}) — purifier ces modules ou repousser "
        f"explicitement l'échéance en justifiant : {expired}"
    )


def test_exception_count_never_grows() -> None:
    """Ratchet inversé : la liste d'exceptions ne peut que rétrécir."""
    assert len(TEMPORARY_EXCEPTIONS) <= MAX_EXCEPTIONS, (
        f"{len(TEMPORARY_EXCEPTIONS)} exceptions > plafond {MAX_EXCEPTIONS} : "
        "toute nouvelle dépendance application→infrastructure doit passer par "
        "un port du domaine (ADR-0003 §2), pas par une exception."
    )


def test_exception_targets_exist() -> None:
    """Une exception devenue inutile (module pur) doit être purgée de la liste."""
    stale = [
        m for m in TEMPORARY_EXCEPTIONS if not _iter_infrastructure_imports(APPLICATION_DIR / m)
    ]
    assert not stale, (
        f"Exceptions devenues inutiles (module pur ou supprimé) — purger la liste : {stale}"
    )


def test_purified_modules_exist() -> None:
    """Sanity : les modules purifiés existent toujours (sinon scan muet)."""
    for module in PURIFIED_MODULES:
        assert (APPLICATION_DIR / module).is_file(), f"{module} introuvable"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__]))
