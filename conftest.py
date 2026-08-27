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
