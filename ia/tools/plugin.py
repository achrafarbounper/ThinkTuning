"""Architecture de plugins pour l'extensibilité des outils. Phase B.

Un plugin est un module Python (chemin importable, ex. ``mypkg.my_tools``)
exposant optionnellement :

    PLUGIN_NAME    : str — nom lisible (défaut : nom du module)
    TOOL_META      : dict — métadonnées déclaratives des outils du plugin

et dont chaque fonction publique devient un outil enregistré dans le registre
``TOOLS``. Les entrées du manifeste sont validées : un outil sans entrée
``TOOL_META`` est rejeté (fail-closed, cohérent avec la config JSON).

Usage : ``load_plugin("mypkg.my_tools")`` — idempotent (un module déjà chargé
n'est pas rechargé). ``loaded_plugins()`` liste ce qui a été intégré.
"""

import importlib
import threading
import types

from .tool_registry import REQUIRED_ARGS, TOOLS, TOOL_META

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
            if name in TOOLS:
                raise PluginError(f"Conflit : l'outil « {name} » existe déjà")
            if name not in meta:
                raise PluginError(
                    f"Outil « {name} » du plugin {plugin_name} sans entrée TOOL_META "
                    "(description/required_args) : refusé (fail-closed)"
                )
            TOOLS[name] = obj
            TOOL_META[name] = meta[name]
            REQUIRED_ARGS[name] = meta[name].get("required_args", [])
            registered.append(name)
        _LOADED[module_path] = plugin_name
    return {"plugin": plugin_name, "registered": registered, "cached": False}


def loaded_plugins() -> dict[str, str]:
    """Plugins chargés : chemin module -> nom de plugin."""
    with _LOCK:
        return dict(_LOADED)
