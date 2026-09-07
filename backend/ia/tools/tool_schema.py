"""Standard JSON de définition d'un tool personnalisé (SCRUM-99).

Format versionné ``thinktuning.tool/v1`` — source unique pour DÉCLARER un tool
(nom, description, schéma de paramètres) avant de l'ENREGISTRER dans la
``ToolRegistry`` (``ia/tools/registry.py``) et de l'exposer aux agents.

SÉPARATION DESIGN-TIME / RUNTIME (règle du standard) :

    DESIGN-TIME (dans la définition déclarative, ex. tools_config.json) :
        ``$schema``, ``name``, ``description``, ``version``, ``category``,
        ``required_args``, ``parameters``, ``allowed_binaries`` (option),
        ``safety`` (option : ``{"level": "safe|restricted|dangerous",
        "requires_approval": bool}``).

    RUNTIME (porté par ``RegisteredTool``, JAMAIS sérialisé dans le JSON) :
        ``enabled``, ``experimental``, ``deprecated``, ``owner``,
        ``source_file``, ``last_updated``, ``dynamic``.

Règles de validation (déterministes, aucune dépendance LLM) :
    - ``name``        : regex ^[a-z][a-z0-9_]{1,63}$ (snake_case) ;
    - ``description`` : non vide ;
    - ``parameters``  : types connus (string/number/integer/boolean/object/array) ;
    - ``required_args`` : sous-ensemble des clés de ``parameters`` ;
    - ``safety``      : ``level`` borné ; ``restricted``/``dangerous`` exigent
      ``requires_approval=true``. ``to_meta_format`` en dérive l'override
      ``approval`` (``auto``/``manual``/``blocked``) consommé par
      ``ia/agent/approvals.py`` — branchement propre sur les gates existants.

Conversions :
    - ``to_meta_format``   : vers le format historique de tools_config.json
      (name/description/required_args/parameters) — compat system_prompt / API ;
    - ``from_meta_format`` : depuis ce format (hydratation de la registry) ;
    - ``to_json_schema``   : vers un JSON Schema standard style function-calling.

``check_args_against_definition`` valide DÉTERMINISTEMENT les arguments d'un
appel contre le schéma : le système REFUSE un appel mal formé avant exécution.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

TOOL_SCHEMA_VERSION = "thinktuning.tool/v1"

NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_PARAM_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ALLOWED_PARAM_TYPES = ("string", "number", "integer", "boolean", "object", "array")
ALLOWED_CATEGORIES = (
    "os", "api", "db", "ml", "file", "shell", "network", "custom", "builtin",
)
# Niveaux de sûreté déclaratifs (design-time) :
#   safe       : lecture pure — peut être auto-approuvé ;
#   restricted : peut muter (écriture, POST…) — validation humaine exigée ;
#   dangerous  : bloqué par défaut (rejet immédiat, même avec l'humain).
SAFETY_LEVELS = ("safe", "restricted", "dangerous")
# Défaut fail-closed pour un tool DYNAMIQUE sans déclaration explicite.
DEFAULT_SAFETY = {"level": "restricted", "requires_approval": True}
DEFAULT_CATEGORY = "custom"
DEFAULT_VERSION = "1.0"

# Correspondance dérivée ``safety -> approval`` (override consommé par
# approvals._config_approval). Une déclaration ``approval`` explicite prime.
_SAFETY_TO_APPROVAL = {
    "safe": "auto",
    "restricted": "manual",
    "dangerous": "blocked",
}

# Checks de types pour la validation DÉTERMINISTE des appels (bool est un int
# en Python : on l'exclut explicitement des types numériques).
_TYPE_CHECKS = {
    "string": lambda v: isinstance(v, str),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "boolean": lambda v: isinstance(v, bool),
    "object": lambda v: isinstance(v, dict),
    "array": lambda v: isinstance(v, (list, tuple)),
}



def is_valid_tool_name(name: Any) -> bool:
    """Vrai si ``name`` est un nom de tool valide (snake_case borné)."""
    return isinstance(name, str) and bool(NAME_PATTERN.match(name))


def approval_from_safety(safety: Optional[Dict[str, Any]]) -> Optional[str]:
    """Dériver l'override ``approval`` depuis la déclaration ``safety``.

    ``None`` si la déclaration est absente/illisible : la policy par défaut du
    moteur (classification fine, puis APPROVE prudent) s'applique alors.
    """
    if not isinstance(safety, dict):
        return None
    level = safety.get("level")
    if level not in SAFETY_LEVELS:
        return None
    if level == "dangerous":
        return "blocked"
    return "manual" if safety.get("requires_approval", True) else "auto"


def validate_tool_definition(definition: Any) -> Tuple[bool, List[str]]:
    """Valide une définition de tool au standard v1. Retourne ``(ok, erreurs)``.

    Aucune exception : la liste d'erreurs est consommable par le reviewer,
    l'API et le validateur de plan (messages affichables tels quels).
    """
    errors: List[str] = []
    if not isinstance(definition, dict):
        return False, ["la définition doit être un objet JSON ({...})"]

    schema = definition.get("$schema", TOOL_SCHEMA_VERSION)
    if schema != TOOL_SCHEMA_VERSION:
        errors.append(
            f"« $schema » inconnu : « {schema} » (attendu « {TOOL_SCHEMA_VERSION} »)"
        )

    name = definition.get("name")
    if not is_valid_tool_name(name):
        errors.append(
            "« name » manquant ou invalide : snake_case de 2 à 64 caractères "
            "(regex ^[a-z][a-z0-9_]{1,63}$)"
        )

    description = definition.get("description")
    if not isinstance(description, str) or not description.strip():
        errors.append("« description » manquante ou vide")

    version = definition.get("version", DEFAULT_VERSION)
    if not isinstance(version, str) or not version.strip():
        errors.append("« version » doit être une chaîne non vide (ex: \"1.0\")")

    category = definition.get("category", DEFAULT_CATEGORY)
    if category not in ALLOWED_CATEGORIES:
        errors.append(
            f"« category » invalide : « {category} » "
            f"(valides : {', '.join(ALLOWED_CATEGORIES)})"
        )

    approval = definition.get("approval")
    if approval is not None and approval not in ("auto", "manual", "blocked"):
        errors.append(
            f"« approval » invalide : « {approval} » (valides : auto, manual, blocked)"
        )

    safety = definition.get("safety")
    if safety is not None:
        if not isinstance(safety, dict):
            errors.append("« safety » doit être un objet {level, requires_approval}")
        else:
            level = safety.get("level")
            if level not in SAFETY_LEVELS:
                errors.append(
                    f"« safety.level » invalide : « {level} » "
                    f"(valides : {', '.join(SAFETY_LEVELS)})"
                )
            requires = safety.get("requires_approval", True)
            if not isinstance(requires, bool):
                errors.append("« safety.requires_approval » doit être booléen")
            elif level in ("restricted", "dangerous") and not requires:
                errors.append(
                    "« safety » incohérent : un niveau restricted/dangerous exige "
                    "requires_approval=true"
                )


    allowed_binaries = definition.get("allowed_binaries")
    if allowed_binaries is not None:
        if (
            not isinstance(allowed_binaries, list)
            or not allowed_binaries
            or any(not isinstance(b, str) or not b.strip() for b in allowed_binaries)
        ):
            errors.append(
                "« allowed_binaries » doit être une liste non vide de noms de binaires"
            )

    required_args = definition.get("required_args", [])
    if not isinstance(required_args, list) or any(
        not isinstance(a, str) or not a.strip() for a in required_args
    ):
        errors.append("« required_args » doit être une liste de noms (chaînes)")
        required_args = []

    parameters = definition.get("parameters", {})
    if not isinstance(parameters, dict):
        errors.append("« parameters » doit être un objet {nom: {type, required…}}")
        parameters = {}
    else:
        for pname, spec in parameters.items():
            if not _PARAM_NAME_PATTERN.match(str(pname)):
                errors.append(f"nom de paramètre invalide : « {pname} »")
                continue
            if not isinstance(spec, dict):
                errors.append(f"paramètre « {pname} » : définition attendue en objet")
                continue
            ptype = spec.get("type")
            if ptype is not None and ptype not in ALLOWED_PARAM_TYPES:
                errors.append(
                    f"paramètre « {pname} » : type inconnu « {ptype} » "
                    f"(valides : {', '.join(ALLOWED_PARAM_TYPES)})"
                )
            if not isinstance(spec.get("required", False), bool):
                errors.append(f"paramètre « {pname} » : « required » doit être booléen")
            if "description" in spec and not isinstance(spec.get("description"), str):
                errors.append(f"paramètre « {pname} » : « description » doit être une chaîne")
            if "enum" in spec:
                enum = spec.get("enum")
                if not isinstance(enum, list) or not enum:
                    errors.append(
                        f"paramètre « {pname} » : « enum » doit être une liste non vide"
                    )

    if isinstance(required_args, list) and isinstance(parameters, dict):
        unknown = [a for a in required_args if a not in parameters]
        if unknown:
            errors.append(
                "« required_args » contient des paramètres non déclarés : "
                + ", ".join(map(str, unknown))
            )

    return (not errors), errors



def to_meta_format(definition: Dict[str, Any]) -> Dict[str, Any]:
    """Convertit une définition standard v1 vers le format historique TOOL_META.

    Sans perte : les clés de paramètres non standardisées (``default``…),
    ``enum``/``description`` et les clés de premier niveau inconnues sont
    préservées. L'override ``approval`` est dérivé de ``safety`` (une
    déclaration explicite ``approval`` prime) pour brancher le gate existant.
    """
    meta: Dict[str, Any] = {
        "name": definition.get("name", ""),
        "description": definition.get("description", ""),
        "required_args": list(definition.get("required_args", []) or []),
        "parameters": {},
    }
    for pname, spec in (definition.get("parameters") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        entry: Dict[str, Any] = {
            "type": spec.get("type", "string"),
            "required": bool(spec.get("required", False)),
        }
        for extra_key, extra_val in spec.items():
            if extra_key not in ("type", "required"):
                entry[extra_key] = extra_val
        meta["parameters"][pname] = entry

    approval = definition.get("approval") or approval_from_safety(
        definition.get("safety")
    )
    if approval:
        meta["approval"] = approval
    if definition.get("safety") is not None:
        meta["safety"] = definition["safety"]
    if definition.get("allowed_binaries"):
        meta["allowed_binaries"] = list(definition["allowed_binaries"])
    if definition.get("category"):
        meta["category"] = definition["category"]
    if definition.get("version"):
        meta["version"] = definition["version"]
    # Clés de premier niveau inconnues : préservées (round-trip sans perte).
    known = {
        "$schema", "name", "description", "version", "category", "approval",
        "safety", "allowed_binaries", "required_args", "parameters",
    }
    for key, value in definition.items():
        if key not in known:
            meta[key] = value
    return meta



def from_meta_format(name: str, meta: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Convertit une entrée TOOL_META (tools_config.json / plugin) au standard v1.

    Utilisée par l'hydratation de la ToolRegistry : le registre statique
    existant devient une collection de définitions standard sans doublon de
    vérité (tools_config.json reste la source déclarative des tools natifs).
    """
    meta = meta or {}
    parameters: Dict[str, Any] = {}
    for pname, spec in (meta.get("parameters") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        entry: Dict[str, Any] = {
            "type": spec.get("type", "string"),
            "required": bool(spec.get("required", False)),
        }
        for extra_key, extra_val in spec.items():
            if extra_key not in ("type", "required"):
                entry[extra_key] = extra_val
        parameters[pname] = entry

    definition: Dict[str, Any] = {
        "$schema": TOOL_SCHEMA_VERSION,
        "name": name,
        "description": str(meta.get("description", "") or ""),
        "version": str(meta.get("version", DEFAULT_VERSION) or DEFAULT_VERSION),
        "category": meta.get("category", "builtin"),
        "required_args": list(meta.get("required_args") or []),
        "parameters": parameters,
    }
    if meta.get("approval"):
        definition["approval"] = meta["approval"]
    if meta.get("safety") is not None:
        definition["safety"] = meta["safety"]
    if meta.get("allowed_binaries"):
        definition["allowed_binaries"] = list(meta["allowed_binaries"])
    known = {
        "name", "description", "version", "category", "approval", "safety",
        "allowed_binaries", "required_args", "parameters",
    }
    for key, value in meta.items():
        if key not in known:
            definition[key] = value
    return definition



def to_json_schema(definition: Dict[str, Any]) -> Dict[str, Any]:
    """Définition standard -> JSON Schema standard (style function-calling).

    Format consommable directement par les LLM (OpenAI / Ollama / OpenRouter)
    pour l'appel d'outils natif : ``{"type": "function", "function": {...}}``.
    Les champs design-time de sécurité (``safety``, ``allowed_binaries``) ne
    sont PAS exposés au LLM : ils gouvernent le moteur, pas le modèle.
    """
    properties: Dict[str, Any] = {}
    for pname, spec in (definition.get("parameters") or {}).items():
        spec = spec if isinstance(spec, dict) else {}
        entry: Dict[str, Any] = {
            "type": spec.get("type", "string"),
            "description": spec.get("description", ""),
        }
        if "enum" in spec:
            entry["enum"] = spec["enum"]
        properties[pname] = entry
    return {
        "type": "function",
        "function": {
            "name": definition.get("name", ""),
            "description": definition.get("description", ""),
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": list(definition.get("required_args", []) or []),
            },
        },
    }


def check_args_against_definition(
    definition: Dict[str, Any], args: Any,
) -> Tuple[bool, List[str]]:
    """Valide DÉTERMINISTEMENT des arguments d'appel contre la définition.

    Le système REFUSE un appel mal formé (argument requis manquant, type
    invalide) AVANT toute exécution. Les paramètres non déclarés sont tolérés
    (les implémentations acceptent ``**kwargs``), seuls les paramètres
    DÉCLARÉS sont vérifiés en type et en ``enum``.
    """
    errors: List[str] = []
    if not isinstance(definition, dict):
        return False, ["définition de tool invalide (objet attendu)"]
    if not isinstance(args, dict):
        return False, ["les arguments d'appel doivent être un objet JSON ({...})"]

    parameters = definition.get("parameters") or {}
    for key in definition.get("required_args") or []:
        if key not in args:
            errors.append(f"argument requis manquant : « {key} »")

    for key, value in args.items():
        spec = parameters.get(key)
        if not isinstance(spec, dict):
            continue  # paramètre libre (non déclaré) : toléré
        ptype = spec.get("type")
        check = _TYPE_CHECKS.get(ptype)
        if check is not None and not check(value):
            errors.append(
                f"type invalide pour « {key} » : attendu {ptype}, "
                f"obtenu {type(value).__name__}"
            )
        if isinstance(spec.get("enum"), list) and spec["enum"]:
            if value not in spec["enum"]:
                errors.append(
                    f"valeur invalide pour « {key} » : « {value} » "
                    f"(autorisé : {', '.join(map(str, spec['enum']))})"
                )
    return (not errors), errors





