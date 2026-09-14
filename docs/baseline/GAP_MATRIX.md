# Matrice des écarts — Baseline ThinkTuning (14/09/2026, commit `8616a9e`)

> Référence cible : `ARCHITECTURE.md` (règle d'or) + `docs/ARCHITECTURE_RECOMMENDATIONS.md`.
> Chaque écart est relié à un risque (`BASELINE_AUDIT.md` §7) et à un backlog (`BACKLOG.md`).
> Légende sévérité : 🔴 bloquant · 🟠 majeur · 🟡 mineur · ⚪ accepté/assumé.

## 1. Écarts d'architecture (cible hexagonale)

| ID | Composant / règle | Cible | Actuel | Écart constaté | Sévérité | Risque | Backlog |
|---|---|---|---|---|---|---|---|
| E-01 | `app/domain/**` pur | 0 dépendance externe | ✅ conforme | Aucun import framework/infra détecté (scan AST) | — | — | — |
| E-02 | `application/**` → domain uniquement | Use cases via ports | ❌ ~40 imports `infrastructure.*` | ~20 modules couplés (agent_cache, trainer_runner, cycle_runner, scheduler, intent_trainer, pipeline_runner, session_memory, copilot…) | 🟠 | R3 | B-3 |
| E-03 | `infrastructure/**` implémente les ports | Adapters sous le domaine | ⚠️ inversion | `persistence/model_versioning.py` importe `application/model_activation` + `application/model_signing` | 🟠 | R3, R4 | B-3 |
| E-04 | `api/**` sans logique métier | Façade fine | ⚠️ partiel | `routes/agent.py` : 1 910 lignes de logique métier, importé actif par `routes/v1/agent.py` (délégation strangler) | 🟠 | R3 | B-5 |
| E-05 | Composition root unique | Container DI unique | ⚠️ partiel | Singletons legacy (`get_*_store()`) hors container ; container réservé aux routes v1 | 🟡 | R3 | B-3 |
| E-06 | Contrat OpenAPI frais | `openapi.json` == spec reconstruite | ❌ dérive | Sérialisation `format: binary` vs `contentMediaType` (versions fastapi/pydantic flottantes) | 🔴 | R1 | B-1 |
| E-07 | Backend ↔ frontend verrouillés | Client TS généré du spec | ⚠️ indirect | `schema.d.ts` généré depuis l'`openapi.json` dérivé (cohérent entre eux, faux vs code réel) | 🔴 | R1 | B-1 |
| E-08 | MCP surface d'entrée | `/mcp/sse` + stdio | ✅ conforme | 28 tools / 10 resources / 5 prompts, version 2.0.0, tests conformance | — | — | — |

## 2. Écarts de dépendances et doublons

| ID | Constat | Cible | Détail | Sévérité | Risque | Backlog |
|---|---|---|---|---|---|---|
| E-09 | 7 cycles d'imports | Graphe acyclique | 5× `mongodb.py` hub ↔ stores, 1× ↔ `service_accounts`, 1× inversion de couche (E-03) | 🟠 | R4 | B-4 |
| E-10 | Hub `persistence/mongodb.py` | Stores découplés | 1 062 lignes, imports de symboles **privés** (`_audit`, `_hash_secret`) | 🟠 | R4 | B-4 |
| E-11 | 13 paires de routes legacy/v1 jumelles | Une seule surface | Strangler par design mais non absorbées (delegation v1→legacy) | 🟡 | R3 | B-5 |
| E-12 | 3 routes legacy jamais importées | Pas de code mort | `routes/health.py`, `routes/intent_train.py`, `routes/maintenance.py` (0 % coverage) | 🟡 | R7 | B-6 |
| E-13 | Racine du dépôt polluée | Arbre propre | ~25 fichiers de traces (`final_*.txt`, `ruff_*.txt`, `pytest_after.txt`…) | 🟡 | R7 | B-6 |
| E-14 | `metrics.py` ×4 | Nommage non ambigu | Middleware + 2 routes + ML metrics : 2 concepts sous 1 nom | 🟡 | R7 | B-6 |

## 3. Écarts de qualité / CI

| ID | Constat | Cible | Baseline mesurée | Sévérité | Risque | Backlog |
|---|---|---|---|---|---|---|
| E-15 | Suite tests : 3 échecs | 100 % verte | 1992 ✓ / 3 ✗ / 2 skip (2× OpenAPI + 1× isolation) | 🔴 | R1, R2 | B-1, B-2 |
| E-16 | Déterminisme tests | Sans pollution d'ordre | `test_agent_cache_lazy_resolution` : échoue en suite, passe seul | 🔴 | R2 | B-2 |
| E-17 | Couverture globale 76 %, outils sandboxés 5–44 % | Gate CI + plancher | 19 214 stmts / 4 611 miss ; `pytest-cov` absent des dev-deps | 🟠 | R5, R6 | B-7 |
| E-18 | mypy : 1 erreur, non bloquant | Bloquant progressif | `agent_core.py:581` ; CI `mypy \|\| true` ; `app/api` exclu du scope | 🟠 | R6 | B-8 |
| E-19 | ruff tests/ : 76 erreurs | 0 partout | 47 auto-fixables (I001×39, E501×23…) — hors scope CI | 🟡 | R6 | B-9 |
| E-20 | venv ≠ pins (starlette 1.0.1 vs ==1.3.1) | Environnement reproductible | Pins flottants fastapi/pydantic | 🟡 | R9 | B-10 |
| E-21 | Démarrage ~33–49 s, effets de bord à l'import | Démarrage rapide, effets en lifespan | Scheduler + MCP + sanity au chargement du module | 🟡 | R8 | B-11 |
| E-22 | vitest : jsdom recréé 19× (79 % du temps) | Pool optimisé | `pool: 'vmThreads'` candidat | ⚪ | — | B-12 |
| E-23 | Chunk recharts 335,7 kB | Chunks lazy | Recharts chargeable en dynamic import | ⚪ | — | B-12 |

## 4. Ce qui est conforme (pour mémoire)

| Domaine | Preuve |
|---|---|
| Domaine pur (E-01) | 0 import de framework dans `domain/**` |
| Surface API versionnée | 57 paths 100 % `/api/v1`, enveloppe d'erreur, auth par clé/JWT |
| MCP 2.0 | SSE + stdio, conformance testée, audit store branché |
| Supply-chain | pins Docker vérifiés, pip-audit + npm audit + trivy bloquants, SBOM |
| Frontend | typecheck 0 erreur, 197 tests verts, build 9,4 s |
| Encapsulation legacy | une seule couture (`agent_cache.py`) vers `agent/legacy/` |
