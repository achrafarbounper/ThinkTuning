"""Garde anti-régression : la résolution PARESSEUSE doit marcher EN INTERNE.

Bug SCRUM-141 : ``PUT /api/v1/agent/settings`` renvoyait 500 avec
``NameError: name 'AgentRunner' is not defined`` (chemin : route v1 →
``reload_agent_runner()`` → ``_build_runner()``).

Cause : le ``__getattr__`` de niveau module (PEP 562,
``app/application/agent_cache.py``) n'est déclenché que par un accès
*attribut* sur le module depuis l'extérieur (``from … import AgentRunner``).
Une référence « nue » interne (``AgentRunner(...)``) compile en ``LOAD_GLOBAL``
qui lit directement ``globals()`` et lève ``NameError`` tant que le symbole
n'a jamais été résolu via un accès attribut. Les annotations restent inertes
grâce à ``from __future__ import annotations`` — seul le runtime plantait.

Correctif : ``agent_cache._resolve_runtime()`` effectue l'accès attribut sur
le module lui-même (``sys.modules[__name__]``) — premier appel : ``__getattr__``
importe le paquet legacy réel et met le symbole en cache ; appels suivants :
lecture directe du cache.

Trois tests :
    1. in-process : ``_resolve_runtime`` renvoie les vraies classes des paquets
       réels (aucun hack ``sys.path``) et les met en cache — état froid ÉTABLI
       par la fixture, donc insensible à l'ordre de la suite (E-16) ;
    2. anti-pollution (backlog B-2) : la résolution reste correcte après une
       pollution d'ordre ALÉATORISÉE (graine figée = reproductible), qui mime
       les accès attribut de la suite complète ;
    3. sous-processus : depuis un interpréteur FRAIS où AUCUN symbole n'a été
       résolu (précondition vérifiée), ``reload_agent_runner()`` et
       ``get_multi_agent_coordinator()`` s'exécutent sans ``NameError`` — les
       seams réseau/base sont stubbés pour rester hors ligne.

Comment lancer : pytest tests/test_agent_cache_lazy_resolution.py -v
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

#: Symboles du runtime v1 résolus paresseusement par la façade strangler.
_LAZY_SYMBOLS = ("AgentCore", "AgentRunner", "MultiAgentCoordinator")


@pytest.fixture()
def _clean_lazy_namespace():
    """Établit un namespace FROID avant le test et le nettoie après (E-16, B-2).

    ``_resolve_runtime`` met le symbole en cache dans ``globals()`` (comportement
    voulu en production). Or la suite complète résout ces symboles AVANT ce
    module (``tests/test_agent_api.py``, imports des routes v1 : un
    ``from app.application.agent_cache import AgentRunner`` déclenche
    ``__getattr__`` et met en cache) : la précondition « namespace à froid » ne
    peut plus être ASSUMÉE, elle doit être ÉTABLIE par la fixture — sinon le
    test passe seul et échoue en suite complète (pollution d'ordre, E-16).

    Le retrait du cache est transparent (avant comme après le test) : un accès
    attribut ultérieur ré-importe le module (déjà dans ``sys.modules``) et
    retrouve le MÊME objet — aucune autre couche d'état n'existe.
    """
    import app.application.agent_cache as agent_cache

    for name in _LAZY_SYMBOLS:  # setup : état froid garanti, quel que soit l'ordre
        agent_cache.__dict__.pop(name, None)
    yield
    for name in _LAZY_SYMBOLS:  # teardown : le test ne fuit pas son propre cache
        agent_cache.__dict__.pop(name, None)


def test_resolve_runtime_returns_real_classes_and_caches(_clean_lazy_namespace) -> None:
    """``_resolve_runtime`` résout les symboles depuis leurs paquets réels.

    Identité vérifiée contre les classes importées directement (autorisé en
    tests — le garde ``test_no_direct_legacy_imports`` ne scanne que ``app/``).
    """
    import app.application.agent_cache as agent_cache
    from app.agent.legacy.agent_core import AgentCore
    from app.agent.legacy.orchestrator import MultiAgentCoordinator
    from app.agent.legacy.runner import AgentRunner

    # Avant résolution : namespace à froid (le module ne charge pas le legacy).
    assert not any(name in vars(agent_cache) for name in _LAZY_SYMBOLS)

    assert agent_cache._resolve_runtime("AgentCore") is AgentCore
    assert agent_cache._resolve_runtime("AgentRunner") is AgentRunner
    assert agent_cache._resolve_runtime("MultiAgentCoordinator") is MultiAgentCoordinator

    # Cache transparent : les accès suivants (attribut inclus) renvoient le
    # même objet sans repasser par l'import paresseux.
    assert agent_cache.AgentRunner is AgentRunner
    assert agent_cache._resolve_runtime("AgentRunner") is AgentRunner


def test_lazy_resolution_is_insensitive_to_pollution_order(_clean_lazy_namespace) -> None:
    """Anti-pollution (E-16 / backlog B-2) : résolution insensible à l'ordre.

    La suite complète résout les symboles paresseux AVANT ce module (accès
    attribut : ``tests/test_agent_api.py``, imports des routes v1). Ce test
    reproduit cette pollution dans un ordre ALÉATORISÉ (graine figée = échec
    reproductible en cas de régression) et vérifie, pour chaque mélange :
    1. l'état chaud attendu (cache ``__getattr__``, comportement voulu) ;
    2. la ré-établissabilité de l'état froid par le SEUL retrait du cache
       (contrat de la fixture) — aucune couche d'état cachée ;
    3. la résolution à froid retrouve les VRAIES classes (identité stable).
    """
    import random

    import app.application.agent_cache as agent_cache
    from app.agent.legacy.agent_core import AgentCore
    from app.agent.legacy.orchestrator import MultiAgentCoordinator
    from app.agent.legacy.runner import AgentRunner

    real_classes = {
        "AgentCore": AgentCore,
        "AgentRunner": AgentRunner,
        "MultiAgentCoordinator": MultiAgentCoordinator,
    }
    rng = random.Random(20260914)  # graine figée : reproductibilité des mélanges
    for _ in range(4):
        order = list(_LAZY_SYMBOLS)
        rng.shuffle(order)

        # 1. Pollution : accès attribut dans un ordre quelconque (ce que fait
        #    la suite). Chaque accès déclenche ``__getattr__`` → mise en cache.
        for name in order:
            assert getattr(agent_cache, name) is real_classes[name]
        assert all(name in vars(agent_cache) for name in _LAZY_SYMBOLS)

        # 2. Ré-établissement de l'état froid : le retrait du cache doit suffire.
        for name in _LAZY_SYMBOLS:
            agent_cache.__dict__.pop(name, None)
        assert not any(name in vars(agent_cache) for name in _LAZY_SYMBOLS)

        # 3. La résolution repart à froid et retrouve les vraies classes.
        for name, cls in real_classes.items():
            assert agent_cache._resolve_runtime(name) is cls
        assert agent_cache.AgentRunner is AgentRunner


_SUBPROCESS_SCRIPT = """
import sys

sys.path.insert(0, sys.argv[1])

import app.application.agent_cache as ac

# Précondition : namespace À FROID — AUCUN symbole du runtime v1 résolu
# (sinon le scénario de production n'est pas reproduit : on échoue
# explicitement plutôt que de faire passer un test vide).
unresolved = [name for name in sys.argv[2:] if name not in vars(ac)]
if unresolved != list(sys.argv[2:]):
    print("PRECONDITION_FAIL:" + repr(unresolved))
    sys.exit(4)


class _FakeLLMClient:  # hors ligne : LLMClient remplacé par un double inerte
    def __init__(self, *args, **kwargs):
        pass


def _fake_agent_config():  # hors ligne : aucune lecture de base
    return {
        "provider": "ollama",
        "ollama_url": "http://127.0.0.1:1/api/chat",
        "openrouter_url": "",
        "openrouter_api_key": "",
        "hf_url": "",
        "hf_api_key": "",
        "lm_studio_url": "",
        "model": "repro-model",
        "timeout": 1.0,
        "context_length": 64,
        "temperature": None,
    }


# Seams stubbés : la construction ne touche ni le réseau ni la base —
# seules les résolutions paresseuses (AgentCore / AgentRunner /
# MultiAgentCoordinator) restent RÉELLES, ce sont elles qu'on vérifie.
ac.agent_config = _fake_agent_config
ac.LLMClient = _FakeLLMClient

# Avant correctif : reload_agent_runner() plantait en NameError ('AgentRunner')
# et get_multi_agent_coordinator() en NameError ('MultiAgentCoordinator').
runner = ac.reload_agent_runner()
coord = ac.get_multi_agent_coordinator()
print("OK:" + type(runner).__name__ + ":" + type(coord).__name__)
"""


def test_runner_and_coordinator_build_from_cold_namespace() -> None:
    """Sous-processus frais : runner + coordinateur se construisent sans NameError.

    Reproduit le scénario SCRUM-141 : un serveur qui démarre, reçoit
    ``PUT /api/v1/agent/settings`` (→ ``reload_agent_runner()``) alors qu'aucun
    accès attribut externe n'a encore résolu les symboles paresseux.
    """
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    env["PYTHONPATH"] = ""  # purge : aucune contamination externe
    env["AGENT_MULTI_INTENT"] = "0"  # pas de classifieur torch/ONNX hors ligne
    result = subprocess.run(
        [sys.executable, "-c", _SUBPROCESS_SCRIPT, str(PROJECT_ROOT), *_LAZY_SYMBOLS],
        capture_output=True,
        text=True,
        cwd=str(PROJECT_ROOT),
        env=env,
        timeout=120,
    )
    assert result.returncode == 0, (
        "Construction runner/coordinateur depuis un namespace à froid impossible"
        " (NameError attendu si régression SCRUM-141).\n"
        f"stdout: {result.stdout}\nstderr: {result.stderr[-2000:]}"
    )
    assert result.stdout.strip().endswith("OK:AgentRunner:MultiAgentCoordinator")
