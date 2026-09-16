# Backlog priorisé — suite de la baseline (14/09/2026)

> Chaque item référence son écart (`GAP_MATRIX.md`) et son risque
> (`BASELINE_AUDIT.md` §7). Ordre recommandé : **ne pas démarrer de refactorisation
> structurelle avant B-1 et B-2** (contrat + déterminisme), sinon la baseline
> devient inutilisable pour prouver les régressions.

## P0 — Débloquer la baseline (avant toute refactorisation)

> ✅ **Clôturé le 14/09/2026** (branche `##-P0-—-Débloquer-la-baseline-(avant-toute-refactorisation)`).
> - **B-1** : `fastapi==0.141.1` / `pydantic==2.13.5` épinglés (`requirements.txt`
>   + miroir `pyproject.toml` ; `starlette==1.3.1` déjà en place) ; contrat
>   régénéré dans l'ordre ADR-0002 (`python export_openapi.py` →
>   `npm run generate:api-types`) : `"format": "binary"` →
>   `"contentMediaType"` (E-06) ; le client TS rattrapé au passage
>   `/api/v1/agent/providers` qui lui manquait (E-07 matérialisé, +282 l.).
>   Preuves : `test_openapi_export.py` 3/3 verts ; `pytest` 1996 passed /
>   2 skipped ; vitest 197 verts ; `vite build` OK ; ruff `app/` 0 erreur.
> - **B-2** : la précondition « namespace à froid » de
>   `test_agent_cache_lazy_resolution.py` est désormais **établie** par la
>   fixture (pop des symboles lazy au setup ET au teardown) au lieu d'être
>   assumée ; test anti-pollution ajouté (pollution d'ordre aléatorisée,
>   graine figée). Preuves : paire pollueur→test verte dans les DEUX ordres
>   (13/13 ×2) ; suite complète verte **2× de suite** (1996 passed /
>   2 skipped, exit 0).

| ID | Action | Écart | Effort estimé | Critère de clôture |
|---|---|---|---|---|
| B-1 | Réconcilier le contrat : régénérer `openapi.json` (`python export_openapi.py`), régénérer `schema.d.ts` (`npm run generate:api-types`), committer ensemble ; épingler `fastapi`/`pydantic`/`starlette` dans `requirements.txt` pour stabiliser la sérialisation | E-06, E-07, E-15 | S (½ j) | `test_openapi_export.py` 3/3 verts ; `pytest` + `vitest` verts ; CI backend verte |
| B-2 | Rétablir l'isolation des tests : identifier la fuite d'ordre (fixtures qui mutent des singletons : `agent_cache`, registres) et la neutraliser ; ajouter un test anti-pollution (run aléatorisé) | E-16 | M (1–2 j) | `pytest tests -p no:randomly` (ou double run ordre inversé) vert 2× de suite |

## P1 — Socle structurel (rendre la refactorisation sûre)

> ✅ **SCRUM-152 (L1) clôturé le 16/09/2026** (branche SCRUM-152) : exécution
> durable MCP et streaming SSE rendus fiables — pont d'événements thread →
> asyncio réellement annulable (`_SseEventBridge`, file boucle-ownée,
> `asyncio.timeout` : plus aucune fuite de thread consommateur par heartbeat) ;
> run durable PRÉPARÉ avant le premier octet (`orchestrate.started` expose
> `run_id`/`resumed`/`last_sequence` — plus de run fantôme créé par le worker) ;
> checkpoint et `last_sequence` MONOTONES (un événement worker tardif ne
> régresse plus `synthesis_running`) ; replay incrémental via
> `orchestrate_events` + `after_sequence` (séquences renvoyées par
> `append_event`, idempotent sur `event_id`) ; `parallel` réellement effectif
> par requête (dispatch + use cases, sans muter le singleton) ; Flow Map
> complète en streaming (plan, workers, synthèse, approbations HITL) ;
> annulation propre sur Stop (aucun run zombie, y compris dès le prélude) ;
> normalisation `phase`/`worker_id` réparée (un événement worker est classé
> « worker »). Front : curseur mémorisé depuis `orchestrate.started` et
> `replayOrchestrateEvents` (vitest). Preuves : `pytest` 2103 passed /
> 2 skipped ; `test_mcp_stream_stability.py` 14 tests ; vitest 226 passed ;
> `ruff check` vert sur les fichiers du lot ; `tsc --noEmit` vert.



> ✅ **B-8 clôturé le 14/09/2026** (branche P1) : erreur mypy résiduelle corrigée
> (`agent_core.py:581` — lambda à 2 paramètres non inférable contre
> `Callable[[dict[str, Any]], Any]`, remplacée par une fonction locale annotée
> préservant le late-binding `_tool=tool`) ; `|| true` retiré de la CI —
> **mypy est bloquant** (épinglé `2.3.1` = version venv locale, même logique que
> le pin ruff) ; sort de l'exclusion `app/api` : **maintenue** et documentée
> comme jalon explicite de B-5 (ADR-0005 §3). Preuves : `mypy app/` 0 erreur /
> 224 fichiers ; `pytest` 1996 passed / 2 skipped ; ruff check+format verts.

> ✅ **B-7 clôturé le 14/09/2026** (branche P1) : `pytest-cov>=6.0.0` ajouté aux
> dev-deps (`pyproject.toml [project.optional-dependencies].dev`) ; step CI
> `pytest --cov=app --cov-report=term --cov-fail-under=76` (plancher = baseline
> audit, ADR-0005 §1.1). Couverture vérifiée **76,00 % pile** (14 605/19 216
> stmts), identique avec et sans `TEST_MODE`, et aucun branchement
> `os.name`/`sys.platform` dans `app/` (parité Windows↔CI) — marge fine
> (~0,003 pt) assumée, le ratchet (+1 pt/sprint, ADR-0005 §2) la consolide ;
> **priorité ratchet déclarée : tests de `infrastructure/tools/` (5–44 %)**,
> restant au backlog. Preuves : 2 runs locaux `--cov-fail-under=76` verts
> (1996 passed / 2 skipped) ; YAML CI validé au parse.

> ✅ **B-6 clôturé le 14/09/2026** (branche P1) : `git rm` des 3 routes legacy
> jamais importées (`routes/health.py`, `routes/intent_train.py`,
> `routes/maintenance.py` — 0 référence vivante vérifiée par grep, la docstring
> du test v1 santé historisée) ; suppression de `backend/entrypoint.py` et
> `backend/supervisord.conf` (le Dockerfile backend tourne sous gunicorn,
> le supervisord référençait en outre un module obsolète `api.main:app`) et de
> `frontend/nginx.conf` (0 référence — le Dockerfile copie `nginx.main.conf` +
> template envsubst) ; références documentaires mises à jour
> (`ARCHITECTURE_DECOUPLAGE.md` « dette assumée », docstrings de
> `health_usecase.py` et `test_api_v1_health.py`) ; fichiers de traces restants
> purgés (non suivis, `*.log` déjà gitignorés — le lot `final_*.txt` de
> l'audit avait déjà été nettoyé). `_migration/` préservé (témoin historique).
> Preuves : `ruff check app/` 0 erreur, `ruff format app/` 271 OK,
> `ruff check tests/` 76 = baseline E-19 inchangée ; `test_api_v1_health.py`
> 8 passed ; suite complète 1996 passed / 2 skipped.

> ✅ **B-4 clôturé le 14/09/2026** (branche P1) : création de
> `persistence/common.py` — module neutre (0 dépendance `app.*` hors stdlib)
> extrait du hub `mongodb.py` : `MongoConfig`, `MongoClientProvider`, singleton
> `get/set/reset_mongo_provider`, `AtlasConnectionError`/`ATLAS_CONNECTION_HINT`,
> `_utcnow`, `_normalize_atlas_uri`, `_safe_host`, `_decode_settings_value`
> (SCRUM-137) et un **registre late-binding** (`register_mongo_store` /
> `get_mongo_store_class`, même technique que le late-binding `_tool` de B-8) ;
> les 5 factories de stores (`audit_store`, `flow_store`, `run_store`,
> `mcp_client_store`, `agent_settings`) résolvent désormais l'implémentation
> Mongo via ce registre au moment de l'appel — **plus aucun import
> `persistence.mongodb` côté stores** ; `MongoServiceAccountStore` rapatrié
> dans `security/service_accounts.py` (imports top-level du hub supprimés,
> symboles privés `_audit`/`_hash_secret` restent locaux — **E-10 levé**) ;
> `mongodb.py` devient façade (ré-exports compat : conftest, scripts,
> `routes/agent.py`) ; commentaires « import de module circulaire » réécrits.
> Critère affiné : **détecteur AST 7 → 1** — les 6 cycles du hub mongodb sont
> morts ; le cycle restant (`model_versioning` ↔ `application.model_activation`)
> EST l'inversion de couche E-03, dont l'élimination propre (port
> `ModelActivationPort`) est précisément le périmètre de **B-3** — le casser
> maintenant via registre changerait le comportement défensif du repli
> `resolve_model_dir` (lazy try/except). Preuves : `ruff check app/` 0 erreur,
> `ruff format app/` OK, `mypy app/` 0 erreur / **225 fichiers** (+common.py),
> tests ciblés persistance 106 passed, suite complète 1996 passed / 2 skipped.

> ✅ **B-3 clôturé le 14/09/2026** (branche P1) : port **`ModelActivationPort`**
> créé (`app/domain/ports/model_activation_ports.py`) — contrat « catalogue +
> pointeur de version active » (``model_root``, ``list_model_versions``,
> ``is_model_version_trained``, ``read/write_active_pointer``,
> ``get_active_pointer_path``, ``get_active_model_dir``) avec registre
> late-binding (``register/reset/get_model_activation_port``,
> ``resolve_active_model_dir``, même technique que les registres B-4/B-8) ;
> adaptateur par défaut `_DomainModelActivationAdapter` dans
> `persistence/model_versioning.py` (auto-enregistrement à l'import du module —
> couvre API, CLI et tests sans toucher `composition.py` ; env
> ``ACTIVE_MODEL_POINTER`` et ``MODEL_ROOT`` lus À CHAQUE appel → parité
> monkeypatch exacte avec l'implémentation historique) ;
> **`model_versioning.py` n'importe plus `application/`** (2 arêtes supprimées :
> lazy `model_activation` et lazy `model_signing`) → **inversion de couche E-03
> supprimée, détecteur AST : 1 → 0 cycle (baseline 7 → 0)** ; use case
> `application/model_activation.py` purifié (0 import infrastructure —
> délégations port, `MODEL_ROOT` local patchable par les tests) ;
> `model_sanity.py` purifié (lazy import → port, sémantique défensive
> try/except préservée) ; `model_signing.py` **requalifié**
> `application/` → `infrastructure/security/` (ADR-0003 §2 : module technique
> de supply-chain, importers mis à jour : `predictor.py`, `model_versioning.py`,
> `test_p2_auth_supplychain.py`). Test de garde **`tests/test_application_purity.py`**
> (scan AST top + lazy) : 0 import `app.infrastructure.*` dans `application/`
> hors **14 exceptions déclarées et DATÉES** (échéance 2026-10-15, ADR-0003
> §4) + 4 ratchets (exception périmée → échec ; plafond 14 figé ; modules
> purifiés verrouillés hors liste ; exception devenue inutile → purge). Critère
> affiné : « 0 import direct + exceptions temporaires datées » — les 14 modules
> restants (agent_cache, trainer_runner, predictor_cache, cycle_runner,
> session_memory…) passent par les ports à l'occasion de **B-5** (absorption
> `routes/agent`) et des ratchets suivants, module par module (strangler,
> ADR-0003 §1 : pas de big-bang). Preuves : `ruff check app/` 0 erreur,
> `ruff format` OK, `mypy app/` 0 erreur / **226 fichiers** (+port),
> **détecteur 0 cycle**, tests ciblés 27 passed (activation + supply-chain +
> purity), suite complète **2002 passed / 2 skipped** (baseline 1996 + 6 tests
> de garde).

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
