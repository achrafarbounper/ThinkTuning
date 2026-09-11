"""Registre de capacités d'outils — la déclaration qui rend le deny-by-default
possible (P2 Lot A).

Principe : on ne peut refuser « l'action non déclarée » que si chaque outil
porté par ``ia/tools/tools_config.json`` est rattaché à UNE catégorie de risque
canonique. La source de vérité est ``sandbox_policy.classify_tool`` (déjà
audité, déjà testé, cache LRU) — ce module NE DUPLIQUE PAS le classement, il le
projette en requêtes d'autorisation :

    tool + category  ->  action normalisée  ``tool.execute:<category>``
    outil inconnu    ->  ``tool.execute:unknown``  (jamais autorisé)

Le champ optionnel ``safety`` de ``tools_config.json`` est vérifié en
cohérence : un outil déclaré ``safety.level == "safe"`` mais classé
write/delete/exec par la policy est une DÉRIVE signalée dans les logs
(la classification policy reste prioritaire).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from app.domain.entities.plan import ActionCategory

logger = logging.getLogger("thinktuning.security.authz")

# tools_config.json est à backend/ia/tools/ ; ce module à backend/app/infra/
# .../authz/ — soit 4 niveaux parents jusqu'à backend/.
_TOOLS_CONFIG_PATH = (
    Path(__file__).resolve().parents[4] / "ia" / "tools" / "tools_config.json"
)

_ACTION_UNKNOWN = "tool.execute:unknown"


def action_for_category(category: ActionCategory | str) -> str:
    """Action PDP normalisée pour une catégorie de risque d'outil."""
    cat = category.value if isinstance(category, ActionCategory) else str(category)
    return f"tool.execute:{cat}"


def _load_config(path: Path) -> dict[str, Any]:
    """Charge tools_config.json (dict vide si fichier absent — dégradation
    dégradée MAIS safe : les outils inconnus tombent dans ``unknown``)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        tools = raw.get("tools", {})
        return tools if isinstance(tools, dict) else {}
    except (OSError, ValueError) as exc:
        logger.warning("Registre de capacités : tools_config.json illisible (%s)", exc)
        return {}


class ToolCapabilityRegistry:
    """Déclarations d'outils : nom -> (catégorie policy, action PDP)."""

    def __init__(self, config_path: Path | None = None) -> None:
        self._tools = _load_config(config_path or _TOOLS_CONFIG_PATH)
        self._known: frozenset[str] = frozenset(self._tools)

    # ------------------------------------------------------------- lectures --

    @property
    def known_tools(self) -> frozenset[str]:
        """Ensemble des outils déclarés dans tools_config.json."""
        return self._known

    def is_declared(self, tool: str) -> bool:
        """L'outil est-il déclaré (sinon -> ``tool.execute:unknown``) ?"""
        return tool in self._known

    def category_for(self, tool: str) -> ActionCategory:
        """Catégorie de risque canonique (UNKNOWN si non déclarée).

        Délègue à ``sandbox_policy.classify_tool`` : source de vérité unique,
        déjà alignée sur policy_adapter MCP et sur les annotations MCP.
        """
        from app.agent.policies.sandbox_policy import classify_tool  # import paresseux (anti-cycle)

        return classify_tool(tool)

    def action_for(self, tool: str) -> str:
        """Action PDP pour un outil (``tool.execute:unknown`` si non déclaré)."""
        if not self.is_declared(tool):
            return _ACTION_UNKNOWN
        return action_for_category(self.category_for(tool))

    def safety_drift(self) -> list[str]:
        """Incohérences entre ``safety.level: safe`` et la catégorie policy.

        Contrôle de santé du registre : ``safe`` (aucune I/O) doit rester en
        catégorie READ/SYSTEM. Une dérive n'est PAS bloquante (la policy
        gagne) mais elle est signalée — un outil mal déclaré ne doit pas
        bénéficier d'un auto-approve de facto côté policy.
        """
        drift: list[str] = []
        for name, meta in self._tools.items():
            safety = (meta or {}).get("safety") or {}
            level = str(safety.get("level", "")).lower()
            if level != "safe":
                continue
            category = self.category_for(name)
            if category in (ActionCategory.WRITE, ActionCategory.DELETE, ActionCategory.EXEC):
                drift.append(f"{name}: safety=safe mais policy={category.value}")
        return drift
