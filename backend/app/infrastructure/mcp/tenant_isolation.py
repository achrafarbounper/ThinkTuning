# project/app/infrastructure/mcp/tenant_isolation.py
"""Isolation multi-tenant MCP (MCP 2.3.0, SCRUM-161).

Ce module est PUR (aucune I/O, aucun transport) : il porte les primitives
d'isolation consommées par le serveur, les transports et l'adapter
d'orchestration — l'isolation repose sur TROIS identifiants portés par
``MCPIdentity`` (domaine, ``app/domain/ports/mcp_ports.py``) :

    - ``tenant_id`` : partition dur (cross-tenant JAMAIS lisible) ;
    - ``client_id`` : client MCP déclaré dans le store (whitelists, quotas) ;
    - ``subject_id`` : sujet humain de bout en bout (audit + run).

Périmètre (tâche SCRUM-161 — isolation multi-tenant) :

    1. **Runs** — tout run durable est estampillé à la création avec
       l'identité de son créateur ; toute lecture/annulation/reprise/replay
       passe par ``assert_run_owner`` (cross-tenant → indiscernable d'un run
       inconnu, aucun oracle). Un run SANS estampille (legacy, antérieur à
       2.3.0) n'est accessible que depuis le tenant par défaut ;
    2. **resources/read** — la whitelist ``visible_resources`` du scope
       (jamais appliquée jusqu'ici — TODO S4/tâche 11) est appliquée par le
       serveur quand l'appelant s'identifie explicitement ;
    3. **tools/call** — ``canonical_tool_name`` résout l'ALIAS d'abord
       (``stop_training`` → ``cancel_training``) : scope, quotas et rate
       limit s'appliquent au nom CANONIQUE — un alias ne contourne jamais
       une vérification ;
    4. **Limites d'arguments ``orchestrate``** — ``check_json_arguments``
       plafonne la TAILLE sérialisée et la PROFONDEUR du JSON des arguments
       (parcours itératif : un payload profond lève une erreur nette, jamais
       une ``RecursionError``) ;
    5. **Quota de coût** — consommé par l'enforceur (fenêtre glissante par
       ``tenant:client``, même pattern que le quota destructif).

Conventions du dépôt : les limites configurables sont lues à l'appel via des
variables d'environnement (``MCP_MAX_JSON_BYTES``, ``MCP_MAX_JSON_DEPTH``) —
même pattern que ``mcp_auth_required()`` / ``run_sweeper._env_int``.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from typing import Any

from app.domain.ports.mcp_ports import MCP_DEFAULT_TENANT_ID, MCPIdentity

# --------------------------------------------------------------------------- #
# Identité — normalisation fail-closed (même convention que correlationId :    #
# ``[A-Za-z0-9._-]``, 64 max — jamais de valeur arbitraire dans audit/logs).  #
# --------------------------------------------------------------------------- #

_ID_SANITIZE = re.compile(r"[^A-Za-z0-9._-]")
MAX_ID_LENGTH = 64

#: Tenant par défaut — les appels sans en-tête d'identité explicite et les
#: runs legacy (non estampillés) appartiennent à cette partition.
#: SOURCE UNIQUE : ``app.domain.ports.mcp_ports.MCP_DEFAULT_TENANT_ID``.
DEFAULT_TENANT_ID = MCP_DEFAULT_TENANT_ID

#: Alias de tools déclarés (tâche alias) : la clé est l'alias public, la
#: valeur le nom CANONIQUE porteur de la sémantique (scope, quota, audit).
TOOL_ALIASES: Mapping[str, str] = {"stop_training": "cancel_training"}


def sanitize_identity_value(raw: Any, *, field: str, default: str = "") -> str:
    """Normalise un identifiant d'identité (nettoyage + borne de longueur).

    Les caractères hors ``[A-Za-z0-9._-]`` sont retirés (jamais rejetés : un
    client mal formé est vidé vers ``default`` — fail-closed, aucune valeur
    arbitraire ne circule dans l'audit ni dans les clés de quota).
    """
    cleaned = _ID_SANITIZE.sub("", str(raw or "").strip())[:MAX_ID_LENGTH]
    if not cleaned:
        if default:
            return default
        raise ValueError(f"identity field {field!r} cannot be empty")
    return cleaned


def canonical_tool_name(name: str) -> str:
    """Résout le nom CANONIQUE d'un tool (résolution d'alias, tâche alias).

    La résolution est itérative avec garde anti-cycle ; un nom inconnu est
    retourné TEL QUEL — la vérification de scope en aval le refuse s'il
    n'est pas déclaré (aucun bypass possible).
    """
    current = str(name or "").strip()
    seen: set[str] = set()
    while current in TOOL_ALIASES:
        if current in seen:  # cycle de déclaration : fail-safe (nom tel quel)
            break
        seen.add(current)
        current = TOOL_ALIASES[current]
    return current


# --------------------------------------------------------------------------- #
# Limites d'arguments JSON (taille + profondeur) — défense en profondeur       #
# --------------------------------------------------------------------------- #

DEFAULT_MAX_JSON_BYTES = 262_144  # 256 Ko — marge au-dessus de tout usage légitime
DEFAULT_MAX_JSON_DEPTH = 32

_ENV_JSON_BYTES = "MCP_MAX_JSON_BYTES"
_ENV_JSON_DEPTH = "MCP_MAX_JSON_DEPTH"


def _env_int(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None or not str(raw).strip():
        return default
    try:
        return max(minimum, int(str(raw).strip()))
    except (TypeError, ValueError):
        return default


def max_json_bytes() -> int:
    """Taille MAX (octets UTF-8 sérialisés) des arguments d'un ``tools/call``."""
    return _env_int(_ENV_JSON_BYTES, DEFAULT_MAX_JSON_BYTES, minimum=1024)


def max_json_depth() -> int:
    """Profondeur MAX d'imbrication des arguments d'un ``tools/call``."""
    return _env_int(_ENV_JSON_DEPTH, DEFAULT_MAX_JSON_DEPTH, minimum=4)


_ENV_ISOLATION = "MCP_TENANT_ISOLATION"


def tenant_isolation_enabled() -> bool:
    """L'isolation multi-tenant est-elle ACTIVE (défaut : oui — fail-closed) ?

    Interrupteur de rollback explicite : ``MCP_TENANT_ISOLATION=0`` (ou
    ``false``/``no``/``off``) rétablit le comportement 2.2.x pour les appels
    portant une identité déclarée — même convention que
    ``MCP_AUTH_REQUIRED`` (défaut actif, rollback documenté).
    """
    raw = os.getenv(_ENV_ISOLATION)
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


class MCPArgumentLimitError(ValueError):
    """Arguments d'un tool au-delà des limites de taille ou de profondeur JSON.

    Traduite par le serveur en ``validation_error`` avec ``fieldErrors`` —
    le client est réparable (réduire la charge utile), non retryable.
    """


def _json_size_bytes(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def _json_depth(value: Any, limit: int) -> int:
    """Profondeur max d'imbrication — parcours ITÉRATIF (jamais de crash récursif).

    Le parcours s'arrête tôt : dès qu'une branche dépasse ``limit``, la valeur
    n'est plus descendue (un payload ad-versarial profond coûte au plus
    ``O(noeuds < limit)`` par branche explorée avant rejet).
    """
    max_depth = 0
    stack: list[tuple[Any, int]] = [(value, 1)]
    while stack:
        current, depth = stack.pop()
        if depth > max_depth:
            max_depth = depth
        if max_depth >= limit:
            return max_depth
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)
    return max_depth


def check_json_arguments(
    payload: Any,
    *,
    max_bytes: int | None = None,
    max_depth: int | None = None,
) -> None:
    """Vérifie la TAILLE et la PROFONDEUR du JSON d'arguments (fail-closed).

    ``payload`` est l'objet ``arguments`` (ou le params entier) DÉJÀ parsé ;
    la taille est mesurée en octets UTF-8 sérialisés (cohérent avec le plafond
    du corps HTTP au transport). Lève ``MCPArgumentLimitError`` au premier
    seuil franchi — le message porte la mesure réelle et la limite (réparable).

    ORDRE INVARIANT : la PROFONDEUR est mesurée AVANT la taille. La mesure de
    taille (``json.dumps``) est récursive : un payload ad-versarial profond
    provoquerait une ``RecursionError`` si elle passait d'abord. Le parcours
    de profondeur est ITÉRATIF avec sortie anticipée — une fois la profondeur
    validée (``<= max_depth``), la sérialisation reste bornée et sûre.
    """
    byte_limit = max_bytes if max_bytes is not None else max_json_bytes()
    depth_limit = max_depth if max_depth is not None else max_json_depth()
    if payload is None:
        return
    depth = _json_depth(payload, depth_limit + 1)
    if depth > depth_limit:
        raise MCPArgumentLimitError(
            f"arguments exceed the JSON depth limit ({depth} > {depth_limit})"
        )
    size = _json_size_bytes(payload)
    if size > byte_limit:
        raise MCPArgumentLimitError(
            f"arguments exceed the JSON size limit ({size} > {byte_limit} bytes)"
        )


# --------------------------------------------------------------------------- #
# Isolation des runs — garde de propriété (tenant / client / sujet)            #
# --------------------------------------------------------------------------- #


class MCPRunOwnerMismatchError(PermissionError):
    """Accès à un run durable hors du périmètre de l'appelant.

    NON exposée telle quelle au client : l'adapter/transport la masque en
    « run inconnu » (aucun oracle de propriété — indiscernable d'un
    ``run_id`` inexistant).
    """


def owner_matches(identity: MCPIdentity | None, owner: MCPIdentity | None) -> bool:
    """L'identité peut-elle accéder à un run estampillé ``owner`` ?

    SOURCE UNIQUE de la décision : ``MCPIdentity.can_access`` (domaine) — ce
    module n'ajoute que la convention « appelant NON identifié → aucune
    garde » (comportement 2.2.x préservé pour les appels internes/tests).

    Sémantique (fail-closed, portée par le domaine) :

        - run SANS propriétaire (legacy, estampille vide) → accessible
          UNIQUEMENT depuis le tenant par défaut ;
        - sinon : ``tenant_id`` doit correspondre (partition dur), puis
          ``client_id`` (le propriétaire d'une reprise est le créateur), puis
          ``subject_id`` UNIQUEMENT quand les DEUX sont renseignés (un
          client sans sujet déclaré n'est pas bloqué par l'estampille
          subject d'un run qui en porte un).
    """
    if identity is None:  # appelant non identifié (tests, appel direct)
        return True
    return identity.can_access(owner)


def assert_run_owner(
    identity: MCPIdentity | None,
    owner: MCPIdentity | None,
    *,
    run_id: str = "",
) -> None:
    """Garde fail-closed : lève ``MCPRunOwnerMismatchError`` si l'accès est
    hors périmètre (l'appelant masque l'erreur en « run inconnu »)."""
    if not owner_matches(identity, owner):
        raise MCPRunOwnerMismatchError(
            f"MCP run {run_id!r} is outside the caller's tenant/client scope"
        )


__all__ = [
    "DEFAULT_MAX_JSON_BYTES",
    "DEFAULT_MAX_JSON_DEPTH",
    "DEFAULT_TENANT_ID",
    "MAX_ID_LENGTH",
    "MCPArgumentLimitError",
    "MCPRunOwnerMismatchError",
    "TOOL_ALIASES",
    "assert_run_owner",
    "canonical_tool_name",
    "check_json_arguments",
    "max_json_bytes",
    "max_json_depth",
    "owner_matches",
    "sanitize_identity_value",
    "tenant_isolation_enabled",
]
