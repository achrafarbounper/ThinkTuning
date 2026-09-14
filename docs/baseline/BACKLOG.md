# Backlog priorisé — suite de la baseline (14/09/2026)

> Chaque item référence son écart (`GAP_MATRIX.md`) et son risque
> (`BASELINE_AUDIT.md` §7). Ordre recommandé : **ne pas démarrer de refactorisation
> structurelle avant B-1 et B-2** (contrat + déterminisme), sinon la baseline
> devient inutilisable pour prouver les régressions.

## P0 — Débloquer la baseline (avant toute refactorisation)

| ID | Action | Écart | Effort estimé | Critère de clôture |
|---|---|---|---|---|
| B-1 | Réconcilier le contrat : régénérer `openapi.json` (`python export_openapi.py`), régénérer `schema.d.ts` (`npm run generate:api-types`), committer ensemble ; épingler `fastapi`/`pydantic`/`starlette` dans `requirements.txt` pour stabiliser la sérialisation | E-06, E-07, E-15 | S (½ j) | `test_openapi_export.py` 3/3 verts ; `pytest` + `vitest` verts ; CI backend verte |
| B-2 | Rétablir l'isolation des tests : identifier la fuite d'ordre (fixtures qui mutent des singletons : `agent_cache`, registres) et la neutraliser ; ajouter un test anti-pollution (run aléatorisé) | E-16 | M (1–2 j) | `pytest tests -p no:randomly` (ou double run ordre inversé) vert 2× de suite |

## P1 — Socle structurel (rendre la refactorisation sûre)

| ID | Action | Écart | Effort estimé | Critère de clôture |
|---|---|---|---|---|
| B-3 | Purifier la couche `application/` : introduire/compléter les ports du domaine pour les dépendances `application → infrastructure` (stores, registres, ML) ; inverser l'inversion E-03 (`model_versioning` → port, pas vers `application/`) ; brancher les singletons sur le container | E-02, E-03, E-05 | L (sprint) | 0 import `app.infrastructure.*` hors `infrastructure/` et `api/` ; test de garde (`test_no_direct_legacy_imports` étendu) |
| B-4 | Démêler le hub `persistence/mongodb.py` : extraire les constantes/erreurs partagées dans un module neutre (`persistence/common.py`), éliminer les imports de symboles privés, supprimer les 7 cycles | E-09, E-10 | M (2–3 j) | Détecteur AST de cycles = 0 cycle |
| B-5 | Absorber `routes/agent.py` (1 910 l.) en use cases : extraire l'état/queues en `application/` (Phase C documentée), v1 n'appelle plus le module legacy par attribut | E-04, E-11 | L (sprint) | `routes/agent.py` supprimé ou < 300 l. de pure délégation ; tests de contrat inchangés |
| B-6 | Purger le code mort et les traces : `git rm` des 3 routes legacy jamais importées (vérifier d'abord 0 référence), nettoyage des ~25 fichiers de traces à la racine, traiter `supervisord.conf`/`entrypoint.py` avec leurs références | E-12, E-13, E-14 | S (½ j) | 0 module 0 % sans justification ; racine propre |
| B-7 | Outiller la couverture : `pytest-cov` dans les dev-deps, step CI `--cov=app --cov-fail-under=76` (plancher = baseline actuelle), puis ratchet +1 pt / sprint ; prioriser les tests des `infrastructure/tools/` (5–44 %) | E-17 | M | Gate couverture actif en CI, plancher jamais régressé |
| B-8 | Durcir mypy : corriger l'erreur résiduelle (`agent_core.py:581`), retirer `\|\| true` de la CI, décider du sort de l'exclusion `app/api` | E-18 | S (½ j) | `mypy app/` bloquant et vert en CI |

## P2 — Confort et prévention

| ID | Action | Écart | Effort estimé | Critère de clôture |
|---|---|---|---|---|
| B-9 | Linter les tests : `ruff check tests/ --fix` (47 auto-fixables) puis manuel, étendre le scope CI à `app tests` | E-19 | S (½ j) | `ruff check app tests` 0 erreur |
| B-10 | Re-synchroniser le venv local (`pip install -r requirements.txt --force-reinstall` ciblé starlette) + doc « env de dev » dans le README | E-20 | XS | `pip check` propre ; starlette 1.3.1 installé |
| B-11 | Alléger le démarrage : déplacer les effets de bord (scheduler, construction MCP, sanity modèle) du corps du module vers `lifespan` FastAPI ; mesurer l'import « pur » | E-21 | M | Import `app.api.main` < 10 s sans effets ; démarrage runtime inchangé fonctionnellement |
| B-12 | DX frontend : vitest `pool: 'vmThreads'` (jsdom ×1), dynamic import de Recharts sur les pages concernées | E-22, E-23 | XS–S | `npm test` < 30 s ; chunk recharts hors bundle initial |
| B-13 | Unifier la taxonomie d'erreurs (4 `errors.py`) et renommer `infrastructure/ml/metrics.py` (conflit de nom avec Prometheus) | E-14 | S | Nommage sans ambiguïté ; imports stables |
