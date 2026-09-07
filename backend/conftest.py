"""Pytest bootstrap for ThinkTuning.

Ensures the project root is importable no matter HOW pytest is launched
(`pytest.exe`, `python -m pytest`, VS Code Test Explorer, ...) or from
which working directory. Without this, top-level imports used across the
codebase (`api`, `core`, `src.*`) only resolve when the CWD happens to be
the repository root.
"""

import os
import sys
import tempfile

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

# Clé API des tests : même convention que les modules de test, qui font tous
# `os.environ.setdefault("API_KEY", "test-key")` avant l'import de l'app.
# Posée ICI une fois pour toute la session (conftest racine = importé avant la
# collecte) afin que la clé attendue par `require_api_key` corresponde
# toujours aux en-têtes `X-API-Key: test-key` codés en dur dans les tests.
# Indispensable en CI : si le workflow définit API_KEY=<autre valeur>,
# setdefault devient un no-op et TOUTES les routes protégées répondent 401
# (134 échecs observés avec API_KEY=ci-test-key).
os.environ.setdefault("API_KEY", "test-key")

# Isolation GLOBALE des paramètres persistants de l'agent : pendant les tests,
# la base SQLite de core.agent_settings doit pointer vers un fichier temporaire
# (jamais experiments/agent_settings.db) pour qu'aucun test n'écrase la
# configuration réelle ni ne dépende d'une config sauvegardée manuellement.
# L'env var doit être posée AVANT le premier agent_config() ; le store étant
# créé paresseusement, chaque module qui en a besoin peut aussi appeler
# core.agent_settings.reset_store_for_tests(...) pour un fichier par test.
if not os.getenv("AGENT_SETTINGS_PATH"):
    os.environ["AGENT_SETTINGS_PATH"] = os.path.join(
        tempfile.gettempdir(), "thinktuning-test-agent-settings.db"
    )

# Isolation de la FILE D'APPROBATION de l'agent : pendant les tests, la base
# SQLite de core.approval_store pointe vers un fichier temporaire (jamais la
# vraie base experiments/agent_approvals.db) pour ne pas polluer ni partager
# les demandes d'un test à l'autre.
if not os.getenv("AGENT_APPROVAL_PATH"):
    os.environ["AGENT_APPROVAL_PATH"] = os.path.join(
        tempfile.gettempdir(), "thinktuning-test-agent-approvals.db"
    )

# Pas de warmup du classifieur de sentiment au démarrage du TestClient pendant
# les tests : il chargerait le modèle de 541 Mo (coût CI). Les tests du warmup
# sont isolés et posent explicitement CLASSIFIER_WARMUP=1.
os.environ.setdefault("CLASSIFIER_WARMUP", "0")
