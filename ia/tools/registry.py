"""ToolRegistry : registre DYNAMIQUE des tools (SCRUM-99) — source de vérité.

CONTRAT « SOURCE DE VÉRITÉ UNIQUE » :
    Avant SCRUM-99, les dicts ``TOOLS`` / ``TOOL_META`` / ``REQUIRED_ARGS``
    (``ia/tools/tool_registry.py``) étaient la vérité, mutés en direct par
    qui voulait (plugin.py…). Désormais :

    1. la ``ToolRegistry`` hydrate sa collection depuis les dicts statiques au
       démarrage (``from_static_registry`` — vue bootstrap) ;
    2. ENSUITE, tout tool passe par la registry, point : ``add_tool`` /
       ``remove_tool`` sont les SEULES voies de mutation, et elles PROJETTENT
       l'état dans les dicts historiques (mutés par référence) pour que toute
       l'intégration existante (system_prompt, API /tools, AgentCore,
       core/agent_cache — qui importe ``ia.tools.tool_registry``) voie
       immédiatement les tools dynamiques sans aucune modification ;
    3. personne d'autre n'écrit dans les dicts (``plugin.py`` est refondu
       pour enregistrer via la registry).

SÉPARATION DESIGN-TIME / RUNTIME :
    - design-time : la définition standard ``thinktuning.tool/v1``
      (``ia/tools/tool_schema.py``) — schéma, description, ``safety``,
      ``allowed_binaries`` ;
    - runtime : état d'exploitation porté par ``RegisteredTool`` — ``enabled``,
      ``experimental``, ``deprecated``, ``owner``, ``source_file``,
      ``last_updated``, ``dynamic``.

SÉCURITÉ (fail-closed) :
    - un tool NATIF (outillé au démarrage) n'est JAMAIS écrasable ni retirable ;
    - un tool DYNAMIQUE n'est écrasable qu'avec ``overwrite=True`` ;
    - un tool DYNAMIQUE est ``manual`` (validation humaine) par défaut, même
      si sa définition déclare ``safe``/``auto`` — sauf ``allow_auto_approval``
      (réservé aux déclarations signées par un humain : API, config) ;
    - ``safety.level = "dangerous"`` ⇒ ``approval: blocked`` ⇒ REJECT du gate ;
    - plafond ``max_dynamic_tools`` contre l'inflation du catalogue.

Thread-safe (RLock) : l'orchestrateur multi-agents enregistre des tools
pendant que des workers s'exécutent en parallèle.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple

from .tool_schema import (
    DEFAULT_SAFETY,
    TOOL_SCHEMA_VERSION,
    approval_from_safety,
    from_meta_format,
    to_meta_format,
    validate_tool_definition,
)

# Vue bootstrap : dicts historiques du registre statique (mêmes objets — la
# projection ci-dessous les mute PAR RÉFÉRENCE). Import relatif : fonctionne
# sous « ia.tools » (tests) comme sous le paquet racine « tools » (runtime).
from .tool_registry import REQUIRED_ARGS, TOOL_META, TOOLS

logger = logging.getLogger("thinktuning.tools.registry")


class ToolRegistryError(ValueError):
    """Enregistrement/retrait de tool invalide (schéma, conflit, natif protégé)."""



@dataclass
class RegisteredTool:
    """Tool enregistré : fonction + définition design-time + état runtime."""

    name: str
    func: Callable[..., Any]
    definition: Dict[str, Any]                 # standard thinktuning.tool/v1
    dynamic: bool = False                      # False = tool natif (bootstrap)
    registered_at: str = ""                    # ISO UTC (dynamique uniquement)
    owner: str = ""                            # qui l'a enregistré (runtime)
    source_file: str = ""                      # fichier sandbox du code généré
    enabled: bool = True
    experimental: bool = False
    deprecated: bool = False
    meta: Dict[str, Any] = field(default_factory=dict)  # annotations libres

    # --- Lectures pratiques (design-time) ---
    @property
    def description(self) -> str:
        return str(self.definition.get("description", ""))

    @property
    def parameters(self) -> Dict[str, Any]:
        return dict(self.definition.get("parameters", {}))

    @property
    def required_args(self) -> List[str]:
        return list(self.definition.get("required_args", []))

    @property
    def category(self) -> str:
        return str(self.definition.get("category", ""))

    @property
    def approval(self) -> str:
        """Override effectif : ``approval`` déclaré, sinon dérivé de ``safety``."""
        explicit = self.definition.get("approval")
        if explicit:
            return str(explicit)
        return str(approval_from_safety(self.definition.get("safety")) or "")

    def to_json_schema(self) -> Dict[str, Any]:
        from .tool_schema import to_json_schema as _to_json_schema
        return _to_json_schema(self.definition)


class ToolRegistry:
    """Registre dynamique : la seule voie de mutation des tools du moteur.

    ``max_dynamic_tools`` : plafond de tools DYNAMIQUES simultanés (garde-fou
    anti-inflation des propositions du Planner). Les tools natifs ne comptent
    pas dans ce plafond.
    """

    def __init__(self, *, max_dynamic_tools: int = 16):
        self._lock = threading.RLock()
        self._tools: Dict[str, RegisteredTool] = {}
        self._version = 0
        self._max_dynamic_tools = max(1, int(max_dynamic_tools))
        self.from_static_registry()

    # --- Bootstrap (vue initiale depuis les dicts statiques) ---------------

    def from_static_registry(self) -> None:
        """Hydrate la registry depuis TOOLS/TOOL_META (outillage natif).

        Les tools natifs sont marqués ``dynamic=False`` : non écrasables, non
        retirables. Après cet appel, les dicts ne sont plus modifiés que par
        ``add_tool``/``remove_tool`` (projection).
        """
        with self._lock:
            for name, func in TOOLS.items():
                definition = from_meta_format(name, TOOL_META.get(name))
                self._tools[name] = RegisteredTool(
                    name=name, func=func, definition=definition, dynamic=False,
                )

    # --- Lecture -----------------------------------------------------------

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: str) -> bool:
        return name in self._tools

    def has_tool(self, name: str) -> bool:
        return name in self._tools

    def has_native(self, name: str) -> bool:
        tool = self._tools.get(name)
        return tool is not None and not tool.dynamic

    def get_tool(self, name: str) -> Optional[RegisteredTool]:
        """Tool enregistré (natif ou dynamique), ``None`` si inconnu."""
        with self._lock:
            return self._tools.get(name)

    def get_function(self, name: str) -> Optional[Callable[..., Any]]:
        with self._lock:
            tool = self._tools.get(name)
            return tool.func if tool is not None else None


    def list_tools(self, *, dynamic_only: bool = False) -> List[Dict[str, Any]]:
        """Définitions design-time (standard v1), triées par nom."""
        with self._lock:
            definitions = [
                dict(rt.definition)
                for rt in self._tools.values()
                if (rt.dynamic or not dynamic_only)
            ]
        return sorted(definitions, key=lambda d: str(d.get("name", "")))

    def list_registered(self, *, dynamic_only: bool = False) -> List[RegisteredTool]:
        """Objets ``RegisteredTool`` complets (design-time + état runtime)."""
        with self._lock:
            return [
                rt for rt in self._tools.values()
                if (rt.dynamic or not dynamic_only)
            ]

    def dynamic_tool_names(self) -> List[str]:
        with self._lock:
            return sorted(n for n, rt in self._tools.items() if rt.dynamic)

    @property
    def version(self) -> int:
        """Compteur de mutations (invalidation de caches dépendants)."""
        with self._lock:
            return self._version

    @property
    def max_dynamic_tools(self) -> int:
        return self._max_dynamic_tools

    def merged_registry(self) -> Tuple[Dict[str, Callable[..., Any]], Dict[str, List[str]]]:
        """Vue fusionnée (natifs + dynamiques) pour construire un agent.

        Retourne ``(tools, required_args)`` — les dicts statiques enrichis des
        tools dynamiques. Utilisée par l'orchestrateur pour l'opérateur.
        """
        with self._lock:
            tools = dict(TOOLS)
            required = dict(REQUIRED_ARGS)
            for name, rt in self._tools.items():
                if rt.dynamic:
                    tools[name] = rt.func
                    required[name] = rt.required_args
        return tools, required


    # --- Écriture (SEULE voie de mutation) ---------------------------------

    def add_tool(
        self,
        func: Callable[..., Any],
        definition: Optional[Dict[str, Any]] = None,
        *,
        name: Optional[str] = None,
        description: Optional[str] = None,
        parameters: Optional[Dict[str, Any]] = None,
        required_args: Optional[List[str]] = None,
        category: str = "custom",
        version: str = "1.0",
        safety: Optional[Dict[str, Any]] = None,
        allowed_binaries: Optional[List[str]] = None,
        owner: str = "",
        source_file: str = "",
        experimental: bool = False,
        overwrite: bool = False,
        allow_auto_approval: bool = False,
    ) -> RegisteredTool:
        """Enregistre un tool DYNAMIQUE et le projette dans les dicts statiques.

        ``definition`` (standard v1) est la forme canonique ; à défaut, les
        champs épars (``name``/``description``/``parameters``…) construisent
        la définition. ``allow_auto_approval=True`` : honorer la déclaration
        ``safety`` de la définition (réservé aux sources humaines — API,
        config) ; sinon tout tool dynamique est ``manual`` (fail-closed).

        Lève ``ToolRegistryError`` : définition invalide, tool natif visé,
        duplicata sans ``overwrite``, plafond dynamique atteint.
        """
        if not callable(func):
            raise ToolRegistryError("« func » doit être un callable exécutable")

        if definition is None:
            if not name or not description:
                raise ToolRegistryError(
                    "add_tool exige une « definition » (standard v1) ou au moins "
                    "« name » + « description »"
                )
            parameters = parameters or {}
            if required_args is None:
                required_args = [
                    p for p, spec in parameters.items()
                    if isinstance(spec, dict) and spec.get("required")
                ]
            built: Dict[str, Any] = {
                "$schema": TOOL_SCHEMA_VERSION,
                "name": name,
                "description": description,
                "version": version,
                "category": category,
                "required_args": list(required_args),
                "parameters": parameters,
            }
            if safety is not None:
                built["safety"] = safety
            if allowed_binaries:
                built["allowed_binaries"] = list(allowed_binaries)
            definition = built

        ok, errors = validate_tool_definition(definition)
        if not ok:
            raise ToolRegistryError(
                "Définition de tool invalide : " + " ; ".join(errors)
            )

        # Normalisation fail-closed : un tool dynamique sans déclaration de
        # sûreté est ``restricted`` (validation humaine). Sauf autorisation
        # explicite (source humaine), la déclaration ``safe``/``auto`` est
        # FORCÉE à ``manual`` : aucun code produit par un LLM ne s'auto-approuve.
        # ``safety.level = dangerous`` ⇒ ``blocked`` TOUJOURS (aucune source
        # ne peut débloquer un tool déclaré dangereux).
        definition = dict(definition)
        if definition.get("safety") is None:
            definition["safety"] = dict(DEFAULT_SAFETY)
        if (
            approval_from_safety(definition.get("safety")) == "blocked"
            or definition.get("approval") == "blocked"
        ):
            definition["approval"] = "blocked"
        elif not allow_auto_approval:
            definition["approval"] = "manual"

        tool_name = str(definition["name"])
        with self._lock:
            existing = self._tools.get(tool_name)
            if existing is not None:
                if not existing.dynamic:
                    raise ToolRegistryError(
                        f"Conflit : « {tool_name} » est un tool NATIF du registre "
                        "(non écrasable)."
                    )
                if not overwrite:
                    raise ToolRegistryError(
                        f"Conflit : le tool dynamique « {tool_name} » existe déjà "
                        "(overwrite=True pour le remplacer)."
                    )
            else:
                dynamic_count = sum(1 for rt in self._tools.values() if rt.dynamic)
                if dynamic_count >= self._max_dynamic_tools:
                    raise ToolRegistryError(
                        f"Plafond de tools dynamiques atteint "
                        f"({self._max_dynamic_tools}) : rejet de « {tool_name} »."
                    )

            registered = RegisteredTool(
                name=tool_name,
                func=func,
                definition=definition,
                dynamic=True,
                registered_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                owner=owner,
                source_file=source_file,
                experimental=bool(experimental),
            )
            self._tools[tool_name] = registered
            # PROJECTION (compat) : les dicts historiques voient le tool.
            # Toute l'intégration existante — system_prompt, API /tools,
            # AgentCore, core/agent_cache — le découvre immédiatement.
            TOOLS[tool_name] = func
            TOOL_META[tool_name] = to_meta_format(definition)
            REQUIRED_ARGS[tool_name] = list(definition.get("required_args", []))
            self._version += 1

        logger.info(
            "tool_registry: add_tool(%s) dynamic=True owner=%s approval=%s",
            tool_name, owner or "unknown", registered.approval or "default",
        )
        return registered


    def remove_tool(self, name: str) -> bool:
        """Retire un tool DYNAMIQUE (projection dans les dicts incluse).

        Retourne ``True`` si retiré, ``False`` si inconnu. Un tool natif lève
        ``ToolRegistryError`` (jamais retirable).
        """
        with self._lock:
            existing = self._tools.get(name)
            if existing is None:
                return False
            if not existing.dynamic:
                raise ToolRegistryError(
                    f"« {name} » est un tool NATIF du registre : retrait interdit."
                )
            del self._tools[name]
            TOOLS.pop(name, None)
            TOOL_META.pop(name, None)
            REQUIRED_ARGS.pop(name, None)
            self._version += 1
        logger.info("tool_registry: remove_tool(%s)", name)
        return True

    # --- État runtime (exploitation, jamais sérialisé en design-time) ------

    def set_runtime_state(
        self,
        name: str,
        *,
        enabled: Optional[bool] = None,
        experimental: Optional[bool] = None,
        deprecated: Optional[bool] = None,
    ) -> RegisteredTool:
        """Mute l'état RUNTIME d'un tool (activé, expérimental, déprécié)."""
        with self._lock:
            tool = self._tools.get(name)
            if tool is None:
                raise ToolRegistryError(f"Tool inconnu : « {name} »")
            if enabled is not None:
                tool.enabled = bool(enabled)
            if experimental is not None:
                tool.experimental = bool(experimental)
            if deprecated is not None:
                tool.deprecated = bool(deprecated)
            self._version += 1
            return tool


# --- Registry globale (singleton) ------------------------------------------

_GLOBAL_REGISTRY: Optional[ToolRegistry] = None
_GLOBAL_LOCK = threading.Lock()


def get_global_registry() -> ToolRegistry:
    """Registry globale du process (hydratée des tools natifs au 1er appel)."""
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        with _GLOBAL_LOCK:
            if _GLOBAL_REGISTRY is None:
                _GLOBAL_REGISTRY = ToolRegistry()
    return _GLOBAL_REGISTRY




