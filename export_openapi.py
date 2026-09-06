# project/export_openapi.py
"""Exporte la spécification OpenAPI de l'API (surface v1 + legacy).

Usage (depuis la racine du dépôt) :

    python export_openapi.py                       # -> openapi.json
    python export_openapi.py --out docs/api.json   # destination explicite

Le fichier exporté sert de source aux générateurs de clients typés
(openapi-typescript, openapi-generator, ...). Le contrat v1 est VERROUILLÉ
par ``tests/test_api_v1_contract.py`` : toute dérive de paths / DTO v1 casse
la CI AVANT de casser un consommateur (dashboard, client généré) — c'est ce
verrou qui rend la génération de client sûre le jour où elle est déclenchée.

La clé API posée ici n'est utilisée QUE pour l'import du module ``api`` :
l'export ne démarre aucun serveur et n'exécute aucune route.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

# Import du paquet ``api`` depuis la racine, quel que soit le cwd d'invocation.
sys.path.insert(0, str(Path(__file__).resolve().parent))

# L'application lit la clé à l'appel (pas à l'import) ; cette valeur de repli
# ne sert qu'à satisfaire d'éventuelles vérifications au boot de l'import.
os.environ.setdefault("API_KEY", "export-local-key")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Exporte la spec OpenAPI de l'API ThinkTuning.")
    parser.add_argument(
        "--out",
        default="openapi.json",
        help="Fichier de destination (défaut : openapi.json à la racine).",
    )
    # argv injectable : testable in-process (le smoke test du verrou de
    # contrat appelle main([...]) sans subprocess ni effet env parasite).
    args = parser.parse_args(argv)

    # Import différé : après la config de sys.path / env ci-dessus.
    from api import app  # noqa: E402

    spec = app.openapi()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(spec, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    paths = sorted(spec.get("paths", {}))
    v1_paths = [p for p in paths if p.startswith("/api/v1")]
    print(f"OpenAPI exporté -> {out_path}")
    print(f"  {len(paths)} paths au total, dont {len(v1_paths)} en /api/v1 :")
    for path in v1_paths:
        methods = ", ".join(m.upper() for m in sorted(spec["paths"][path]))
        print(f"    {methods:<10} {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
