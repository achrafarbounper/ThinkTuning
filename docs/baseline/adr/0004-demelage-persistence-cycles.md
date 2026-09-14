# ADR-0004 — Démêlage du hub de persistance et suppression des cycles

**Statut :** Accepté (14/09/2026) · **Décideurs :** équipe SCRUM-141

## Contexte

7 cycles d'imports détectés dans `app/` (5 autour de
`persistence/mongodb.py`, 1 avec `security/service_accounts.py`, 1 inversion
de couche traitée par ADR-0003). Tous passent par des imports paresseux :
aucun crash, mais impossible de remplacer ou tester un store isolément, et le
hub `mongodb.py` (1 062 lignes) importe des symboles **privés** des stores
(`_audit`, `_hash_secret`).

## Décision

1. Extraire les constantes/erreurs partagées (`STATUSES`, `MCP_ACTIONS`,
   erreurs métier, helpers de redaction/hashing) dans un module neutre
   `persistence/common.py` (ou `persistence/symbols.py`) sans dépendance
   vers les stores ni vers `mongodb.py`.
2. `mongodb.py` ne doit plus importer les stores : les opérations MongoDB
   sont déléguées via les mêmes **ports** que les stores SQLite (ADR-0003) ;
   la sélection du backend se fait au niveau du container/composition root.
3. Interdiction d'importer des symboles préfixés `_` entre modules :
   à faire respecter par une règle ruff custom ou un test de garde.
4. Objectif mesurable : détecteur AST = **0 cycle** (baseline : 7).

## Alternatives rejetées

- Garder les imports paresseux « tels quels » : la dette reste invisible et
  bloque l'isolation des tests (liée à B-2).

## Conséquences

- Stores remplaçables/testables un par un ; prépare la bascule MongoDB/SQLite
  à la composition root plutôt qu'au niveau des modules.
