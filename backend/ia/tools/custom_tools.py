"""Tools personnalisés d'exemple (SCRUM-99) : ``run_shell`` + ``call_api``.

Le ticket exige quatre examples de tools déclarés au standard
``thinktuning.tool/v1`` : ``read_file`` et ``write_file`` existent déjà
(``system_tools.py`` / ``file_tools.py``) ; ce module ajoute :

    - ``run_shell`` : exécute une commande SÛRE via ``run_command`` — liste
      d'arguments (une chaîne est découpée via ``shlex``), allowlist de
      binaires (AGENT_ALLOWED_BINARIES), JAMAIS de shell, timeout plafonné,
      sorties tronquées. ``dry_run=True`` (ou env ``AGENT_TOOLS_DRY_RUN``)
      renvoie ``{"would_run": [...], "dry_run": true}`` SANS exécuter :
      les tests d'intégration ne dépendent d'aucun environnement ;
    - ``call_api``  : HTTP générique GET/POST (délègue à ``http_get`` /
      ``http_post`` — schémas http/https, garde SSRF optionnelle, corps JSON
      ou brut, sortie tronquée).

``EXAMPLE_TOOL_DEFINITIONS`` porte les DÉFINITIONS standard v1 des quatre
tools d'exemple (``safety`` = le pire cas de la policy effective) ; un test
anti-divergence vérifie leur cohérence avec ``tools_config.json``.
"""

from __future__ import annotations

import os
import shlex
from typing import Any, Dict, List, Optional

from .network_tools import http_get, http_post
from .shell_tools import run_command

DEFAULT_SHELL_TIMEOUT_S = 60.0
DEFAULT_API_TIMEOUT_S = 30.0
_TRUE_VALUES = ("1", "true", "yes", "on")


def _dry_run_requested(dry_run: Optional[bool]) -> bool:
    """Dry-run explicite OU global via AGENT_TOOLS_DRY_RUN (mode tests/CI)."""
    if dry_run is not None:
        return bool(dry_run)
    return os.getenv("AGENT_TOOLS_DRY_RUN", "").strip().lower() in _TRUE_VALUES


def _normalize_command(command: Any) -> List[str]:
    """Liste d'arguments (une chaîne est découpée via shlex — jamais de shell)."""
    if isinstance(command, str):
        try:
            command = shlex.split(command)
        except ValueError as exc:
            raise ValueError(f"Commande intraitable (shlex) : {exc}") from exc
    if isinstance(command, tuple):
        command = list(command)
    if not isinstance(command, list) or not command:
        raise ValueError(
            "'command' doit être une liste d'arguments non vide "
            "(ex: [\"git\", \"--version\"])."
        )
    if not all(isinstance(arg, str) for arg in command):
        raise ValueError("'command' doit être une liste de chaînes.")
    return command


def run_shell(
    command: Any,
    timeout: float = DEFAULT_SHELL_TIMEOUT_S,
    cwd: Optional[str] = None,
    dry_run: Optional[bool] = None,
) -> Dict[str, Any]:
    """Exécute une commande SÛRE (allowlist, sans shell) — voir ``run_command``.

    ``command`` : liste d'arguments (ex. ``["git", "--version"]``) OU chaîne
    découpée via ``shlex`` (aucun métacaractère shell interprété). ``dry_run``
    renvoie la commande qui serait exécutée SANS la lancer (avec le verdict
    d'allowlist), pour prévisualiser et pour les tests.
    """
    argv = _normalize_command(command)

    if _dry_run_requested(dry_run):
        # Aucune exécution : on vérifie quand même l'allowlist (déterministe,
        # sans effet de bord) pour refléter ce qui se passerait.
        from .sandbox import check_command_allowed

        try:
            check_command_allowed(argv)
            allowed, reason = True, ""
        except PermissionError as exc:
            allowed, reason = False, str(exc)
        return {
            "command": argv,
            "would_run": argv,
            "dry_run": True,
            "allowed": allowed,
            **({"reason": reason} if reason else {}),
        }

    return run_command(argv, timeout=timeout, cwd=cwd)




def call_api(
    url: str,
    method: str = "GET",
    headers: Optional[Dict[str, str]] = None,
    body: Optional[str] = None,
    json_payload: Optional[Dict[str, Any]] = None,
    timeout: float = DEFAULT_API_TIMEOUT_S,
    max_chars: int = 8000,
) -> Dict[str, Any]:
    """Appel HTTP générique GET/POST (délègue à ``http_get``/``http_post``).

    GET sans corps ; POST avec ``body`` brut OU ``json_payload`` (exclusifs,
    garde déjà présente dans http_post). Le code HTTP est renvoyé tel quel
    (pas d'exception sur 4xx/5xx) pour que l'agent puisse raisonner dessus.
    """
    normalized = str(method or "GET").strip().upper()
    if normalized not in ("GET", "POST"):
        raise ValueError("« method » doit être GET ou POST.")
    if normalized == "GET":
        if body is not None or json_payload is not None:
            raise ValueError(
                "Une requête GET n'accepte pas de corps (body/json_payload) : "
                "utilisez method=\"POST\"."
            )
        return http_get(url, headers=headers, timeout=timeout, max_chars=max_chars)
    return http_post(
        url, data=body, json_payload=json_payload, headers=headers,
        timeout=timeout, max_chars=max_chars,
    )




# ---------------------------------------------------------------------------
# Définitions standard thinktuning.tool/v1 des quatre tools d'exemple.
# ``safety`` documente le PIRE CAS de la policy effective (la classification
# fine d'approvals.py peut auto-approuver des sous-cas de lecture : GET,
# git status, dry_run…). Un test anti-divergence vérifie la cohérence avec
# tools_config.json (name / required_args / parameters).
# ---------------------------------------------------------------------------
EXAMPLE_TOOL_DEFINITIONS: Dict[str, Dict[str, Any]] = {
    "read_file": {
        "$schema": "thinktuning.tool/v1",
        "name": "read_file",
        "description": (
            "Lit un fichier texte (UTF-8) DANS la sandbox ; tronque au-delà "
            "de max_bytes."
        ),
        "version": "1.0",
        "category": "file",
        "required_args": ["path"],
        "parameters": {
            "path": {
                "type": "string",
                "required": True,
                "description": "Chemin DANS la sandbox (relatif ou absolu confiné).",
            },
            "max_bytes": {
                "type": "integer",
                "required": False,
                "default": 65536,
                "description": "Octets max lus (tronqué au-delà).",
            },
        },
        "safety": {"level": "safe", "requires_approval": False},
    },
    "write_file": {
        "$schema": "thinktuning.tool/v1",
        "name": "write_file",
        "description": (
            "Écrit `content` DANS la sandbox (chemins relatifs résolus depuis "
            "la racine autorisée, évasion hors racine refusée)."
        ),
        "version": "1.0",
        "category": "file",
        "required_args": ["filename", "content"],
        "parameters": {
            "filename": {
                "type": "string",
                "required": True,
                "description": "Nom du fichier cible DANS la sandbox.",
            },
            "content": {
                "type": "string",
                "required": True,
                "description": "Contenu UTF-8 à écrire.",
            },
            "max_bytes": {
                "type": "integer",
                "required": False,
                "default": None,
                "description": "Plafond d'octets (refuse l'écriture au-delà).",
            },
        },
        "safety": {"level": "restricted", "requires_approval": True},
    },
    "run_shell": {
        "$schema": "thinktuning.tool/v1",
        "name": "run_shell",
        "description": (
            "Exécute une commande SÛRE en liste d'arguments (allowlist "
            "AGENT_ALLOWED_BINARIES, jamais de shell, timeout plafonné). "
            "dry_run=true renvoie la commande qui serait exécutée sans la "
            "lancer."
        ),
        "version": "1.0",
        "category": "shell",
        "required_args": ["command"],
        "parameters": {
            "command": {
                "type": "array",
                "required": True,
                "description": (
                    "Liste d'arguments, ex [\"git\", \"--version\"] ; une "
                    "chaîne est découpée via shlex (pas de shell)."
                ),
            },
            "timeout": {
                "type": "number",
                "required": False,
                "default": 60.0,
                "description": "Timeout en secondes (plafonné à 600).",
            },
            "cwd": {
                "type": "string",
                "required": False,
                "default": None,
                "description": "Répertoire de travail, confiné à la sandbox.",
            },
            "dry_run": {
                "type": "boolean",
                "required": False,
                "default": False,
                "description": "Prévisualiser sans exécuter.",
            },
        },
        "allowed_binaries": ["git", "python", "pip", "pytest", "docker", "node"],
        "safety": {"level": "restricted", "requires_approval": True},
    },
    "call_api": {
        "$schema": "thinktuning.tool/v1",
        "name": "call_api",
        "description": (
            "Appel HTTP générique GET/POST vers une API externe (schéma "
            "http/https, sortie tronquée). GET sans corps ; POST avec `body` "
            "brut ou `json_payload` (exclusifs)."
        ),
        "version": "1.0",
        "category": "api",
        "required_args": ["url"],
        "parameters": {
            "url": {
                "type": "string",
                "required": True,
                "description": "URL http(s) cible.",
            },
            "method": {
                "type": "string",
                "required": False,
                "default": "GET",
                "enum": ["GET", "POST"],
                "description": "Méthode HTTP (GET par défaut).",
            },
            "headers": {
                "type": "object",
                "required": False,
                "default": None,
                "description": "En-têtes HTTP (objet {nom: valeur}).",
            },
            "body": {
                "type": "string",
                "required": False,
                "default": None,
                "description": "Corps brut (POST uniquement).",
            },
            "json_payload": {
                "type": "object",
                "required": False,
                "default": None,
                "description": "Corps JSON (POST, exclusif avec body).",
            },
            "timeout": {
                "type": "number",
                "required": False,
                "default": 30.0,
                "description": "Timeout en secondes (1 à 120).",
            },
            "max_chars": {
                "type": "integer",
                "required": False,
                "default": 8000,
                "description": "Plafond de caractères de la réponse.",
            },
        },
        "safety": {"level": "restricted", "requires_approval": True},
    },
}

# @@CHUNK_END@@


