"""Bootstrap : génère ia/tools/tools_config.json depuis le registre réel.

Pour chaque outil enregistré (tools.tool_registry.TOOLS) on extrait :
  - description  : 1re phrase de la docstring (même règle que system_prompt)
  - required_args : REQUIRED_ARGS du registre
  - parameters    : {nom -> {type (si annoté), required, default (si optionnel)}}

Usage : venv\\Scripts\\python.exe scripts\\dump_tool_manifest.py
"""

import inspect
import json
import logging
from pathlib import Path

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "ia"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.tool_registry import REQUIRED_ARGS, TOOLS  # noqa: E402

logger = logging.getLogger(__name__)

_DESC_MAX_CHARS = 140

_TYPE_ALIAS = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def short_description(func) -> str:
    doc = (getattr(func, "__doc__", "") or "").strip()
    if not doc:
        return ""
    paragraph = doc.split("\n\n", 1)[0].replace("\n", " ").strip()
    sentence = paragraph.split(". ", 1)[0].strip().rstrip(".")
    if len(sentence) > _DESC_MAX_CHARS:
        sentence = sentence[: _DESC_MAX_CHARS - 1].rstrip() + "…"
    return sentence


def param_spec(param) -> dict:
    spec = {"required": param.default is param.empty}
    if param.annotation is not param.empty:
        meta = getattr(param.annotation, "__origin__", None)
        if meta is not None and meta in _TYPE_ALIAS:  # type[list[str]]…
            inner = getattr(param.annotation, "__args__", ())
            spec["type"] = _TYPE_ALIAS.get(meta, "string")
            if inner and all(isinstance(x, type) for x in inner):
                spec["items"] = _TYPE_ALIAS.get(inner[0], "string")
        elif param.annotation in _TYPE_ALIAS:
            spec["type"] = _TYPE_ALIAS[param.annotation]
        else:
            spec["type"] = "string"
    if not spec["required"]:
        default = param.default
        try:
            json.dumps(default)  # JSON-safe ?
        except TypeError:
            default = str(default)
        spec["default"] = default
    return spec


def build_manifest() -> dict:
    manifest = {}
    for name in sorted(TOOLS):
        func = TOOLS[name]
        params = []
        try:
            params = list(inspect.signature(func).parameters.values())
        except (TypeError, ValueError):
            params = []
        parameters = {}
        for p in params:
            if p.kind in (p.VAR_POSITIONAL, p.VAR_KEYWORD):
                continue
            parameters[p.name] = param_spec(p)
        manifest[name] = {
            "name": name,
            "description": short_description(func),
            "required_args": REQUIRED_ARGS.get(name, []),
            "parameters": parameters,
        }
    return manifest


if __name__ == "__main__":
    import inspect

    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

    out = Path(__file__).resolve().parents[1] / "ia" / "tools" / "tools_config.json"
    data = build_manifest()
    out.write_text(
        json.dumps({"tools": data}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    logger.info(f"écrit {len(data)} outils -> {out}")