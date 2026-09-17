# project/app/infrastructure/mcp/error_contract.py
"""Contrat d'erreurs structuré MCP (v2.3.0).

Uniformise la projection des erreurs MCP dans le champ JSON-RPC 2.0
``error.data`` (voie standard d'extension, sans breaking change pour les
clients 2.2.x) avec les champs :

    errorType           famille stable (``MCPErrorType``) — les clients et le
                        dashboard s'y accrochent, jamais au message libre ;
    retryable           le client peut-il retenter tel quel ;
    retryAfterSeconds   délai minimal de réessai (présent UNIQUEMENT si un
                        délai est connu — backpressure, rate limit) ;
    correlationId       identifiant de corrélation généré par requête —
                        relie la réponse d'erreur aux logs serveur et à l'audit ;
    fieldErrors         détails PAR CHAMP (validation de paramètres, policy).

Règles de sécurité (non négociables) :
    - les messages exposés au client sont SANITISÉS : jamais de chemin local
      (Windows ou POSIX), jamais de secret (clé API, bearer, mot de passe) ;
    - les ``fieldErrors`` héritent de la même sanitisation ;
    - le champ ``data`` est PUREMENT descriptif : ``code``/``message`` JSON-RPC
      restent la source de vérité du protocole (aucune divergence 2.2.x).

Module PUR : aucune I/O, aucune dépendance transport (SSE/stdio), aucun
framework — instanciable et testable isolément (``tests/test_mcp_error_contract.py``).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.domain.errors import (
    BudgetExceededError,
    DomainError,
    GatewayTimeoutError,
    LLMClientError,
    NotFoundError,
    PlanRejectedError,
    PolicyUnavailableError,
    SandboxViolationError,
    ServiceUnavailableError,
    ValidationError,
)


class MCPErrorType(StrEnum):
    """Familles stables d'erreurs MCP (contrat 2.3.0).

    L'énumération est FERMÉE : toute nouvelle famille passe par une revue
    (les clients testent ``errorType`` en égalité, pas en absence).
    """

    VALIDATION = "validation_error"
    POLICY = "policy_error"
    TIMEOUT = "timeout_error"
    RATE_LIMITED = "rate_limited"
    NOT_FOUND = "not_found"
    INTERNAL = "internal_error"


#: Réessayabilité PAR DÉFAUT de chaque famille (fail-closed : on ne re-tente
#: jamais sans preuve que c'est sûr — backpressure et rate limit seules familles
#: éphémères, timeout inclus car idempotence MCP garantie côté serveur).
_FAMILY_RETRYABLE: dict[MCPErrorType, bool] = {
    MCPErrorType.VALIDATION: False,
    MCPErrorType.POLICY: False,
    MCPErrorType.TIMEOUT: True,
    MCPErrorType.RATE_LIMITED: True,
    MCPErrorType.NOT_FOUND: False,
    MCPErrorType.INTERNAL: False,
}

# ---------------------------------------------------------------------------
# Sanitisation — jamais de secret ni de chemin local dans une réponse MCP
# ---------------------------------------------------------------------------

# Chemins Windows (``C:\Users\...\fichier.py``) — lettres de lecteur + suites.
_WINDOWS_PATH = re.compile(r"[A-Za-z]:\\(?:[^\\/\s\"']+\\)*[^\\/\s\"']+")
# Chemins POSIX sensibles (homes, racines système, workspaces). Les URI
# ``thinktuning://`` ne sont PAS touchées (schéma explicite ≠ chemin absolu).
_POSIX_PATH = re.compile(r"(?:/home|/root|/Users|/var|/tmp|/workspace)(?:/[^\s\"'`,;:]+)+")
# Tokens « Bearer <valeur> » (schéma Authorization standard — la valeur suit).
_SECRET_BEARER = re.compile(r"(?i)\bbearer\s+[^\s,;\"']+")
# Secrets étiquetés (``api_key=…``, ``Authorization: …``, ``token: …``).
_SECRET_KEY = re.compile(
    r"(?i)\b(api[_-]?key|authorization|password|passwd|secret|token)\b"
    r"(\s*[=:]\s*)[^\s,;\"']+"
)

_REDACTED_PATH = "[redacted-path]"
_REDACTED_SECRET = "[redacted]"


def sanitize_message(message: str) -> str:
    """Masque chemins locaux et secrets d'un message destiné au client MCP.

    Ordre important : les SECRETS d'abord (un secret peut être encodé dans un
    chemin), puis les chemins Windows, puis POSIX.
    """
    redacted = _SECRET_BEARER.sub("Bearer [redacted]", message)
    redacted = _SECRET_KEY.sub(lambda m: f"{m.group(1)}{m.group(2)}{_REDACTED_SECRET}", redacted)
    redacted = _WINDOWS_PATH.sub(_REDACTED_PATH, redacted)
    redacted = _POSIX_PATH.sub(_REDACTED_PATH, redacted)
    return redacted


def new_correlation_id() -> str:
    """Identifiant de corrélation court (12 hex) — logs + audit + réponse."""
    return uuid.uuid4().hex[:12]


# ---------------------------------------------------------------------------
# Erreur structurée (valeur immuable, projection ``error.data``)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MCPStructuredError:
    """Erreur MCP typée projetée dans le champ JSON-RPC ``error.data``.

    Attributs :
        error_type : famille stable (``MCPErrorType``) ;
        message : message SANITISÉ (jamais de chemin local ni de secret) ;
        retryable : le client peut retenter tel quel (défaut par famille) ;
        retry_after_seconds : délai de réessai en secondes — ``None`` sauf si
            un délai CONNU est porté (backpressure, rate limit) ;
        correlation_id : identifiant de corrélation (``""`` tant que la
            requête n'a pas été taguée — le serveur injecte le sien) ;
        field_errors : détails par champ ``{champ: [messages]}`` (sanitisés).
    """

    error_type: MCPErrorType
    message: str = ""
    retryable: bool | None = None
    retry_after_seconds: int | None = None
    correlation_id: str = ""
    field_errors: Mapping[str, list[str]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        # frozen dataclass : mutation contrôlée via ``object.__setattr__`` —
        # la sanitisation est une NORMALISATION, pas un changement d'état.
        object.__setattr__(self, "message", sanitize_message(self.message))
        object.__setattr__(
            self,
            "field_errors",
            {
                str(key): [sanitize_message(str(item)) for item in values]
                for key, values in self.field_errors.items()
            },
        )
        if self.retry_after_seconds is not None:
            # Un délai de réessai ne peut être ni négatif ni nul.
            object.__setattr__(self, "retry_after_seconds", max(1, int(self.retry_after_seconds)))

    @property
    def effective_retryable(self) -> bool:
        """Réessayabilité effective (défaut par famille si non explicite)."""
        if self.retryable is not None:
            return self.retryable
        return _FAMILY_RETRYABLE[self.error_type]

    def to_data(self) -> dict[str, Any]:
        """Projection ``error.data`` — champs optionnels OMIS quand absents.

        Contrat : ``errorType`` et ``retryable`` sont TOUJOURS présents ;
        ``correlationId`` n'est inclus que s'il est connu (le serveur l'injecte
        après coup) ; ``retryAfterSeconds`` uniquement avec un délai réel ;
        ``fieldErrors`` uniquement si au moins un champ est détaillé.
        """
        data: dict[str, Any] = {
            "errorType": self.error_type.value,
            "retryable": self.effective_retryable,
        }
        if self.retry_after_seconds is not None:
            data["retryAfterSeconds"] = self.retry_after_seconds
        if self.correlation_id:
            data["correlationId"] = self.correlation_id
        if self.field_errors:
            data["fieldErrors"] = {key: list(values) for key, values in self.field_errors.items()}
        return data


# ---------------------------------------------------------------------------
# Fabriques — mapping déterministe exceptions → erreur structurée
# ---------------------------------------------------------------------------


def structured_validation(
    message: str = "",
    *,
    field_errors: Mapping[str, list[str]] | None = None,
    correlation_id: str = "",
) -> MCPStructuredError:
    """Erreur de VALIDATION (paramètres client réparables — non retryable)."""
    return MCPStructuredError(
        MCPErrorType.VALIDATION,
        message=message,
        field_errors=field_errors or {},
        correlation_id=correlation_id,
    )


def structured_policy(
    message: str = "",
    *,
    field_errors: Mapping[str, list[str]] | None = None,
    correlation_id: str = "",
) -> MCPStructuredError:
    """Erreur de POLICY (scope, quota destructif, sandbox, budget, approbation).

    Non retryable en l'état : corriger la demande, demander une approbation
    ou changer de scope — JAMAIS de rejeu borgne (un refus de policy
    re-tenté identique reste un refus).
    """
    return MCPStructuredError(
        MCPErrorType.POLICY,
        message=message,
        field_errors=field_errors or {},
        correlation_id=correlation_id,
    )


def structured_timeout(
    message: str = "",
    *,
    retry_after_seconds: int | None = None,
    correlation_id: str = "",
) -> MCPStructuredError:
    """Erreur de TIMEOUT (dépendance lente) — retryable (idempotence MCP)."""
    return MCPStructuredError(
        MCPErrorType.TIMEOUT,
        message=message,
        retry_after_seconds=retry_after_seconds,
        correlation_id=correlation_id,
    )


def structured_rate_limited(
    message: str = "",
    *,
    retry_after_seconds: int,
    correlation_id: str = "",
) -> MCPStructuredError:
    """Erreur de DÉBIT/CAPACITÉ — retryable avec délai contractuel."""
    return MCPStructuredError(
        MCPErrorType.RATE_LIMITED,
        message=message,
        retryable=True,
        retry_after_seconds=retry_after_seconds,
        correlation_id=correlation_id,
    )


def structured_not_found(
    message: str = "",
    *,
    field_errors: Mapping[str, list[str]] | None = None,
    correlation_id: str = "",
) -> MCPStructuredError:
    """Cible introuvable (resource URI, prompt) — non retryable."""
    return MCPStructuredError(
        MCPErrorType.NOT_FOUND,
        message=message,
        field_errors=field_errors or {},
        correlation_id=correlation_id,
    )


def structured_internal(
    message: str = "Internal error",
    *,
    correlation_id: str = "",
) -> MCPStructuredError:
    """Défaut serveur — message GÉNÉRIQUE (aucune fuite d'implémentation)."""
    return MCPStructuredError(MCPErrorType.INTERNAL, message=message, correlation_id=correlation_id)


#: Exceptions de policy du domaine → famille POLICY (refus, jamais un rejeu).
_POLICY_ERRORS: tuple[type[DomainError], ...] = (
    PolicyUnavailableError,
    SandboxViolationError,
    BudgetExceededError,
    PlanRejectedError,
)

_TIMEOUT_ERRORS: tuple[type[DomainError], ...] = (GatewayTimeoutError, LLMClientError)


def structured_from_domain_error(
    exc: DomainError, *, correlation_id: str = ""
) -> MCPStructuredError:
    """Mapping déterministe ``DomainError`` → erreur structurée MCP.

    - ``ValidationError`` → validation (détails du domaine recopiés par champ) ;
    - ``NotFoundError`` → not_found ;
    - policy (sandbox, budget, plan rejeté, PDP indisponible) → policy ;
    - timeout / LLM client (transient) → timeout, retryable ;
    - service momentanément indisponible → timeout (éphémère, retryable) ;
    - tout le reste → internal, message générique (aucune fuite).
    """
    if isinstance(exc, ValidationError):
        details = exc.details or {}
        field_errors = {str(key): [sanitize_message(str(value))] for key, value in details.items()}
        return structured_validation(
            exc.message, field_errors=field_errors, correlation_id=correlation_id
        )
    if isinstance(exc, NotFoundError):
        return structured_not_found(exc.message, correlation_id=correlation_id)
    if isinstance(exc, _POLICY_ERRORS):
        return structured_policy(exc.message, correlation_id=correlation_id)
    if isinstance(exc, (*_TIMEOUT_ERRORS, ServiceUnavailableError)):
        return structured_timeout(exc.message, correlation_id=correlation_id)
    return structured_internal(correlation_id=correlation_id)


def structured_from_enforcer_error(
    exc: Exception, *, correlation_id: str = ""
) -> MCPStructuredError:
    """Mapping des erreurs de l'enforceur MCP (scope, quota, débit).

    Import local (paresseux) : le paquet ``security`` reste léger ; les
    classes ne sont nécessaires qu'à l'exécution d'un refus réel.
    """
    from app.infrastructure.mcp.security.scope_enforcer import (  # noqa: PLC0415
        MCPAccessDeniedError,
        MCPQuotaExceededError,
        MCPRateLimitExceededError,
    )

    if isinstance(exc, MCPRateLimitExceededError):
        return structured_rate_limited(
            str(exc),
            retry_after_seconds=max(1, int(exc.retry_after)),
            correlation_id=correlation_id,
        )
    if isinstance(exc, (MCPQuotaExceededError, MCPAccessDeniedError)):
        return structured_policy(str(exc), correlation_id=correlation_id)
    return structured_internal(correlation_id=correlation_id)


def fallback_from_rpc_code(
    code: int, message: str, *, correlation_id: str = ""
) -> MCPStructuredError:
    """Contrat minimal pour une erreur JSON-RPC NON encore taguée.

    Filet de sécurité du serveur : toute erreur émise sans ``errorType``
    (parse, invalid request, méthode inconnue…) reçoit un contrat cohérent
    dérivé du code JSON-RPC — les codes -32xxx « client » sont réparables
    (validation), ``-32603`` est un défaut serveur (internal).
    """
    from app.infrastructure.mcp.protocol import ErrorCode  # noqa: PLC0415

    if code == ErrorCode.INTERNAL_ERROR:
        return structured_internal(correlation_id=correlation_id)
    if code == ErrorCode.METHOD_NOT_FOUND:
        return structured_not_found(sanitize_message(message), correlation_id=correlation_id)
    return structured_validation(sanitize_message(message), correlation_id=correlation_id)


__all__ = [
    "MCPErrorType",
    "MCPStructuredError",
    "fallback_from_rpc_code",
    "new_correlation_id",
    "sanitize_message",
    "structured_from_domain_error",
    "structured_from_enforcer_error",
    "structured_internal",
    "structured_not_found",
    "structured_policy",
    "structured_rate_limited",
    "structured_timeout",
    "structured_validation",
]
