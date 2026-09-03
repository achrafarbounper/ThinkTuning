"""Ports de sortie du domaine — contrats vers l'infrastructure et l'agent.

Un **port** est une interface abstraite définie par le domaine : les cas
d'usage dépendent de ces contrats, jamais des implémentations concrètes
(SQLite, Ollama/OpenRouter, registre d'outils). C'est le point de la
hexagonale qui permet :

    - de tester les use-cases avec des fakes en mémoire ;
    - de remplacer le stockage (SQLite -> Postgres) sans toucher au domaine ;
    - de brancher le client LLM réel OU un mock déterministe.

Alignement : chaque Protocol reprend les signatures réelles des modules
legacy qu'il encapsulera à la migration (core/session_store.py,
core/audit_store.py, core/run_store.py, core/approval_store.py,
ia/agent/llm_client.py, ia/tools/tool_registry.py). Les stores legacy
implémentent déjà ces signatures : un simple adaptateur suffira.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

Message = dict[str, Any]  # format OpenAI : {"role": ..., "content": ...}


@runtime_checkable
class LLMClientPort(Protocol):
    """Contrat du client LLM (cf. ia/agent/llm_client.py, contrat identique).

    ``call()`` réassemble la réponse complète ; ``call_stream()`` consomme le
    flux et invoque les callbacks temps réel (thinking / content). Lève
    ``app.domain.errors.LLMClientError`` en cas d'échec (le retry/circuit
    breaker reste de la responsabilité de l'implémentation).
    """

    def call(self, messages: list[Message]) -> str:
        """Réponse complète (str) pour un historique de messages."""
        ...

    def call_stream(
        self,
        messages: list[Message],
        on_thinking: Callable[[str], None] | None = None,
        on_content: Callable[[str], None] | None = None,
    ) -> str:
        """Réponse complète en consommant le flux, callbacks temps réel."""
        ...


@runtime_checkable
class ToolRegistryPort(Protocol):
    """Contrat du registre d'outils (cf. ia/tools/tool_registry.py).

    Le domaine ne connaît que l'existence d'un outil, ses métadonnées et
    l'exécution validée ; la gestion du JSON déclaratif et de la sandbox est
    de la responsabilité de l'implémentation.
    """

    def tool_names(self) -> list[str]:
        """Noms des outils enregistrés (pour le system prompt du planner)."""
        ...

    def get(self, tool: str) -> Callable[..., Any] | None:
        """Fonction exécutable d'un outil, ou None si inconnu."""
        ...

    def meta(self, tool: str) -> dict[str, Any] | None:
        """Métadonnées déclaratives (description, required_args, parameters)."""
        ...


@runtime_checkable
class SessionStorePort(Protocol):
    """Contrat mémoire conversationnelle (cf. core/session_store.py).

    Short-term : messages de la session. Long-term : résumés par clé
    (``save_memory``/``get_memory``).
    """

    def create_session(self, title: str = "", model: str = "") -> dict[str, Any]:
        """Crée une session, renvoie sa représentation dict."""
        ...

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        ...

    def list_sessions(self, limit: int = 100) -> list[dict[str, Any]]:
        ...

    def rename_session(self, session_id: str, title: str) -> dict[str, Any] | None:
        ...

    def delete_session(self, session_id: str) -> bool:
        ...

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        tool_calls: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any] | None:
        """Ajoute un message (None si session inexistante, ValueError rôle invalide)."""
        ...

    def get_messages(self, session_id: str, limit: int = 200) -> list[dict[str, Any]]:
        ...

    # --- Mémoire long-term -------------------------------------------------
    def save_memory(self, key: str, summary: str) -> None:
        ...

    def get_memory(self, key: str) -> str:
        ...

    def delete_memory(self, key: str) -> None:
        ...


@runtime_checkable
class AuditStorePort(Protocol):
    """Contrat du journal d'audit (cf. core/audit_store.py, signature ``log`` identique)."""

    def log(
        self,
        action: str,
        subject: str = "",
        detail: dict[str, Any] | None = None,
        actor: str = "system",
        ip: str | None = None,
        request_id: str | None = None,
        run_id: str | None = None,
    ) -> dict[str, Any]:
        """Ajoute une entrée d'audit (détail anonymisé/tronqué à l'écriture)."""
        ...

    def get(self, audit_id: str) -> dict[str, Any] | None:
        ...

    def query(self, **filters: Any) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class RunStorePort(Protocol):
    """Contrat de traçabilité des runs agent (cf. core/run_store.py).

    Signatures alignées sur l'implémentation legacy : un adaptateur conforme
    doit accepter exactement ces arguments (les tests de contrat vérifient
    l'alignement par introspection des signatures)."""

    def start_run(
        self, prompt: str, model: str = "", source: str = "api"
    ) -> dict[str, Any]:
        """Ouvre un run ``running`` et renvoie sa ligne (dict, clé ``id``)."""
        ...

    def append_tool_event(self, run_id: str, event: dict[str, Any]) -> None:
        """Journalise un événement d'outil (tool_start / tool_result / error)."""
        ...

    def finish_run(
        self,
        run_id: str,
        status: str,
        answer_summary: str = "",
        error: str | None = None,
    ) -> dict[str, Any] | None:
        """Clôture le run (statut final, résumé de réponse ou erreur)."""
        ...

    def get(self, run_id: str) -> dict[str, Any] | None:
        ...

    def list(
        self,
        limit: int = 50,
        status: str | None = None,
        tool: str | None = None,
    ) -> list[dict[str, Any]]:
        ...


@runtime_checkable
class ApprovalStorePort(Protocol):
    """Contrat de la file d'approbation humaine (cf. core/approval_store.py).

    Flux : ``create`` (status pending) -> ``approve``/``reject`` -> le runner
    reprend l'action si ``approved``. Le filtrage passe par ``list(status)``.

    Convention ``create`` : l'adaptateur de référence
    (``app/infrastructure/legacy_approval_store``) renvoie la LIGNE créée
    (dict avec ``request_id``) et non l'identifiant brut du legacy — les
    use-cases s'appuient sur ``record.get("request_id")``."""

    def create(
        self,
        tool: str,
        args: Any,
        category: str,
        decision: str,
        reason: str,
        prompt: str = "",
        args_hash: str = "",
        status: str = "pending",
    ) -> dict[str, Any]:
        """Enregistre une demande (pending ou trace de rejet) et renvoie la ligne."""
        ...

    def get(self, request_id: str) -> dict[str, Any] | None:
        ...

    def approve(self, request_id: str, decided_by: str | None = None) -> dict[str, Any] | None:
        ...

    def reject(self, request_id: str, decided_by: str | None = None) -> dict[str, Any] | None:
        ...

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        """Liste les demandes (toutes si status=None, sinon filtrées par statut)."""
        ...
