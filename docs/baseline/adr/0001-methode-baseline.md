# ADR-0001 — Méthode de la baseline et périmètre de mesure

**Statut :** Accepté (14/09/2026) · **Décideurs :** équipe SCRUM-141

## Contexte

La refactorisation (purification hexagonale, absorption strangler) doit être
prouvée par comparaison à un état de référence. Aucune baseline chiffrée
n'existait : les seuls artefacts historiques sont les rapports de migration
`_migration/` (13/09) et le snapshot pré-migration `_migration/baseline/`.

## Décision

1. La baseline est **figée sur le commit `8616a9e`** (branche `SCRUM-141`,
   arbre propre). Toute mesure postérieure cite ce commit.
2. Les mesures de référence sont : pytest (pass/fail/durée), couverture
   `--cov=app` (pytest-cov installé ponctuellement), ruff (`app/` et `tests/`),
   ruff format, mypy, tsc, vitest, vite build, temps de démarrage à froid.
3. La détection de doublons/cycles se fait par script AST sur les imports
   `app.*` (liste blanche des modules internes), complétée par greps par couche.
4. `_migration/baseline/` est un **témoin historique** : il ne doit ni être
   mesuré comme du code actif, ni être supprimé avant la clôture de la
   migration (il prouve l'absorption `ia/`, `src/`, `app/legacy/core/`).

## Conséquences

- Toute évolution compare ses mesures à `docs/baseline/BASELINE_AUDIT.md` §6.
- Les commandes de reproduction sont normées dans `BASELINE_AUDIT.md` §8.
- La couverture 76 % devient le plancher de départ (ratchet B-7).
