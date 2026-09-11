"""Règles de sécurité du DOMAINE — pures, sans I/O ni framework (P1 SEC).

Source unique des cibles de fichiers SENSIBLES et de la classification de
risque d'un chemin, partagée par les deux couches qui implémentent la
contention :

    - la policy décisionnelle ``app/agent/policies/sandbox_policy.py`` (qui
      ré-importe et ré-exporte ces symboles — zéro changement de contrat) ;
    - la sandbox physique ``ia/tools/sandbox.py`` (``ensure_writable_target``).

Défense en profondeur : la policy DÉCIDE (REJECT au niveau du plan) et la
sandbox EXÉCUTE (refus physique indépendant). Chaque couche bloque donc la
même cible, même si un appel d'outil contourne le gate de policy.
"""

from __future__ import annotations

import os

# Cibles sensibles : tout chemin dont une composante (ou extension) figure
# ici est considérée critique. Ces noms sont volontairement interdits en
# écriture/suppression/exécution par l'agent, quel que soit le réglage.
DENIED_PATH_PARTS = frozenset(
    {
        ".git",
        ".env",
        "__pycache__",
        "venv",
        ".venv",
        "node_modules",
        "id_rsa",
        "id_ed25519",
    }
)

DENIED_EXTENSIONS = frozenset({".env", ".pem", ".key", ".p12", ".pfx"})


def classify_path_risk(path: str) -> bool:
    """Vrai si le chemin pointe une cible sensible (partie ou extension).

    Pure (aucune I/O) : normalise les séparateurs (Windows : ``\\\\``) puis
    compare chaque composante à ``DENIED_PATH_PARTS`` et l'extension à
    ``DENIED_EXTENSIONS``.
    """
    parts = [p.lower() for p in os.path.normpath(str(path)).replace("\\", "/").split("/")]
    if any(part in DENIED_PATH_PARTS for part in parts):
        return True
    # id_rsa / id_ed25519 : préfixes de clés privées (id_rsa.pub reste interdit
    # en écriture par prudence ; la lecture publique est un faux positif
    # acceptable pour un backend ML).
    stem = parts[-1] if parts else ""
    ext = os.path.splitext(stem)[1]
    return stem.startswith(("id_rsa", "id_ed25519")) or ext in DENIED_EXTENSIONS


__all__ = ["DENIED_PATH_PARTS", "DENIED_EXTENSIONS", "classify_path_risk"]
