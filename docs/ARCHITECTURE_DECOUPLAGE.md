# Architecture & Découplage Backend ↔ Frontend

> **Statut (septembre 2026)** — Architecture actuelle et contrat de référence :
> backend FastAPI indépendant (`backend/`) + frontend React 19/Vite 8
> indépendant (`frontend/`). Les tableaux « réalisé » et « restant » sont
> historiques/backlog et ne doivent pas être lus comme des services actifs.

> Document de référence de la migration « strangler » du projet ThinkTuning.
> Il consolide l'état d'entrée, l'architecture cible, la mécanique de
> migration appliquée, les verrous de contrat et la dette technique restante.

---

## 1. Contexte & objectif

Le projet a migré d'un monolithe (FastAPI servant le dashboard statique + API
non versionnée) vers une architecture **découplée** :

- **Backend** : API FastAPI autonome (`/api/v1/*` versionné), image Docker
  dédiée, gunicorn + UvicornWorker.
- **Frontend** : dashboard React/Vite isolé (`frontend/`), image nginx
  dédiée, proxy `/api` vers l'API.
- **Contrat** : HTTP versionné, verrouillé par tests de contrat.

La contrainte majeure était de **ne pas réécrire** le socle existant : une
**architecture hexagonale pragmatique** a été posée sur les flux critiques,
et la migration de la surface consommée s'est faite par **strangler pattern**
(endpoint par endpoint, sans big bang).

---

## 2. Architecture cible

```
┌──────────────────────────────────────────────────────────────┐
│                    DASHBOARD (React/Vite)                     │
│  src/api (client manuel typé, transport centralisé _request)  │
└───────────────────────────────┬──────────────────────────────┘
                                │ HTTP /api/v1/*  +  WS /api/v1/train/stream/{id}
                                ▼
┌──────────────────────────────────────────────────────────────┐
│                   API FastAPI (api/)                          │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ app/api/routes/v1/*   ← versionné, enveloppe d'erreur v1   │  │
│  │   (façade anti-corruption : délègue ou encapsule)       │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ app/ (hexagonale pragmatique)                          │  │
│  │   domain/        entities, ports, errors               │  │
│  │   application/   use cases (predict, health, training…)│  │
│  │   infrastructure adapters (ml, persist, legacy)        │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ app/api/dependencies/composition.py   composition root     │  │
│  └────────────────────────────────────────────────────────┘  │
└───────────────────────────────┬──────────────────────────────┘
                                │
     ┌────────────┬─────────────┼──────────────┬────────────┐
     ▼            ▼             ▼              ▼            ▼
  infrastructure/ml  agent/legacy  stores    scheduler   MCP
  persist, ML)
```

**Couche `core/` reclassée** (référence) — majoritairement de
l'infrastructure, pas du domaine :

| Module                          | Couche réelle        | Usage v1                     |
|---------------------------------|----------------------|------------------------------|
| `predictor_cache`, `model_*`    | Infrastructure ML    | adaptateurs `app/infrastructure` |
| `scheduler`, `job_store`, `*_store` | Infrastructure persistance | partagé tel quel (kernel) |
| `trainer_runner`, `cycle_runner`, `pipeline_runner` | Orchestration legacy | appelé via handlers/delegation |
| `app/domain/entities/models.py` (TrainJob, enums, DTO) | Modèle partagé        | réutilisé tel quel (zéro dérive) |

---

## 3. Contrat API v1

### Versioning

- `/api/v1/*` : version stable, **la seule surface montée** (épuration faite :
  les 16 `include_router` legacy ont été retirés de `app/api/main.py`).
- Les fichiers legacy (`app/api/routes/*.py`) ne sont plus montés : les routes v1
  y délèguent par attribut de module (monkeypatchs des tests préservés).
- Toute évolution **breaking** = création d'un routeur `/api/v2/*`.

### Enveloppe d'erreur

Les erreurs métier répondent en payload **normalisé** :

```json
{ "error": { "code": "not_found", "message": "…", "details": {…} } }
```

Codes usités : `bad_request` (400), `validation_error` (422), `not_found`
(404), `conflict` (409), `model_not_available`/`model_unhealthy` (503),
`agent_run_error` (500), `gateway_timeout` (504).

- Source : `app/domain/errors.py` (hiérarchie `DomainError`).
- Runtime : `app/api/errors.py` (handler global) + `app/infrastructure/legacy_errors.py`
  (convertisseur `HTTPException` legacy → `DomainError`).
- Le transport frontend ne connaît PLUS que l'enveloppe v1 : le repli legacy
  (`detail`) a été neutralisé avec la surface legacy (`clientCore.ts` en
  enveloppe unique).

### Auth

- `X-API-Key` requise sur la surface **écrite** ; les GET de lecture
  (`/health`, `/health/model-sanity`, `/metrics*`, `/classifiers`,
  `/sessions`) sont **publics** (parité legacy) — posture **verrouillée par
  test**.
- WebSocket training : jeton en query `?token=` (les navigateurs ne peuvent
  pas poser de header sur un WS).

---

## 4. Chronologie du strangler (réalisé)

| Phase | Contenu | Surface v1 ajoutée |
|---|---|---|
| 0/1 | Socle hexagonal (domain/application/infrastructure), composition root, handler `DomainError`, montage `v1_router` | convention + infra |
| 2 | `GET /health` | 1 |
| 3a | `GET /health/model-sanity` + couche erreur unifiée + `SanityCaseResult` | 1 |
| 3b | `POST /api/v1/predict` + rate-limit | 1 |
| 3c | `POST /api/v1/predict/reload` | 1 |
| 3d-1 | `POST /train*` (jobs, status, history, cancel, schedules, WS `/train/stream/{id}`) | 10 |
| 3d-2 | `/train/intent*` (start, status, cancel, jobs, versions, activate) | 6 |
| 3d-3 | `/models/*` (details, active, activate, delete) + `/evaluate/confusion` | 5 |
| 3d-4 | `/agent/*` (settings, ask/core±stream, multi/ask/stream, approvals, flow) + `/sessions*` + `/chat/*` | 17 |
| 3d-5 | `/metrics*`, `/drift`, `/explain`, `/pipeline*`, `/active_learning*`, `/annotate*`, `/classifiers*` | 17 |
| complétion | `/api/v1/predict/batch` (multipart CSV, délégation legacy interne) | 1 |

**Total** : 58 routes v1 enregistrées (57 HTTP + 1 WS). **Épuration faite** :
la surface legacy (80 routes HTTP) n'est **plus montée** — `app/api/main.py` ne
monte que la v1, et les fichiers legacy servent uniquement de cible de
délégation / monkeypatch pour les tests.

---

## 5. Verrous de contrat (le contrat est une propriété codée, pas un artefact)

| Test | Rôle |
|---|---|
| `tests/test_api_v1_contract.py` | Paths v1 attendus invariants, DTO/ordre des champs, posture auth, `export_openapi.py` cohérent avec le spec. Toute dérive de contrat casse la CI. |
| `tests/test_api_v1_client_contract.py` | **Verrous croisés client ↔ backend** : (1) chaque `/api/v1/*` littéral du dashboard résout vers une route v1 enregistrée (par segments, comme le routeur — 404 silencieux impossible) ; (2) **post-strangler, verrous inversés** : `test_aucune_route_hors_v1_montee` (aucune route hors `/api/v1` dans le spec) et `test_aucun_appel_reseau_hors_v1` (tout appel réseau `request/fetch/WebSocket/EventSource` du client cible `/api/v1/*`) ; garde-fou de volume (≥ 50 routes v1). |
| `tests/test_openapi_export.py` | **Verrou de fraîcheur du spec** : `openapi.json` commité ≡ spec de l'app montée (toute dérive de contrat non commitée casse la CI AVANT la génération du client) ; export 100 % `/api/v1`, volume plausible ; le script `export_openapi.py` produit exactement le fichier commité. |
| Tests par tranche (`test_api_v1_*.py`) | Comportement (statuts, payloads, enveloppe d'erreur) verrouillé pour chaque surface migrée. |

Le spec OpenAPI est **générable à la demande** et alimente le client
TypeScript (`openapi-typescript`, dev-dep du dashboard) :

```bash
python export_openapi.py --out openapi.json   # 57 paths, 100 % v1
cd dashboard && npm run generate:api-types    # -> src/api/generated/schema.d.ts
```

Le verrou de fraîcheur (`test_openapi_export.py`) garantit que `openapi.json`
commité reflète toujours l'app montée. Branchement **progressif** des DTO du
client sur le schéma généré : `ApiHealth` est déjà dérivé de
`components["schemas"]["HealthResponse"]` (toute dérive backend casse `tsc`
avant de casser l'IHM) ; les autres DTO historiques suivront au fil de l'eau.

---

## 6. Ajouter un endpoint v1 (procédure)

1. Créer `app/api/routes/v1/<feature>.py`, l'ajouter dans `app/api/routes/v1/__init__.py`.
2. Préférer la **délégation au handler legacy** (parité par construction) tant
   que la logique n'a pas de use case dédié ; sinon port/usecase/adapter
   (cf. `prediction`, `health`, `training`).
3. Convertir les erreurs legacy via `app/infrastructure/legacy_errors.py`.
4. Ajouter le path dans `V1_PATHS` + posture auth dans
   `tests/test_api_v1_contract.py` — **c'est une décision explicite**.
5. Si le dashboard consomme le nouvel endpoint, le verrou croisé
   `test_api_v1_client_contract.py` le vérifiera automatiquement.
6. Tester : statuts/payload, enveloppe erreur, auth.

---

## 7. Déploiement

Deux images Docker indépendantes (Phase 5) :

| Service | Dockerfile | Expose | Healthcheck | User |
|---|---|---|---|---|
| API | `backend/Dockerfile` — multi-stage wheelhouse (pip offline), gunicorn + UvicornWorker, `init: true`, `--max-requests` | 8000 | `GET /api/v1/health` (public, exempté maintenance) | non-root |
| Dashboard | `frontend/Dockerfile` — build Vite + nginx standalone, template `envsubst` (`API_UPSTREAM=app:8000`), SSE/buffering off, user 101 | 8080 | GET `/` | non-root |

Compose : services `app` + `dashboard` ; le dashboard n'embarque plus l'API.

---

## 8. État courant & travaux restants

### Fait
- 100 % des endpoints consommés par le dashboard passent par `/api/v1/*`.
- Contrat verrouillé (2 tests de contrat + tests par tranche), OpenAPI
  exportable.
- Docker production-ready (2 images).
- **Épuration legacy (Phase A)** : aucun routeur legacy monté, verrous
  inversés (backend + client), transport frontend en enveloppe v1 unique,
  `openapi.json` généré (57 paths, 100 % v1).
- **Client TypeScript généré (Phase B)** : `openapi-typescript` en dev-dep
  (override TS 6 dans `package.json`), script `generate:api-types`, verrou de
  fraîcheur du spec (3 tests), DTO `ApiHealth` branché sur le schéma généré.
- **Extraction métier de la surface agent (Phase C — B-5, écarts E-04/E-11)** :
  voir §8.1 ci-dessous.

### 8.1 Phase C — absorption de `routes/agent.py` en use cases (réalisé)

Avant : `app/api/routes/agent.py` (1 910 lignes) mêlait logique métier, état
de module (runs/queues/bus), câblage infrastructure et DTOs. Après :

| Module | Rôle | Taille |
|---|---|---|
| `app/application/agent_surface.py` | Use cases purs (runs, streaming SSE core/multi, approbations, réglages, providers, Flow Map, sonde provider — stores/queues/bus **injectés**, zéro import FastAPI/infrastructure) | ~1 040 l. |
| `app/api/routes/agent.py` | Adaptateur legacy : câblage des collaborateurs + traduction `DomainError` → `HTTPException` (`{"detail": ...}`), **surface réduite aux 9 endpoints encore requis par les tests de contrat** | 392 l. (≈ -80 %) |
| `app/api/routes/v1/agent.py` | Surface v1 : **n'appelle plus le module legacy par attribut** — use cases directs + DTOs `app/api/schemas/agent`, erreurs via le handler `DomainError` global (enveloppe `{"error": ...}`) | 287 l. |
| `app/api/dependencies/mcp_first.py` | Garde lecture seule `MCP_FIRST` **partagée** par les deux surfaces (règle de policy unique — E-04) | 69 l. |
| `app/api/dependencies/agent_probe.py` | Sonde de connectivité (plan use case + appel HTTP) partagée legacy/v1 | 74 l. |

Règles établies (ADR-0003) :

1. **Source unique du câblage** : les collaborateurs (stores, factories, bus,
   télémétrie) restent définis dans `routes/agent.py` et sont lus à l'appel
   par la v1 (`agent_wiring.<nom>`) — c'est LE seam de monkeypatch des tests
   des deux surfaces ; aucune duplication d'état.
2. **Erreurs** : un seul statut HTTP par erreur (`DomainError.http_status`) ;
   l'adaptateur legacy l'habille en `{"detail": ...}`, le handler global v1 en
   `{"error": {"code", ...}}`.
3. **Retraits assumés** (surface legacy dépréciée, non montée en production) :
   endpoints sans consommateur ni équivalent v1/MCP retirés — copilot
   (`/suggest*`, `/complete`), tools custom/recommend/stats, `/runs`,
   `/audit`, `/features`, `/ws`, `/multi/ask` bloquant. Les use cases
   correspondants restent dans l'historique git ; les équivalents MCP
   (`tools/call`, audit, `orchestrate`) couvrent les besoins MCP.
4. **Dépréciation** inchangée : `DeprecationWarning` à l'import, en-têtes
   `Deprecation`/`Sunset`/`Warning: 299`, 405 `mcp_first_read_only` si
   `MCP_FIRST=true` (approbation humaine exceptée).

### Restant (post-épuration)
1. **Suppression des fichiers legacy** (`app/api/routes/*.py`, `core/*` devenus
   morts) : ~~la délégation par attribut de module devra d'abord être portée en
   use cases/adapters réels pour les flux concernés~~ **fait pour l'agent
   (Phase C, §8.1)** — reste le même travail pour les tranches non-agent
   (`predict`, `train`, `models`, …) avant leur retrait.
2. **Poursuivre le branchement des DTO** (`PredictionResult`, `ModelVersion`,
   `Explanation`, …) sur le schéma généré, puis consommer `operations` pour
   typer les chemins d'appels.
3. ~~**Extraction métier des handlers `app/api/routes/agent.py`**~~ — **RÉALISÉ
   (Phase C / B-5, §8.1)**.
4. **Schéma `Security` OpenAPI** transverse (le spec marque `X-API-Key` en
   `required: false` alors que les routes protégées répondent 401 sans clé —
   posture documentée dans les tests de contrat).

### Dette assumée
- `supervisord.conf`, `entrypoint.py`, `frontend/nginx.conf` : **supprimés en
  P1 (B-6)** avec mise à jour de leurs références (docstrings de
  `app/application/health_usecase.py` et `tests/test_api_v1_health.py`). Le
  backend Docker tourne sous gunicorn (`CMD` du Dockerfile, healthcheck
  `/api/v1/health`) ; le frontend est servi via `nginx.main.conf` + template
  envsubst. `frontend/nginx.main.conf` reste vivant (copié par le Dockerfile).
- 110 erreurs ruff au total : ~101 dans les routeurs legacy non montés
  (encore importés par la v1 via attribut de module) + ~9 sur le périmètre v1
  vivant. Nettoyage en 2 lots : `--fix` auto (76) puis manuel ciblé.
- `npm run lint` opérationnel (jiti installé) : 28 erreurs react-hooks
  préexistantes (`set-state-in-effect`, règles récentes) — portée corrective
  à planifier séparément (QA visuelle requise).