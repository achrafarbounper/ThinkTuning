"""Exceptions du domaine — hiérarchie unique partagée par API, agent et services.

Principes :
    - ``DomainError`` est la racine : toute erreur métier en hérite, ce qui
      permet aux routes FastAPI de mapper exceptions -> codes HTTP en un seul
      handler (fail-fast, messages explicites, pas d'AttributeError 500).
    - ``AgentError`` couvre la couche agentique (plan, approbation, sandbox) :
      distincte des erreurs d'infrastructure (LLM, stores) pour que le runner
      puisse décider retry vs recovery vs rejet.
    - Chaque erreur porte un ``code`` stable (exposé tel quel dans les
      réponses JSON) : les clients et le dashboard s'y accrochent.
"""

from __future__ import annotations

from typing import Any


class DomainError(Exception):
    """Racine de toutes les erreurs métier du projet."""

    code: str = "domain_error"
    http_status: int = 500

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = details or {}

    def to_payload(self) -> dict[str, Any]:
        """Représentation JSON stable pour les réponses d'erreur API."""
        return {"error": {"code": self.code, "message": self.message, "details": self.details}}


# ============================================================
# VALIDATION / RESSOURCES
# ============================================================


class ValidationError(DomainError):
    """Entrée invalide au sens métier (au-delà de la validation Pydantic)."""

    code = "validation_error"
    http_status = 422


class NotFoundError(DomainError):
    """Ressource introuvable (job, session, version de modèle...)."""

    code = "not_found"
    http_status = 404


class ConflictError(DomainError):
    """État incompatible avec l'opération (ex. job déjà annulé)."""

    code = "conflict"
    http_status = 409


# ============================================================
# AGENT
# ============================================================


class AgentError(DomainError):
    """Racine des erreurs de la couche agentique."""

    code = "agent_error"
    http_status = 500


class PlanRejectedError(AgentError):
    """Le plan proposé par le LLM viole une politique (outil inconnu,
    argument interdit, budget dépassé, sandbox). Non retryable en l'état :
    le plan doit être corrigé ou soumis à approbation manuelle."""

    code = "plan_rejected"
    http_status = 400


class ApprovalRequiredError(AgentError):
    """Le plan exige une approbation humaine (manual approval) qui n'a pas
    encore été accordée. Contient l'identifiant d'approbation à résoudre."""

    code = "approval_required"
    http_status = 202

    def __init__(self, approval_id: str, message: str = "Approbation manuelle requise") -> None:
        super().__init__(message, details={"approval_id": approval_id})
        self.approval_id = approval_id


class ToolExecutionError(AgentError):
    """Échec d'exécution d'un outil. Retryable selon la classification
    de l'erreur (cf. ia/agent/reliability.py : transient vs permanent)."""

    code = "tool_execution_error"
    http_status = 502


class SandboxViolationError(AgentError):
    """Tentative d'évasion de la sandbox (chemin hors racine autorisée,
    commande interdite, limite de ressources franchie). Bloquant et audité."""

    code = "sandbox_violation"
    http_status = 403


class LLMClientError(AgentError):
    """Échec du client LLM (timeout, 5xx provider, réponse non parseable).
    Retryable via le circuit breaker si l'erreur est transient."""

    code = "llm_client_error"
    http_status = 502


class BudgetExceededError(AgentError):
    """Budget du run dépassé (rounds LLM max, appels d'outils max, coût)."""

    code = "budget_exceeded"
    http_status = 429


class AgentRunError(AgentError):
    """Échec non récupérable d'un run agentique (noyau v2 ou legacy) :
    le run a été clôturé en ``error`` côté run_store avant la levée."""

    code = "agent_run_error"
    http_status = 502
