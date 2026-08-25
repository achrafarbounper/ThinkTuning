"""System prompt de l'agent — généré depuis le registre des outils.

Source de vérité UNIQUE : ``tools.tool_registry.TOOLS`` / ``REQUIRED_ARGS``
et la première ligne des docstrings. Ajouter un outil dans le registre suffit
donc à l'exposer au LLM ici. Fini les listes codées en dur qui se
désynchronisaient et poussaient le modèle à inventer des noms ('cat', …).
"""

import inspect

from tools.tool_registry import REQUIRED_ARGS, TOOLS

# Regroupement thématique (miroir de la table « Outils disponibles » du README).
# Un outil présent dans TOOLS mais absent d'ici est exposé dans « Autres ».
_CATEGORIES: list[tuple[str, list[str]]] = [
    ("Math", ["add"]),
    (
        "Fichiers (bac à sable, chemins relatifs à la racine autorisée)",
        [
            "write_file",
            "list_dir",
            "read_file",
            "make_dir",
            "copy_path",
            "move_path",
            "remove_path",
        ],
    ),
    ("Exécution", ["run_command", "run_python"]),
    ("Réseau", ["http_get", "http_post"]),
    ("Docker", ["docker_ps", "docker_logs", "docker_exec"]),
    ("GPU", ["gpu_info"]),
    ("Bases de données", ["sqlite_query", "postgres_query"]),
]

_HEADER = """\
Tu es un agent capable d’appeler des outils Python.

FORMAT DES APPELS DE TOOLS :
Tu réponds UNIQUEMENT avec du JSON strict :
{{"tool": "nom_du_tool", "args": {{...}}}}
Un JSON = une action. Pas de texte avant, pas de texte après.
Si plusieurs actions sont nécessaires, renvoie plusieurs JSON séparés par des
retours à la ligne.
Les explications finales destinées à l’utilisateur sont en TEXTE NORMAL, jamais
en JSON.

OUTILS DISPONIBLES (liste générée depuis le registre — source de vérité) :
N’appelle JAMAIS un outil absent de cette liste (pas de 'cat', 'ls', …) :
utilise les équivalents listés ici."""

_RULES = """

CONSIGNES DE SÉCURITÉ :
- remove_path est destructif : recursive=true seulement si l’utilisateur l’a demandé.
- run_command : uniquement la liste d’arguments, jamais une chaîne.
- Les requêtes SQL sont en lecture seule par défaut (readonly=true).

RÈGLE ABSOLUE DE FIDÉLITÉ :
- Pour montrer un contenu (fichier, code, données) : appelle d’abord l’outil
  approprié (ex. read_file), puis REPRODUIS le retour OBTENU tel quel, entre
  triples backticks, dans ta réponse finale.
- N’invente JAMAIS un contenu, un chemin ou un résultat que les outils n’ont
  pas renvoyé. Si un outil échoue ou renvoie une erreur, dis-le franchement."""


def _format_default(value) -> str:
    if isinstance(value, str):
        return f'"{value}"'
    return repr(value)


def _tool_line(name: str) -> str:
    """Une ligne du catalogue : ``- nom(args)   # résumé docstring``."""
    fn = TOOLS[name]
    try:
        parameters = list(inspect.signature(fn).parameters.values())
        parts = [
            p.name
            if p.default is inspect.Parameter.empty
            else f"{p.name}={_format_default(p.default)}"
            for p in parameters
        ]
        signature = ", ".join(parts)
    except (TypeError, ValueError):  # callable exotique -> repli sur le registre
        signature = ", ".join(REQUIRED_ARGS.get(name, []))

    doc = inspect.getdoc(fn) or ""
    summary = doc.splitlines()[0].strip() if doc else ""
    line = f"- {name}({signature})"
    return f"{line}   # {summary}" if summary else line


def build_system_prompt() -> str:
    """Assemble header + catalogue des outils + règles (à chaque appel)."""
    sections: list[str] = [_HEADER.format()]
    covered: set[str] = set()

    for title, names in _CATEGORIES:
        known = [name for name in names if name in TOOLS]
        if not known:
            continue
        sections.append(f"\n{title} :")
        sections.extend(_tool_line(name) for name in known)
        covered.update(known)

    extras = sorted(set(TOOLS) - covered)
    if extras:
        sections.append("\nAutres :")
        sections.extend(_tool_line(name) for name in extras)

    sections.append(_RULES)
    return "\n".join(sections)


# Constante historique conservée pour compatibilité (imports existants).
SYSTEM_PROMPT = build_system_prompt()

