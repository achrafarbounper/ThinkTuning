# Audit baseline ThinkTuning — Établissement d'une référence fiable avant refactorisation

> **Statut (14 septembre 2026)** — Mesures figées sur le commit `8616a9e`
> (branche `SCRUM-141`). Ce document est la **baseline de référence** : toute
> refactorisation doit pouvoir être comparée à ces chiffres, reproduits via les
> commandes de la section 8.
>
> Compléments : [`GAP_MATRIX.md`](./GAP_MATRIX.md) (matrice des écarts),
> [`BACKLOG.md`](./BACKLOG.md) (backlog priorisé), [`adr/`](./adr/) (décisions
> d'architecture).

---

## 1. Méthode

| Élément | Choix | Justification |
|---|---|---|
| Périmètre figé | commit `8616a9e`, branche `SCRUM-141`, arbre git propre | Reproductibilité : les mesures ne sont comparables que sur un état commité |
| Référence « avant » | `_migration/baseline/` (snapshot pré-migration : `backend/src/`, `backend/ia/`, `app/legacy/core/`) | Prouve l'absorption legacy et sert de témoin pour la comparaison cible |
| Cible de référence | `ARCHITECTURE.md` (règle d'or des dépendances) + `docs/ARCHITECTURE_RECOMMENDATIONS.md` §2 | Les deux documents font autorité sur l'architecture cible |
| Mesures exécutées | pytest, coverage (installé ponctuellement), ruff, ruff format, mypy, tsc, vitest, vite build, temps de démarrage | Couvre les critères : tests, couverture, lint, typage, démarrage |
| Analyse statique custom | script AST de détection de cycles + greps d'imports par couche | Détecte doublons, dépendances circulaires et composants legacy |

---

## 2. Cartographie de la structure réelle

### 2.1 Volumétrie

| Zone | Fichiers | Lignes | Remarques |
|---|---|---|---|
| `backend/app/` | 274 `.py` | 44 177 | 6 couches + `agent/legacy/` (16 modules) |
| `backend/tests/` | 147 `.py` | 24 243 | ratio tests/app ≈ 0,55 |
| `backend/` racine | 28 scripts `.py` | — | `train.py`, `predict.py`, `pipeline.py`… à la racine (pas sous `scripts/`) |
| `backend/scripts/` | 10 outils | — | `check_docker_pins.py` (CI), `export_onnx.py`, `retention.py`… |
| `frontend/src/` | 114 `.ts/.tsx` | 25 908 | dont `api/generated/schema.d.ts` (140 kB) |
| `docs/` | 25 documents | — | 12 racines + `mcp/` (13) + `rfc/` |
| `_migration/` | 15 scripts/reports + snapshot | — | témoin pré-migration, **référence historique uniquement** |

### 2.2 Backend — couches effectives

```
backend/app/
├── domain/            entities/ ports/ errors/ utils/        ← PUR (vérifié, §4.1)
├── application/       31 modules (use cases + services ex-core/, copilot/)
├── agent/             core.py (934 l.), factory, memory/, policies/, tool_router
│   └── legacy/        16 modules runtime v1 — orchestrator (1 363 l.), agent_core (987 l.)
├── api/               main.py, routes/ (17 legacy), routes/v1/ (18), middlewares/, dependencies/, schemas/
├── infrastructure/    persistence/ (16 stores), ml/ (dataset, model, inference,
│                      augmentation, classifiers), llm/, mcp/ (9 sous-paquets),
│                      tools/ (14 outils sandboxés), security/, events/, context/, training/
└── config/            settings.py (pydantic-settings), flags, training_config, logging_setup
```

Hotspots de taille (top 6) :

| Fichier | Lignes | Rôle |
|---|---|---|
| `app/api/routes/agent.py` | 1 910 | Handler agentique legacy — **actif** via délégation v1 |
| `app/agent/legacy/orchestrator.py` | 1 363 | Orchestrateur multi-agents v1 |
| `app/infrastructure/persistence/mongodb.py` | 1 062 | Hub de persistance MongoDB (délégation vers stores) |
| `app/agent/legacy/agent_core.py` | 987 | Boucle agentique v1 |
| `app/agent/core.py` | 934 | Noyau agentique v2 (cible) |
| `app/application/agent_cache.py` | 914 | Cache/registres réabsorbés — **seule couture** vers `agent/legacy/` |

### 2.3 Frontend

```
frontend/src/
├── api/               clientCore.ts (transport unique), sentimentApiClient.ts,
│                      agentSettings, mcpClient, flowApi, intentTrainApi, authSession,
│                      prometheusParser + generated/schema.d.ts (types OpenAPI)
├── components/        auth/ chat/ flowmap/ layout/ ui/
├── context/           AppProvider/AppContext (état global)
├── hooks/             usePolling, useLocalStorage, useIntentCache, useExplain…
├── pages/  lib/  styles/  test/
```

### 2.4 CI/CD (`.github/workflows/ci.yml`)

| Job | Étapes | Verrou |
|---|---|---|
| `backend` | ruff check `app/` → ruff format `app/` → mypy `app/` (`|| true`) → pytest | mypy **non bloquant** ; tests non lintés |
| `frontend` | `tsc --noEmit` → vitest → `vite build` | bloquant |
| `supply-chain` | pins Docker → pip-audit → npm audit → trivy → SBOM CycloneDX | bloquant High/Critical |

---

## 3. Lecture des contrats et configurations clés

- **Contrat API** : `backend/openapi.json` (210 kB, 57 paths, 100 % `/api/v1`) +
  `export_openapi.py` (script de génération) + tests de fraîcheur
  (`tests/test_openapi_export.py`) + verrou croisé
  (`tests/test_api_v1_client_contract.py`) — **2 des 3 tests de fraîcheur échouent** (§6).
- **Ports du domaine** : 6 modules (`ports.py`, `prediction_ports.py`,
  `training_ports.py`, `model_versioning_ports.py`, `authorization_ports.py`,
  `mcp_ports.py`) — Protocols typés, couverts par `tests/test_domain_ports.py`
  et `tests/test_persistence_ports.py`.
- **Composition root** : `app/api/dependencies/composition.py` — container DI
  minimal (factories paresseuses + singletons), consommé par les routes v1 ;
  le code legacy passe par des singletons (`get_*_store()`).
- **Config** : `backend/pyproject.toml` (ruff py311/100 cols, mypy 3.11 avec
  `app/api` exclu, coverage `source=["app"]`), `requirements.txt` (miroir du
  pyproject, pins starlette `==1.3.1`), `pytest.ini` (pythonpath=.).
- **Frontend** : `package.json` (scripts `typecheck`/`test`/`build`/
  `generate:api-types`), `tsconfig.json`, `eslint.config.ts`, `vite.config.ts`.

---

## 4. Comparaison architecture actuelle ↔ cible

### 4.1 Règle d'or des dépendances (`ARCHITECTURE.md`) — verdict par règle

| # | Règle cible | Verdict | Preuve |
|---|---|---|---|
| 1 | `domain/**` ne dépend de rien | ✅ **CONFORME** | 0 import fastapi/torch/transformers/sqlite/pymongo dans `app/domain/**` (scan AST + grep) |
| 2 | `agent/**` et `application/**` ne dépendent que de `domain` | ❌ **NON CONFORME** | ~40 imports `app.infrastructure.*` dans ~20 modules `application/` (agent_cache, trainer_runner, cycle_runner, scheduler, intent_trainer…) |
| 3 | `infrastructure/**` implémente les ports | ⚠️ **PARTIEL** | Adapters v1 OK (`predictor_adapter`, `training_adapter`, `legacy_errors`) MAIS inversion : `persistence/model_versioning.py` → `application/model_activation.py` et `application/model_signing.py` |
| 4 | `api/**` assemble et expose, aucune logique métier | ⚠️ **PARTIEL** | `routes/v1/*` délèguent ✓ ; mais `routes/agent.py` (1 910 l.) reste porteur de logique métier et est **importé actif** par `routes/v1/agent.py` |
| 5 | MCP = surface d'entrée privilégiée | ✅ **CONFORME** | `mcp_router` monté (`/mcp/sse`), serveur `thinktuning-mcp@2.0.0`, 28 tools / 10 resources / 5 prompts |
| 6 | Composition root = point unique d'instanciation | ⚠️ **PARTIEL** | Container utilisé par v1 uniquement ; les singletons legacy (`get_job_store()`…) contournent le container |

### 4.2 Migration « strangler » — état des résidus

| Résidu | État | Détail |
|---|---|---|
| `app/legacy` / `ia.` / `src.` imports | ✅ **ÉLIMINÉS** | 0 occurrence dans `backend/app` actuel (le snapshot `_migration/baseline/` en contient encore, normal) |
| `app/agent/legacy/` (runtime v1) | ⚠️ **ENCAPSULÉ** | 16 modules ; une seule couture d'entrée : `application/agent_cache.py` (3 refs) ; couverture moyenne 72–91 % |
| Routes legacy `app/api/routes/*.py` | ⚠️ **DÉLÉGUÉES** | Non montées (seuls `v1_router` + `mcp_router` le sont) mais 3 fichiers **jamais importés** (code mort) et 13 paires legacy/v1 cohabitent |
| `supervisord.conf`, `entrypoint.py`, `nginx.conf` | ⚠️ **DETTE ASSUMÉE** | Suivis par git, référencés dans le code — suppression à traiter avec ces références |
| Racine dépôt polluée | ❌ **À NETTOYER** | ~25 fichiers de traces de migration (`final_*.txt`, `ruff_*.txt`, `pytest_after.txt`…) |

---

## 5. Doublons, dépendances circulaires, composants legacy

### 5.1 Dépendances circulaires (7 détectées — script AST sur `app/`)

Toutes via imports **paresseux** (fonctions) : pas de crash à l'exécution, mais
dette architecturale qui bloque le remplacement isolé des modules.

| Cycle | Nature |
|---|---|
| `persistence/mongodb.py` ↔ `persistence/agent_settings.py` | hub ↔ store (import paresseux de `SETTING_KEYS`) |
| `persistence/mongodb.py` ↔ `persistence/audit_store.py` | hub ↔ store (constantes `MCP_ACTIONS`, `redact`) |
| `persistence/mongodb.py` ↔ `persistence/flow_store.py` | hub ↔ store (`STATUSES`) |
| `persistence/mongodb.py` ↔ `persistence/mcp_client_store.py` | hub ↔ store (erreurs métier) |
| `persistence/mongodb.py` ↔ `persistence/run_store.py` | hub ↔ store (`STATUSES`) |
| `persistence/mongodb.py` ↔ `security/service_accounts.py` | hub ↔ service (`_audit`, `_hash_secret` — symboles privés importés !) |
| `persistence/model_versioning.py` ↔ `application/model_activation.py` | **inversion de couche** infrastructure → application |

**Cause racine** : `mongodb.py` est un hub de 1 062 lignes qui réimplémente en
MongoDB les opérations des stores SQLite et importe leurs constantes ; les
stores importent `mongodb` pour la détection de backend.

### 5.2 Doublons de noms de fichiers (hors `__init__.py`)

| Groupe | Occurrences | Lecture |
|---|---|---|
| `errors.py` | ×4 (`domain`, `api`, `agent/legacy`, `infrastructure/llm`) | Erreurs par couche — acceptable, mais 4 taxonomies distinctes à unifier à terme |
| `metrics.py` | ×4 (middleware, 2 routes, `infrastructure/ml`) | 2 concepts différents (Prometheus vs ML) — confusion de nom |
| routes legacy/v1 jumelles | ×13 paires (`agent`, `health`, `models`, `mcp`, `pipeline`, `sessions`, `classifiers`, `drift`, `evaluate`, `explain`, `active_learning`, `maintenance`, `metrics`) | Strangler par design — à absorber endpoint par endpoint |
| `health.py` / `mcp.py` / `models.py` / `prediction.py` | ×3 | routes + schemas/domain entities — OK (couches distinctes) |

### 5.3 Code mort et legacy

| Élément | Statut | Preuve (couverture) |
|---|---|---|
| `app/api/routes/health.py` (28 stmts) | **mort — jamais importé** | 0 % |
| `app/api/routes/intent_train.py` (68 stmts) | **mort — jamais importé** | 0 % |
| `app/api/routes/maintenance.py` (15 stmts) | **mort — jamais importé** | 0 % |
| `app/infrastructure/ml/inference/predictor.py` (158 stmts) | non chargé dans la suite (import paresseux, chemin TEST_MODE) | 0 % |
| `app/agent/legacy/` (16 modules) | vivant via `agent_cache` | 48–100 % |
| 13 routes legacy montées-non-montées | vivantes via délégation v1 (`agent.py`), ou mortes (§5.3) | 39–100 % |

---

## 6. Mesures de la baseline (14/09/2026, commit `8616a9e`, Windows CPU)

### 6.1 Tests backend

| Mesure | Valeur |
|---|---|
| Résultat | **1992 passed / 3 failed / 2 skipped** |
| Durée (sans coverage) | **272 s** (4 min 32) |
| Durée (avec coverage) | 422 s (7 min 02) |
| Échec 1–2 | `test_openapi_export.py::test_openapi_json_est_frais` et `::test_export_openapi_script_produit_le_meme_fichier` — **`openapi.json` commité a dérivé du spec reconstruit** (`"format": "binary"` vs `"contentMediaType": "application/octet-stream"`) |
| Échec 3 | `test_agent_cache_lazy_resolution.py` — **passe isolément, échoue en suite complète** (pollution d'ordre de tests) |

Cause racine de la dérive OpenAPI : `fastapi>=0.110.0` / `pydantic>=2.0.0`
flottants ; l'environnement local résout **fastapi 0.141.1 / pydantic 2.13.5**,
dont la génération JSON Schema diffère de celle du fichier commité.
Constat additionnel : **starlette 1.0.1 installé vs `==1.3.1` épinglé** — le venv
local est désynchronisé des pins.

### 6.2 Couverture (pytest-cov 7.1.0, installé ponctuellement)

| Périmètre | Stmts | Miss | Couverture |
|---|---|---|---|
| **TOTAL `app/`** | 19 214 | 4 611 | **76 %** |

Zones < 50 % (triées par gravité) :

| Zone | Couverture |
|---|---|
| `api/routes/{intent_train, maintenance, health}` | 0 % (code mort) |
| `infrastructure/ml/inference/predictor.py` | 0 % (paresseux) |
| `infrastructure/tools/` : gpu (5 %), discovery (17 %), search (14 %), ops (19 %), database (20 %), plugin (26 %), network (28 %), docker/calc (43 %), ml_tools (44 %) | **outils sandboxés quasi non testés** |
| `application/` : cycle_runner (17 %), intent_trainer (28 %), scheduler (33 %), classifier_monitoring (34 %), explain_agent (38 %), agent_cache (49 %) | services réabsorbés peu testés |
| `persistence/` : run_store (34 %), approval_store (37 %), audit_store (42 %) | MongoDB partiellement testé |
| `infrastructure/training/` adapters (40–42 %) | seams v1 peu exercées |

### 6.3 Lint & format

| Périmètre | Résultat |
|---|---|
| `ruff check app/` (scope CI) | ✅ **0 erreur** |
| `ruff format --check app/` | ✅ 274 fichiers formatés |
| `ruff check tests/` (hors CI) | ❌ **76 erreurs** : 39 I001 (imports non triés), 23 E501, 6 B017, 4 W292, 3 F401, 1 UP017 — 47 auto-fixables |

### 6.4 Typage

| Périmètre | Résultat |
|---|---|
| `mypy app/` (224 fichiers, scope CI sans `app/api`) | **1 erreur** (`agent/legacy/agent_core.py:581` — « Cannot infer type of lambda ») — 67 s |
| `tsc --noEmit` (frontend) | ✅ **0 erreur** |
| Verrou CI | `mypy app/ || true` → **mypy ne bloque jamais** ; `app/api` exclu du scope mypy |

### 6.5 Frontend

| Mesure | Valeur |
|---|---|
| vitest | **197 tests / 19 fichiers — 100 % verts**, 62 s (jsdom recréé 19× = 79 % du temps) |
| `vite build` | ✅ 9,4 s — bundle principal 51,9 kB + react-vendor 192 kB + **recharts 335,7 kB** (gzip 98 kB) |

### 6.6 Démarrage

| Mesure | Valeur |
|---|---|
| Import `app.api.main` à froid (processus neuf, cache OS chaud) | **~33–49 s** (3 mesures : 32,6 / 48,9 / 99,6 s sous contention) |
| Reload chaud (même process) | ~5 ms |
| Effets de bord à l'import | démarrage du scheduler, construction du serveur MCP, sanity modèle (documenté comme voulu) |

### 6.7 Conformité venv ↔ pins

| Paquet | Épinglé (requirements.txt/pyproject) | Installé (venv local) |
|---|---|---|
| fastapi | `>=0.110.0` (flottant) | 0.141.1 |
| pydantic | `>=2.0.0` (flottant) | 2.13.5 |
| starlette | `==1.3.1` | **1.0.1 — écart** |

---

## 7. Risques prioritaires

| # | Risque | Impact | Probabilité | Niveau |
|---|---|---|---|---|
| R1 | Dérive du contrat OpenAPI commité (sérialisation dépendante des versions flottantes fastapi/pydantic) | Client TS généré faux après `generate:api-types` ; faux verts/rouges en CI selon version résolue | Haute (déjà matérialisé) | 🔴 **P0** |
| R2 | Suite de tests non déterministe (pollution `agent_cache`) | Fausses alertes, perte de confiance, tests skippés en pratique | Haute (déjà matérialisé) | 🔴 **P0** |
| R3 | `application/` couplée à `infrastructure/` (~20 modules) + inversion `infrastructure → application` | Testabilité hexagonale illusoire ; refactorisation risquée sans ce socle | Moyenne | 🟠 **P1** |
| R4 | Cycles d'imports (hub `mongodb.py` + inversion model_versioning) | Rechargement/isolation fragile ; impossible de remplacer un store isolément | Moyenne | 🟠 **P1** |
| R5 | Outils sandboxés quasi non testés (5–28 %) — surface d'exécution d'outils LLM | Régression silencieuse sur la sécurité du sandbox | Moyenne | 🟠 **P1** |
| R6 | Gates qualité incomplets en CI (mypy `|| true`, pas de couverture, tests non lintés) | La dette peut croître sans signal | Haute | 🟠 **P1** |
| R7 | Code mort non supprimé (3 routes legacy 0 %) + fichiers de traces à la racine du dépôt | Confusion, maintenance fantôme | Certaine | 🟡 **P2** |
| R8 | Démarrage lourd (~35–50 s) avec effets de bord à l'import | Boucle dev lente ; diagnostics difficiles | Moyenne | 🟡 **P2** |
| R9 | venv local désynchronisé des pins (starlette) + `pytest-cov` absent des dev-deps | Reproductibilité locale ≠ CI | Moyenne | 🟡 **P2** |

---

## 8. Commandes de validation reproductibles

Exécutées depuis `backend/` (PowerShell, venv activé) :

```powershell
# 0. État figé
git log --oneline -1                     # attendu : 8616a9e (baseline)
git status --short                        # attendu : vide

# 1. Tests (baseline : 1992 passed, 3 failed, 2 skipped, ~4 min 32)
$env:TEST_MODE='1'
python -m pytest tests -q --tb=no -p no:cacheprovider

# 2. Couverture (baseline : 76 % TOTAL) — pytest-cov requis
python -m pip install pytest-cov
python -m pytest tests -q --tb=no -p no:cacheprovider --cov=app --cov-report=term

# 3. Lint (baseline : app/ 0 erreur ; tests/ 76)
python -m ruff check app/
python -m ruff check tests/ --statistics
python -m ruff format --check app/

# 4. Typage (baseline : 1 erreur sur 224 fichiers, ~67 s)
python -m mypy app

# 5. Démarrage (baseline : ~33–49 s à froid)
Measure-Command { python -c "import app.api.main" }

# 6. Cycles d'imports (baseline : 7 — exit 1 si dépassement de la baseline)
python ../docs/baseline/tools/detect_cycles.py

# 7. Frontend (baseline : typecheck OK, 197 tests OK, build 9,4 s)
cd ../frontend
npm run typecheck ; npm test ; npm run build
```

---

## 9. Traçabilité vers les livrables

| Critère d'acceptation | Livrable |
|---|---|
| Structure réelle cartographiée | §2 (ce document) |
| Docs / configs / contrats / tests lus | §3 |
| Comparaison actuelle ↔ cible | §4 |
| Doublons / cycles / legacy identifiés | §5 |
| Risques prioritaires | §7 |
| Tests, couverture, lint, typage, démarrage mesurés | §6 |
| Matrice des écarts | [`GAP_MATRIX.md`](./GAP_MATRIX.md) |
| Backlog priorisé | [`BACKLOG.md`](./BACKLOG.md) |
| Commandes de validation | §8 |
| Décisions d'architecture | [`adr/`](./adr/) |
