"""Architecture de plugins pour l'extensibilité des outils. Phase B.

Un plugin est un module Python (chemin importable, ex. ``mypkg.my_tools``)
exposant optionnellement :

    PLUGIN_NAME    : str — nom lisible (défaut : nom du module)
    TOOL_META      : dict — métadonnées déclaratives des outils du plugin

et dont chaque fonction publique devient un outil enregistré dans le registre
``TOOLS``. Les entrées du manifeste sont validées : un outil sans entrée
``TOOL_META`` est rejeté (fail-closed, cohérent avec la config JSON).

SCRUM-99 : l'enregistrement passe désormais par la ``ToolRegistry``
(source de vérité unique — cf. ``ia/tools/registry.py``) au lieu de muter
directement les dicts statiques. Les métadonnées du plugin sont converties
au standard ``thinktuning.tool/v1`` (``from_meta_format``) puis validées.

Usage : ``load_plugin("mypkg.my_tools")`` — idempotent (un module déjà chargé
n'est pas rechargé). ``loaded_plugins()`` liste ce qui a été intégré.
"""

import importlib
import threading
import types

from .registry import ToolRegistryError, get_global_registry
from .tool_schema import from_meta_format

_LOCK = threading.Lock()
_LOADED: dict[str, str] = {}  # module_path -> plugin_name


class PluginError(ValueError):
    """Plugin invalide (outil sans métadonnées, conflit de nom, etc.)."""


def load_plugin(module_path: str) -> dict:
    """Importe un module plugin et enregistre ses outils. Idempotent."""
    with _LOCK:
        if module_path in _LOADED:
            return {"plugin": _LOADED[module_path], "registered": [], "cached": True}
    module = importlib.import_module(module_path)
    plugin_name = getattr(module, "PLUGIN_NAME", module_path.rsplit(".", 1)[-1])
    meta = getattr(module, "TOOL_META", {})
    registry = get_global_registry()
    registered: list[str] = []
    with _LOCK:
        for name in dir(module):
            if name.startswith("_") or name in ("PLUGIN_NAME", "TOOL_META"):
                continue
            obj = getattr(module, name)
            # Tout callable exposé par le module devient un outil candidat.
            # (On ne filtre pas sur __module__ : cela casserait les modules
            # synthétiques et les fonctions réexportées volontairement.)
            if not callable(obj) or isinstance(obj, types.ModuleType):
                continue
            if name not in meta:
                raise PluginError(
                    f"Outil « {name} » du plugin {plugin_name} sans entrée TOOL_META "
                    "(description/required_args) : refusé (fail-closed)"
                )
            if registry.has_tool(name):
                raise PluginError(f"Conflit : l'outil « {name} » existe déjà")
            # SOURCE DE VÉRITÉ UNIQUE : l'enregistrement (et la projection
            # dans TOOLS/TOOL_META/REQUIRED_ARGS) passe par la registry.
            try:
                registry.add_tool(
                    obj,
                    from_meta_format(name, meta[name]),
                    owner=f"plugin:{plugin_name}",
                )
            except ToolRegistryError as exc:
                raise PluginError(str(exc)) from exc
            registered.append(name)
        _LOADED[module_path] = plugin_name
    return {"plugin": plugin_name, "registered": registered, "cached": False}


def loaded_plugins() -> dict[str, str]:
    """Plugins chargés : chemin module -> nom de plugin."""
    with _LOCK:
        return dict(_LOADED)
