# project/app/infrastructure/mcp/security/scope_enforcer.py
"""Enforceur de sécurité MCP — scopes, quotas et rate limiting (S4, tâche 11).

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tâche 11) :
    - ``check_scope(client_id, tool_name)``   → vérifie ``visible_tools``
      (whitelist du client) / catalogue du rôle — fail-closed ;
    - ``check_quota(client_id, tool_name)``   → vérifie ``destructive_quota``
      (fenêtre glissante d'une heure, outils « manual approval ») ;
    - ``check_rate_limit(client_id)``         → vérifie ``rate_limit_per_minute``
      (``TokenBucket`` PARTAGÉ avec ``api/middlewares/rate_limit.py``) ;
    - 4 rôles : ``read_only`` (12) < ``contributor`` (25) < ``operator`` (35)
      < ``admin`` (40) ;
    - test : ``tests/test_mcp_scope_enforcer.py`` (chaque rôle + dépassements).

Architecture (hexagonale, docs/mcp/MCP_IMPLEMENTATION_MAPPING.md) :
    - module d'INFRASTRUCTURE : il ne consomme que le domaine (``MCPSecurityScope``,
      ``MCPScopeRole``) et des adapteurs ; il ne connaît ni FastAPI, ni transport
      MCP, ni ``api`` (import lourd interdit — la suite MCP reste légère) ;
    - le store SQLite (``core/mcp_client_store``) est résolu PAresseusement via
      ``default_scope_resolver`` : importer ce module ne crée AUCUNE base ;
    - quota et buckets rate-limit sont des états en MÉMOIRE, thread-safe (RLock)
      et bornés (éviction des entrées inactives) — mêmes conventions que
      ``api/middlewares/rate_limit.py``.

Sémantique (fail-closed) :
    - client inconnu ou révoqué → ``MCPAccessDeniedError`` (aucun oracle) ;
    - rôle inconnu              → ``MCPAccessDeniedError`` (comparaison stricte) ;
    - tool hors portée          → ``MCPAccessDeniedError``. La portée effective
      d'un client est : whitelist ``visible_tools`` si non vide, sinon le
      catalogue de son rôle (docs/mcp/MCP_SECURITY.md) ;
    - outil destructif au-delà du quota horaire → ``MCPQuotaExceededError`` ;
      le slot est RÉSERVÉ au moment du check (gate) : deux requêtes concurrentes
      ne peuvent pas dépasser le quota ensemble ;
    - débit client dépassé → ``MCPRateLimitExceededError`` (``retry_after``).

Catalogues par rôle (tailles cibles roadmap — docs/mcp/MCP_SECURITY.md) :
    - ``READ_ONLY_ROLE_TOOLS`` : sous-ensemble « 12 tools lecture » de la
      sélection v0.1.0 (``V010_READ_ONLY_TOOLS`` nomme 13 noms ; le label
      roadmap « 12 » arrondissait — on retire ``file_checksum``, outil de
      hachage non essentiel au périmètre lecture) ;
    - ``CONTRIBUTOR_ROLE_TOOLS`` : surface v1.0.0 = ``V100_READ_ONLY_TOOLS``
      (25 tools read-only, tâche 7) ;
    - ``OPERATOR_ROLE_TOOLS``    : contributor + 10 tools write/exec (tâche 17 :
      write_file, write_json, append_file, make_dir, copy_path, run_command,
      run_python, start_training, cancel_training, stop_training) ;
    - ``ADMIN_ROLE_TOOLS``       : operator + 5 tools (tâche 19 : move_path,
      remove_path, split_file, dedupe_lines, unzip_file).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from app.domain.entities.mcp import MCPScopeRole
from app.domain.ports.mcp_ports import MCPSecurityScope
from app.infrastructure.mcp.legacy_tool_provider import (
    V010_READ_ONLY_TOOLS,
    V100_READ_ONLY_TOOLS,
)
from app.infrastructure.mcp.manifest_generator import compile_tool
from app.infrastructure.mcp.security.rate_limit_bucket import TokenBucket

logger = logging.getLogger("thinktuning.mcp.security")

# Fenêtre du quota destructif : « manual approval » par HEURE (docs/mcp/MCP_SECURITY.md).
DEFAULT_QUOTA_WINDOW_SECONDS: float = 3600.0

# Bornes mémoire du rate limit per-client (mêmes conventions que le middleware REST :
# purge anti-fuite — chaque client id crée un bucket jamais libéré si l'on n'évacue pas).
_MAX_CLIENT_BUCKETS = 1024
_IDLE_BUCKET_SECONDS = 600.0

# ============================================================================
# Catalogues par rôle (tailles roadmap 12 / 25 / 35 / 40)
# ============================================================================

# read_only : les 12 tools « lecture » du jalon v0.1.0. ``V010_READ_ONLY_TOOLS``
# (tâche 6) nomme 13 noms ; la roadmap arrondissait le label à « 12 » — on retire
# ``file_checksum`` (hachage), outil non essentiel au périmètre lecture.
READ_ONLY_ROLE_TOOLS: frozenset[str] = frozenset(
    V010_READ_ONLY_TOOLS - {"file_checksum"}
)

# contributor : surface publique v1.0.0 = les 25 tools read-only (tâche 7).
CONTRIBUTOR_ROLE_TOOLS: frozenset[str] = frozenset(V100_READ_ONLY_TOOLS)

# operator : +10 tools write/exec (tâche 17 — écriture sandbox + pilotage
# d'entraînement). Annotations mutation (APPROVE humain) à chaque appel, et
# quota destructif appliqué ici (le seul rôle à voir du muté et de l'exec).
_OPERATOR_EXTENSION: frozenset[str] = frozenset(
    {
        "write_file",
        "write_json",
        "append_file",
        "make_dir",
        "copy_path",
        "run_command",
        "run_python",
        "start_training",
        "cancel_training",
        "stop_training",
    }
)
OPERATOR_ROLE_TOOLS: frozenset[str] = frozenset(
    CONTRIBUTOR_ROLE_TOOLS | _OPERATOR_EXTENSION
)

# admin : +5 tools (tâche 19 — gestion mutante de fichiers/archives).
_ADMIN_EXTENSION: frozenset[str] = frozenset(
    {"move_path", "remove_path", "split_file", "dedupe_lines", "unzip_file"}
)
ADMIN_ROLE_TOOLS: frozenset[str] = frozenset(
    OPERATOR_ROLE_TOOLS | _ADMIN_EXTENSION
)

# Mapping rôle (str, valeur de ``MCPScopeRole``) → catalogue par défaut.
ROLE_TOOLS: dict[str, frozenset[str]] = {
    MCPScopeRole.READ_ONLY.value: READ_ONLY_ROLE_TOOLS,
    MCPScopeRole.CONTRIBUTOR.value: CONTRIBUTOR_ROLE_TOOLS,
    MCPScopeRole.OPERATOR.value: OPERATOR_ROLE_TOOLS,
    MCPScopeRole.ADMIN.value: ADMIN_ROLE_TOOLS,
}

# ============================================================================
# Erreurs de l'enforceur
# ============================================================================


class MCPEnforcerError(Exception):
    """Erreur de base de l'enforceur de sécurité MCP (tâche 11)."""


class MCPAccessDeniedError(MCPEnforcerError):
    """Le client ne peut VOIR ni APPELER le tool (scope, révocation, rôle).

    Fail-closed : un client inconnu, révoqué, avec un rôle inconnu, ou hors
    whitelist/catalogue est indiscernable d'un accès refusé (aucun oracle).
    """


class MCPQuotaExceededError(MCPEnforcerError):
    """Quota « manual approval » / heure épuisé pour ce client (``destructive_quota``)."""


class MCPRateLimitExceededError(MCPEnforcerError):
    """Débit client dépassé (``rate_limit_per_minute``) — rejouer après ``retry_after`` s."""

    def __init__(self, message: str, *, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


# ============================================================================
# Classification « destructif » (posture compilée du manifeste, anti-divergence)
# ============================================================================

_destructive_cache: dict[str, bool] = {}
_destructive_lock = threading.Lock()


def is_destructive_tool(name: str) -> bool:
    """Un tool est « destructif » (soumis au quota) si sa posture compilée est mutation.

    Compilation paresseuse MÉMOISÉE depuis le registre déclaratif legacy
    (``compile_tool`` → ``annotations.destructiveHint`` — la même source que le
    manifeste MCP officiel, tâche 4 : zéro règle dupliquée). Fail-closed : un
    tool inconnu OU non compilable est traité comme destructif (le doute n'est
    jamais résolu en faveur du quota).
    """
    cached = _destructive_cache.get(name)
    if cached is not None:
        return cached
    from ia.tools import tool_registry as _legacy_registry

    try:
        compiled, _ = compile_tool(name, _legacy_registry.TOOL_META.get(name))
        destructive = bool(compiled["annotations"]["destructiveHint"])
    except Exception:  # nom inconnu / entrée illisible → posture la plus stricte
        destructive = True
    with _destructive_lock:
        _destructive_cache[name] = destructive
    return destructive


# ============================================================================
# Portée effective + résolution de scope
# ============================================================================


def effective_tools(scope: MCPSecurityScope) -> frozenset[str]:
    """Portée effective d'un client : whitelist si non vide, sinon catalogue du rôle.

    Docs/mcp/MCP_SECURITY.md : ``visible_tools`` est la whitelist EXPLICITE ;
    « vide » signifie « tous les tools autorisés par le rôle » (catalogue). Un
    rôle inconnu renvoie un catalogue VIDE → fail-closed à l'appel.
    """
    if scope.visible_tools:
        return frozenset(scope.visible_tools)
    return ROLE_TOOLS.get(scope.role, frozenset())


def resolve_scope(
    client_id: str,
    *,
    resolver: Callable[[str], MCPSecurityScope | None] | None = None,
) -> MCPSecurityScope:
    """Résout et VALIDE le scope d'un client (fail-closed).

    Lève ``MCPAccessDeniedError`` si le client est inconnu, révoqué, ou porte
    un rôle hors catalogue — avant toute vérification de tool.
    """
    client_id = client_id.strip()
    scope = (resolver or default_scope_resolver)(client_id)
    if scope is None:
        raise MCPAccessDeniedError(f"Client MCP inconnu ou révoqué : {client_id!r}")
    if not scope.is_active:
        raise MCPAccessDeniedError(f"Client MCP révoqué : {client_id!r}")
    if scope.role not in ROLE_TOOLS:
        raise MCPAccessDeniedError(f"Rôle MCP inconnu : {scope.role!r}")
    return scope


# ============================================================================
# Résolveur par défaut — MCPClientStore (lazy, aucune base au module import)
# ============================================================================

_client_store_instance: Any | None = None
_client_store_lock = threading.Lock()


def _default_client_store() -> Any:
    """Instance unique paresseuse du ``MCPClientStore`` (source des scopes)."""
    global _client_store_instance
    with _client_store_lock:
        if _client_store_instance is None:
            from core.mcp_client_store import get_mcp_client_store

            _client_store_instance = get_mcp_client_store()
        return _client_store_instance


def default_scope_resolver(client_id: str) -> MCPSecurityScope | None:
    """Résout le scope via le registre SQLite ; inconnu/révoqué → ``None``."""
    from core.mcp_client_store import (
        MCPClientNotFoundError,
        MCPClientRevokedError,
    )

    try:
        return _default_client_store().get_scope(client_id)
    except (MCPClientNotFoundError, MCPClientRevokedError):
        return None


# ============================================================================
# Enforceur composé (état mémoire thread-safe)
# ============================================================================


class MCPScopeEnforcer:
    """Enforceur composé scope + quota + rate limit (thread-safe, état en mémoire).

    Args:
        scope_resolver:       résout ``{client_id: MCPSecurityScope | None}`` ;
        destructive_tools:    prédicat ``(tool_name) -> bool`` (défaut :
            ``is_destructive_tool`` — posture compilée du manifeste) ;
        quota_window_seconds: fenêtre glissante du quota destructif (1h défaut) ;
        clock:                source de temps de la fenêtre de quota (tests).
    """

    def __init__(
        self,
        *,
        scope_resolver: Callable[[str], MCPSecurityScope | None],
        destructive_tools: Callable[[str], bool] | None = None,
        quota_window_seconds: float = DEFAULT_QUOTA_WINDOW_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._scope_resolver = scope_resolver
        self._is_destructive = destructive_tools or is_destructive_tool
        self._quota_window = quota_window_seconds
        self._clock = clock
        self._lock = threading.RLock()
        self._quota_usage: dict[str, deque[float]] = {}
        self._buckets: dict[str, TokenBucket] = {}
        self._buckets_seen: dict[str, float] = {}

    def allowed_tools(self, client_id: str) -> frozenset[str]:
        """Ensemble des tools effectivement autorisés (lève ``MCPAccessDeniedError``)."""
        scope = resolve_scope(client_id, resolver=self._scope_resolver)
        return effective_tools(scope)

    def check_scope(self, client_id: str, tool_name: str) -> None:
        """Tâche 11 : vérifie que ``tool_name`` est dans la portée effective du client."""
        scope = resolve_scope(client_id, resolver=self._scope_resolver)
        if tool_name not in effective_tools(scope):
            raise MCPAccessDeniedError(
                f"Tool {tool_name!r} non autorisé pour le client {client_id!r} "
                f"(rôle {scope.role!r})"
            )

    def check_quota(self, client_id: str, tool_name: str) -> None:
        """Tâche 11 : réserve un slot du quota destructif horaire (« manual approval »).

        Fail-closed : client inconnu/révoqué → erreur même pour un tool de
        lecture ; quota épuisé → ``MCPQuotaExceededError``. Les tools de
        lecture pure ne consomment JAMAIS de quota. Le slot est réservé au
        moment du check (gate) : deux requêtes concurrentes ne peuvent pas
        dépasser le quota ensemble.
        """
        scope = resolve_scope(client_id, resolver=self._scope_resolver)
        if not self._is_destructive(tool_name):
            return
        now = self._clock()
        with self._lock:
            usage = self._quota_usage.setdefault(client_id, deque())
            # Fenêtre glissante : purge des slots tombés hors de l'heure.
            while usage and now - usage[0] >= self._quota_window:
                usage.popleft()
            if len(usage) >= scope.destructive_quota:
                raise MCPQuotaExceededError(
                    f"Quota destructif épuisé pour {client_id!r} : "
                    f"{scope.destructive_quota} / {int(self._quota_window // 60)} min "
                    f"sur {tool_name!r}"
                )
            usage.append(now)

    def check_rate_limit(self, client_id: str) -> None:
        """Tâche 11 : vérifie le débit per-client (``rate_limit_per_minute``).

        Mêmes conventions que ``api/middlewares/rate_limit.py`` : un bucket par
        client (clé = ``client_id`` ici, IP là-bas), recréé si la capacité a
        changé, éviction des buckets inactifs pour borner la mémoire.
        """
        scope = resolve_scope(client_id, resolver=self._scope_resolver)
        limit = scope.rate_limit_per_minute
        with self._lock:
            self._evict_idle_buckets_locked()
            bucket = self._buckets.get(client_id)
            if bucket is None or bucket.capacity != max(1, limit):
                bucket = TokenBucket(max(1, limit))
                self._buckets[client_id] = bucket
            self._buckets_seen[client_id] = self._clock()
            allowed, wait_seconds = bucket.consume(1.0)
            if not allowed:
                raise MCPRateLimitExceededError(
                    f"Rate limit dépassé pour {client_id!r} "
                    f"({scope.rate_limit_per_minute} appels/minute)",
                    retry_after=wait_seconds,
                )

    def enforce(self, client_id: str, tool_name: str) -> None:
        """Portail sécurité complet : scope → quota → rate limit (ordre plan tâche 11).

        Une seule ligne à appeler au transport MCP (SSE/stdio) avant un
        ``tools/call`` — lève la première garde franchie.
        """
        self.check_scope(client_id, tool_name)
        self.check_quota(client_id, tool_name)
        self.check_rate_limit(client_id)

    def _evict_idle_buckets_locked(self) -> None:
        """Purge les buckets inactifs (à appeler avec ``_lock`` posé)."""
        now = self._clock()
        if len(self._buckets) < _MAX_CLIENT_BUCKETS:
            return
        stale = [
            key
            for key, seen in self._buckets_seen.items()
            if now - seen > _IDLE_BUCKET_SECONDS
        ]
        for key in stale:
            self._buckets.pop(key, None)
            self._buckets_seen.pop(key, None)
        # Toujours saturé (clients actifs mais récents) : évacue les plus anciens.
        if len(self._buckets) >= _MAX_CLIENT_BUCKETS:
            oldest = sorted(self._buckets_seen.items(), key=lambda item: item[1])
            for key, _seen in oldest[: len(oldest) // 2]:
                self._buckets.pop(key, None)
                self._buckets_seen.pop(key, None)


# ============================================================================
# API module (les fonctions nommées par la tâche 11)
# ============================================================================

_default_enforcer: MCPScopeEnforcer | None = None
_default_enforcer_lock = threading.Lock()


def get_default_enforcer(
    *,
    resolver: Callable[[str], MCPSecurityScope | None] | None = None,
) -> MCPScopeEnforcer:
    """Enforceur partagé (résolveur store par défaut).

    Un ``resolver`` explicitement injecté force une instance dédiée (tests,
    isolation) — l'enforceur partagé reste inchangé.
    """
    global _default_enforcer
    if resolver is not None:
        return MCPScopeEnforcer(scope_resolver=resolver)
    with _default_enforcer_lock:
        if _default_enforcer is None:
            _default_enforcer = MCPScopeEnforcer(scope_resolver=default_scope_resolver)
        return _default_enforcer


def reset_default_enforcer() -> None:
    """Remet à zéro l'enforceur partagé (isolation des tests)."""
    global _default_enforcer
    with _default_enforcer_lock:
        _default_enforcer = None


def check_scope(
    client_id: str,
    tool_name: str,
    *,
    enforcer: MCPScopeEnforcer | None = None,
) -> None:
    """Tâche 11 : vérifie la visibilité d'un tool (whitelist ``visible_tools``/rôle).

    Portage d'état : passez un ``enforcer`` explicite (ou utilisez l'enforceur
    partagé ``get_default_enforcer()``) — le quota et le rate limit sont
    STATEFUL, jamais recréés par appel.
    """
    (enforcer or get_default_enforcer()).check_scope(client_id, tool_name)


def check_quota(
    client_id: str,
    tool_name: str,
    *,
    enforcer: MCPScopeEnforcer | None = None,
) -> None:
    """Tâche 11 : vérifie le quota destructif horaire (« manual approval »)."""
    (enforcer or get_default_enforcer()).check_quota(client_id, tool_name)


def check_rate_limit(
    client_id: str,
    *,
    enforcer: MCPScopeEnforcer | None = None,
) -> None:
    """Tâche 11 : vérifie le débit per-client (``rate_limit_per_minute``).

    L'état du bucket vit dans l'enforceur : il est obligatoire de réutiliser la
    même instance entre deux ``check_rate_limit`` (partagée ou injectée).
    """
    (enforcer or get_default_enforcer()).check_rate_limit(client_id)


def enforce(
    client_id: str,
    tool_name: str,
    *,
    enforcer: MCPScopeEnforcer | None = None,
) -> None:
    """Tâche 11 : portail complet scope → quota → rate limit pour un ``tools/call``."""
    (enforcer or get_default_enforcer()).enforce(client_id, tool_name)


__all__ = [
    "ADMIN_ROLE_TOOLS",
    "CONTRIBUTOR_ROLE_TOOLS",
    "DEFAULT_QUOTA_WINDOW_SECONDS",
    "MCPAccessDeniedError",
    "MCPEnforcerError",
    "MCPQuotaExceededError",
    "MCPRateLimitExceededError",
    "MCPScopeEnforcer",
    "OPERATOR_ROLE_TOOLS",
    "READ_ONLY_ROLE_TOOLS",
    "ROLE_TOOLS",
    "check_quota",
    "check_rate_limit",
    "check_scope",
    "default_scope_resolver",
    "effective_tools",
    "enforce",
    "get_default_enforcer",
    "is_destructive_tool",
    "reset_default_enforcer",
    "resolve_scope",
]
