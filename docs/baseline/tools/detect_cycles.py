"""Détection de cycles d'imports dans backend/app/ — outil de la baseline ThinkTuning.

Usage (depuis backend/) :
    python ../docs/baseline/tools/detect_cycles.py            # affiche les cycles
    python ../docs/baseline/tools/detect_cycles.py --quiet    # exit code seulement

Baseline de référence (commit 8616a9e) : 7 cycles, cf. docs/baseline/BASELINE_AUDIT.md §5.1.
Objectif cible (ADR-0004) : 0 cycle.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import ast

ROOT = Path(__file__).resolve().parents[3] / "backend" / "app"


def build_graph() -> dict[str, list[str]]:
    """Parse tous les modules de app/ et retourne les arêtes internes app.* -> app.*."""
    raw: dict[str, set[str]] = {}
    for path in ROOT.rglob("*.py"):
        module = ".".join(path.with_suffix("").relative_to(ROOT.parent).parts)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:  # pragma: no cover — garde-fou
            print(f"PARSE-ERROR {module}: {exc}", file=sys.stderr)
            continue
        imports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level == 0 and node.module:
                    imports.add(node.module)
                else:  # import relatif : remonter de `level` niveaux
                    base = module.split(".")[:-node.level]
                    if node.module:
                        imports.add(".".join(base + [node.module]))
                    else:
                        imports.add(".".join(base))
        raw[module] = imports

    # Filtrage APRÈS la boucle : tous les modules doivent être connus (bug d'ordre corrigé).
    return {
        module: sorted(
            {i for i in imports if any(i == n or i.startswith(n + ".") for n in raw) and i != module}
        )
        for module, imports in raw.items()
    }


def find_cycles(edges: dict[str, list[str]]) -> list[list[str]]:
    """DFS classique avec coloration — retourne les cycles trouvés."""
    WHITE, GRAY, BLACK = 0, 1, 2
    color = dict.fromkeys(edges, WHITE)
    cycles: list[list[str]] = []
    stack: list[str] = []

    def dfs(u: str) -> None:
        color[u] = GRAY
        stack.append(u)
        for v in edges[u]:
            if v in color:
                if color[v] == GRAY:
                    cycles.append(stack[stack.index(v):] + [v])
                elif color[v] == WHITE:
                    dfs(v)
        stack.pop()
        color[u] = BLACK

    for module in sorted(edges):
        if color[module] == WHITE:
            dfs(module)
    return cycles


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--quiet", action="store_true", help="n'affiche que le total")
    args = parser.parse_args()

    cycles = find_cycles(build_graph())
    print(f"=== CYCLES DETECTES: {len(cycles)} (baseline: 7, cible: 0) ===")
    if not args.quiet:
        for cycle in cycles:
            print(" -> ".join(cycle))
    return 1 if len(cycles) > 7 else 0  # gate : ne jamais dépasser la baseline


if __name__ == "__main__":
    sys.exit(main())
