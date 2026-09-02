"""Adaptateur : registre d'outils legacy (ia/tools) -> ToolRegistryPort.

Premier adapter d'infrastructure de la migration : il expose le registre
historique ``ia/tools/tool_registry.py`` (TOOLS + TOOL_META chargés depuis
tools_config.json) derrière le port du domaine. AUCUNE logique nouvelle :
l'adaptateur délègue et normalise les types.

Les imports legacy sont best-effort tolérants à la double identité
d'import documentée (``ia.tools`` vs ``tools``) — mais l'échec des DEUX
lève immédiatement (fail-fast : un registre indisponible n'est jamais
silencieux).
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any

from app.domain.ports import ToolRegistryPort

logger = logging.getLogger("thinktuning.agent.registry")

try:  # identité « ia.tools » (racine projet, tests)
    from ia.tools import tool_registry as _legacy
except ImportError:  # identité « tools » (core/agent_cache.py ajoute ia/ au path)
    try:
        from tools import tool_registry as _legacy  # type: ignore[no-redef]
    except ImportError as exc:
        raise ImportError(
            "Registre d'outils legacy introuvable (ni ia.tools.tool_registry, "
            "ni tools.tool_registry) : impossible d'initialiser l'adaptateur."
        ) from exc


class LegacyToolRegistryAdapter:
    """Implémentation de ``ToolRegistryPort`` au-dessus du registre legacy."""

    def __init__(self, exclude: set[str] | None = None) -> None:
        """``exclude`` permet de retirer des outils à haut risque d'un
        déploiement donné (deny-list opérationnelle, ex. {"run_command"})."""
        self._exclude = exclude or set()

    # --- ToolRegistryPort ----------------------------------------------------

    def tool_names(self) -> list[str]:
        return sorted(name for name in _legacy.TOOLS if name not in self._exclude)

    def get(self, tool: str) -> Callable[..., Any] | None:
        if tool in self._exclude:
            return None
        return _legacy.TOOLS.get(tool)

    def meta(self, tool: str) -> dict[str, Any] | None:
        if tool in self._exclude:
            return None
        meta = _legacy.get_tool_meta(tool)
        return dict(meta) if meta else None


def build_default_registry() -> ToolRegistryPort:
    """Registre par défaut de l'application (outils métier ML inclus)."""
    return LegacyToolRegistryAdapter()
