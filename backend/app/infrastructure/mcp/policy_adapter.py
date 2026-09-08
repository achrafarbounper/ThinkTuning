# project/app/infrastructure/mcp/policy_adapter.py
"""Policy Adapter — projection runtime de la policy de sandbox → MCP (S2, tâche 5).

Rec. 7 du mapping (docs/mcp/MCP_IMPLEMENTATION_MAPPING.md) : la policy interne
existante (``app/agent/policies/sandbox_policy.py``) reste la SOURCE DE VÉRITÉ
des décisions — ce module l'ADAPTE à la surface MCP sans rien réinventer :

    sandbox_policy.decide / decide_action   (verdict auto_approve / approve / reject)
        → ``decide`` / ``decide_action``    verdict typé ``PolicyVerdict`` + raison auditée
        → ``decision_to_annotations``       projection MCP ``annotations``
          (readOnlyHint / destructiveHint / idempotentHint)

SÉPARATION DES RESPONSABILITÉS (tâche 4 ≠ tâche 5) :
    - manifest_generator (tâche 4)  : posture DESIGN-TIME, statique, sans
      arguments — catalogue déclaratif fidèle aux sources (``tools/list``) ;
    - policy_adapter (ce module)    : décision RUNTIME par appel — chemins
      sensibles, anti-SSRF, SQL mutant — + filtre de scope (``visible_tools``).

Sémantique des annotations par verdict (fail-closed, postures du manifeste) :
    AUTO_APPROVE → read-only  (read/system/network lisibles : exécution immédiate) ;
    APPROVE      → mutation   (write/delete/exec/UNKNOWN : validation humaine) ;
    REJECT       → mutation   (règle dure : l'action est traitée comme la plus
                   dangereuse — le BLOCAGE est porté par ``PolicyVerdict.decision``,
                   les hints restant des indications, pas des permissions).

Le portage MCP n'est PAS un bypass de la security interne
(docs/mcp/MCP_SECURITY.md) : ``PolicyGateToolProvider`` fait respecter le
verdict à chaque ``tools/call`` (auto → exécution ; approve → validation
humaine ; reject → refus) et ``ScopeFilteredToolProvider`` projette la vue
sécurisée des tools (``visible_tools``) — prête pour la whitelist explicite de
``MCPSecurityScope`` (S4). Les deux implémentent ``MCPToolRegistryPort`` et se
composent (gate ∘ scope) au câblage du serveur (``build_mcp_server``).
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from app.agent.policies.sandbox_policy import (
    classify_path_risk,
    is_private_host,
)
from app.agent.policies.sandbox_policy import (
    decide as sandbox_decide,
)
from app.domain.entities.mcp import MCPScopeRole, MCPTool
from app.domain.entities.plan import Action, ActionCategory, Decision
from app.domain.ports.mcp_ports import MCPToolRegistryPort
from app.infrastructure.mcp.manifest_generator import (
    MUTATING_ANNOTATIONS,
    READ_ONLY_ANNOTATIONS,
)
from app.infrastructure.mcp.mcp_server import ToolError

logger = logging.getLogger("thinktuning.mcp.policy")

__all__ = [
    "PolicyGateToolProvider",
    "PolicyVerdict",
    "ScopeFilteredToolProvider",
    "decide",
    "decide_action",
    "decision_to_annotations",
    "visible_tools",
]


# ---------------------------------------------------------------------------
# Projection verdict → annotations MCP (pure, déterministe)
# ---------------------------------------------------------------------------


def decision_to_annotations(decision: Decision) -> dict[str, bool]:
    """Mappe un verdict ``sandbox_policy`` → annotations MCP (déterministe).

    Règles (fail-closed, postures identiques au manifeste — tâche 4) :
        - ``AUTO_APPROVE`` : catégorie lisible (read/system/network) → posture
          read-only (readOnlyHint true, destructiveHint false, idempotentHint true) ;
        - ``APPROVE``      : catégorie à risque ou tool inconnu → posture
          mutation (le gate exige une validation humaine) ;
        - ``REJECT``       : règle dure → posture mutation également (les
          annotations décrivent le RISQUE, pas la permission : une action
          rejetée est traitée comme la plus dangereuse).

    Returns:
        Une NOUVELLE dict (les constantes ne sont jamais exposées mutables).
    """
    if decision is Decision.AUTO_APPROVE:
        return dict(READ_ONLY_ANNOTATIONS)
    return dict(MUTATING_ANNOTATIONS)


@dataclass(frozen=True)
class PolicyVerdict:
    """Verdict runtime de la policy pour un appel de tool MCP (gate + audit).

    Attributes:
        tool:        nom du tool appelé ;
        decision:    verdict de ``sandbox_policy`` (auto_approve/approve/reject) ;
        annotations: projection MCP du verdict (readOnlyHint/destructiveHint/idempotentHint) ;
        reason:      raison humaine auditée (journal MCP, tâche 12).
    """

    tool: str
    decision: Decision
    annotations: dict[str, bool] = field(default_factory=dict)
    reason: str = ""

    @property
    def allowed(self) -> bool:
        """Exécution immédiate, sans validation humaine (AUTO_APPROVE)."""
        return self.decision is Decision.AUTO_APPROVE

    @property
    def requires_approval(self) -> bool:
        """Validation humaine requise avant exécution (APPROVE)."""
        return self.decision is Decision.APPROVE

    @property
    def blocked(self) -> bool:
        """Action interdite par une règle dure (REJECT) — jamais exécutée."""
        return self.decision is Decision.REJECT

    def to_dict(self) -> dict[str, Any]:
        """Représentation JSON stable pour l'audit MCP (``ACT_MCP_TOOL_CALL``, tâche 12)."""
        return {
            "tool": self.tool,
            "decision": self.decision.value,
            "allowed": self.allowed,
            "requires_approval": self.requires_approval,
            "blocked": self.blocked,
            "annotations": dict(self.annotations),
            "reason": self.reason,
        }


# ---------------------------------------------------------------------------
# Décision runtime (délégation sandbox_policy — zéro règle dupliquée)
# ---------------------------------------------------------------------------


def _first_sensitive_arg(args: Mapping[str, Any]) -> str | None:
    """Premier argument visant une cible sensible (aide à l'audit, pas une règle)."""
    for value in args.values():
        if isinstance(value, str) and value and classify_path_risk(value):
            return value
    return None


def _first_url(args: Mapping[str, Any]) -> str:
    """Premier argument ressemblant à une URL (aide à l'audit anti-SSRF)."""
    return next(
        (str(value) for value in args.values() if isinstance(value, str) and "://" in value),
        "",
    )


def _verdict_reason(tool: str, args: Mapping[str, Any], decision: Decision) -> str:
    """Raison auditée du verdict — DÉRIVE l'explication, ne décide PAS.

    Les verdicts viennent exclusivement de ``sandbox_policy`` : ce helper
    réutilise ses helpers publics (``classify_path_risk``, ``is_private_host``)
    pour produire un message actionnable, sans dupliquer la logique de décision
    (un futur durcissement de la policy reste automatiquement suivi).
    """
    if decision is Decision.REJECT:
        sensitive = _first_sensitive_arg(args)
        if sensitive is not None:
            return f"cible sensible interdite : {sensitive!r}"
        url = _first_url(args)
        if url and is_private_host(url):
            return f"hôte privé interdit (anti-SSRF) : {url!r}"
        return "règle dure de la policy : action interdite (mutation SQL ou cible sensible)"
    if decision is Decision.APPROVE:
        return "catégorie à risque (write/delete/exec/unknown) : validation humaine requise"
    return "catégorie lisible (read/system/network) : exécution immédiate"


def decide(
    tool: str, args: Mapping[str, Any], category: ActionCategory | None = None
) -> PolicyVerdict:
    """Verdict de policy pour un appel MCP ``tools/call`` (pur, déterministe).

    Délègue intégralement à ``sandbox_policy.decide`` — règles dures incluses
    (chemins sensibles, SQL mutant, anti-SSRF) — puis projette le verdict en
    annotations MCP + raison auditée.

    Args:
        tool:     nom du tool (registre ia/tools) ;
        args:     arguments de l'appel ; ``None`` → ``{}`` ;
        category: catégorie connue, sinon classée par ``sandbox_policy``.

    Returns:
        Le ``PolicyVerdict`` correspondant (decision + annotations + reason).
    """
    normalized = dict(args or {})
    decision = sandbox_decide(tool, normalized, category)
    return PolicyVerdict(
        tool=tool,
        decision=decision,
        annotations=decision_to_annotations(decision),
        reason=_verdict_reason(tool, normalized, decision),
    )


def decide_action(action: Action) -> PolicyVerdict:
    """Variante typée pour une entité ``Action`` du domaine (miroir sandbox_policy).

    La couture demandée par la tâche 5 : ``decide_action()`` (sandbox_policy)
    → verdict + annotations MCP.
    """
    return decide(action.tool, action.args, action.category)


# ---------------------------------------------------------------------------
# Filtre de scope (« visible_tools »)
# ---------------------------------------------------------------------------


def visible_tools(
    tools: Iterable[MCPTool],
    scope: MCPScopeRole,
    *,
    whitelist: Collection[str] | None = None,
) -> list[MCPTool]:
    """Tools visibles pour un rôle client (fail-closed) — « visible_tools » MCP.

    Deux filtres soustractifs (jamais additifs) :
        1. rôle      : ``MCPScopeRole.granted(tool.required_scope)`` — la même
           règle que ``MCPServer._visible_tools`` (un rôle inconnu lève) ;
        2. whitelist : si fournie (S4 ``MCPSecurityScope.visible_tools``), le
           tool doit AUSSI y figurer — intersection, pas union.

    Args:
        tools:     catalogue complet (le port rend la vérité) ;
        scope:     rôle du client (``MCPScopeRole``) ;
        whitelist: noms explicitement autorisés ; ``None`` → rôle seul.

    Returns:
        Une NOUVELLE liste (ordre d'entrée préservé) — jamais le catalogue.
    """
    # Normalisation tolérante (fail-closed) : un rôle inconnu lève ValueError.
    role = scope if isinstance(scope, MCPScopeRole) else MCPScopeRole(scope)
    allowed = frozenset(whitelist) if whitelist is not None else None
    catalogue = list(tools)
    visible = [
        tool
        for tool in catalogue
        if role.granted(tool.required_scope) and (allowed is None or tool.name in allowed)
    ]
    if allowed is not None:
        unknown = sorted(allowed - {tool.name for tool in catalogue})
        if unknown:
            # Une whitelist nommant des tools inexistants est une erreur de
            # configuration : loggée, non bloquante (tolérance au déploiement).
            logger.warning("Whitelist MCP : tools inconnus ignorés : %s", unknown)
    return visible


class ScopeFilteredToolProvider:
    """``MCPToolRegistryPort`` projetant la vue SÉCURISÉE d'un registre interne.

    Décorateur de port (composable avec ``PolicyGateToolProvider``) :
        - ``list_tools`` : ne rend que ``visible_tools(...)`` — la vérité reste
          chez le registre interne, la vue est filtrée par rôle (+ whitelist S4) ;
        - ``call_tool``  : fail-closed — un tool non visible est INDISCERNABLE
          d'un tool absent (même sémantique que ``MCPServer`` : aucun oracle de
          visibilité pour le client).
    """

    def __init__(
        self,
        inner: MCPToolRegistryPort,
        *,
        scope: MCPScopeRole,
        whitelist: Collection[str] | None = None,
    ) -> None:
        self._inner = inner
        self._scope = scope
        self._whitelist = whitelist

    def list_tools(self) -> list[MCPTool]:
        """Vue filtrée par scope du catalogue interne."""
        return visible_tools(self._inner.list_tools(), self._scope, whitelist=self._whitelist)

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Délègue l'exécution ; refuse (ToolError) tout tool non visible."""
        if not any(tool.name == name for tool in self.list_tools()):
            raise ToolError(f"Unknown tool: {name}")
        return self._inner.call_tool(name, arguments)


class PolicyGateToolProvider:
    """``MCPToolRegistryPort`` faisant respecter la policy à chaque ``tools/call``.

    MCP n'est pas un bypass de la security interne (docs/mcp/MCP_SECURITY.md) :
    tout appel passe par ``sandbox_policy.decide`` (via :func:`decide`).

        AUTO_APPROVE → délégation au registre interne (exécution immédiate) ;
        APPROVE      → ``ToolError`` « validation humaine requise » — v0.1.0
                       n'expose que des tools read-only ; le flux d'approbation
                       MCP sera branché avec les tools write/exec (tâche 17) ;
        REJECT       → ``ToolError`` « rejeté » (règle dure, jamais exécutée).

    Un tool inconnu du registre interne reste « Unknown tool » (indiscernable
    d'un tool absent). Chaque verdict est loggé (traçabilité, audit tâche 12).
    """

    def __init__(self, inner: MCPToolRegistryPort) -> None:
        self._inner = inner

    def list_tools(self) -> list[MCPTool]:
        """Catalogue inchangé : la policy borne l'EXÉCUTION, pas la visibilité."""
        return self._inner.list_tools()

    def call_tool(self, name: str, arguments: dict[str, Any]) -> str:
        """Gate : verdict policy → exécuter / exiger une approbation / refuser."""
        if not any(tool.name == name for tool in self._inner.list_tools()):
            raise ToolError(f"Unknown tool: {name}")
        verdict = decide(name, dict(arguments or {}))
        if verdict.blocked:
            logger.warning("MCP tools/call rejeté : %s (%s)", name, verdict.reason)
            raise ToolError(f"Policy rejected: {name} — {verdict.reason}")
        if verdict.requires_approval:
            logger.info("MCP tools/call en attente d'approbation humaine : %s", name)
            raise ToolError(f"Manual approval required: {name} — {verdict.reason}")
        logger.info("MCP tools/call auto-approuvé : %s (%s)", name, verdict.reason)
        return self._inner.call_tool(name, arguments)
