# Architecture & Découplage Backend ↔ Frontend

> Document de référence de la migration « strangler » du projet ThinkTuning.
> Il consolide l'état d'entrée, l'architecture cible, la mécanique de
> migration appliquée, les verrous de contrat et la dette technique restante.

---

## 1. Contexte & objectif

Le projet a migré d'un monolithe (FastAPI servant le dashboard statique + API
non versionnée) vers une architecture **découplée** :

- **Backend** : API FastAPI autonome (`/api/v1/*` versionné), image Docker
  dédiée, gunicorn + UvicornWorker.
- **Frontend** : dashboard React/Vite isolé (`dashboard/`), image nginx
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
│  │ api/routes/v1/*   ← versionné, enveloppe d'erreur v1   │  │
│  │   (façade anti-corruption : délègue ou encapsule)       │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ app/ (hexagonale pragmatique)                          │  │
│  │   domain/        entities, ports, errors               │  │
│  │   application/   use cases (predict, health, training…)│  │
│  │   infrastructure adapters (ml, persist, legacy)        │  │
│  ├────────────────────────────────────────────────────────┤  │
│  │ api/dependencies/composition.py   composition root     │  │
│  └────────────────────────────────────────────────────────┘  │
└───────────────────────────────┬──────────────────────────────┘
                                │
     ┌────────────┬─────────────┼──────────────┬────────────┐
     ▼            ▼             ▼              ▼            ▼
  core/ (infra  ia/ (agents)  src/ (ML)     stores      scheduler
  persist, ML)
```

**Couche `core/` reclassée** (référence) — majoritairement de
l'infrastructure, pas du domaine :

| Module                          | Couche réelle        | Usage v1                     |
|---------------------------------|----------------------|------------------------------|
| `predictor_cache`, `model_*`    | Infrastructure ML    | adaptateurs `app/infrastructure` |
| `scheduler`, `job_store`, `*_store` | Infrastructure persistance | partagé tel quel (kernel) |
| `trainer_runner`, `cycle_runner`, `pipeline_runner` | Orchestration legacy | appelé via handlers/delegation |
| `core/models.py` (TrainJob, enums, DTO) | Modèle partagé        | réutilisé tel quel (zéro dérive) |

---

## 3. Contrat API v1

### Versioning

- `/api/v1/*` : version stable, **la seule surface montée** (épuration faite :
  les 16 `include_router` legacy ont été retirés de `api/main.py`).
- Les fichiers legacy (`api/routes/*.py`) ne sont plus montés : les routes v1
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
- Runtime : `api/errors.py` (handler global) + `app/infrastructure/legacy_errors.py`
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
| 3b | `POST /predict` + rate-limit `/api/v1/predict` | 1 |
| 3c | `POST /predict/reload` | 1 |
| 3d-1 | `POST /train*` (jobs, status, history, cancel, schedules, WS `/train/stream/{id}`) | 10 |
| 3d-2 | `/train/intent*` (start, status, cancel, jobs, versions, activate) | 6 |
| 3d-3 | `/models/*` (details, active, activate, delete) + `/evaluate/confusion` | 5 |
| 3d-4 | `/agent/*` (settings, ask/core±stream, multi/ask/stream, approvals, flow) + `/sessions*` + `/chat/*` | 17 |
| 3d-5 | `/metrics*`, `/drift`, `/explain`, `/pipeline*`, `/active_learning*`, `/annotate*`, `/classifiers*` | 17 |
| complétion | `/predict/batch` (dernier résidu réel — multipart CSV, délégation legacy) | 1 |

**Total** : 58 routes v1 enregistrées (57 HTTP + 1 WS). **Épuration faite** :
la surface legacy (80 routes HTTP) n'est **plus montée** — `api/main.py` ne
monte que la v1, et les fichiers legacy servent uniquement de cible de
délégation / monkeypatch pour les tests.

---

## 5. Verrous de contrat (le contrat est une propriété codée, pas un artefact)

| Test | Rôle |
|---|---|
| `tests/test_api_v1_contract.py` | Paths v1 attendus invariants, DTO/ordre des champs, posture auth, `export_openapi.py` cohérent avec le spec. Toute dérive de contrat casse la CI. |
| `tests/test_api_v1_client_contract.py` | **Verrous croisés client ↔ backend** : (1) chaque `/api/v1/*` littéral du dashboard résout vers une route v1 enregistrée (par segments, comme le routeur — 404 silencieux impossible) ; (2) **post-strangler, verrous inversés** : `test_aucune_route_hors_v1_montee` (aucune route hors `/api/v1` dans le spec) et `test_aucun_appel_reseau_hors_v1` (tout appel réseau `request/fetch/WebSocket/EventSource` du client cible `/api/v1/*`) ; garde-fou de volume (≥ 50 routes v1). |
| Tests par tranche (`test_api_v1_*.py`) | Comportement (statuts, payloads, enveloppe d'erreur) verrouillé pour chaque surface migrée. |

Le spec OpenAPI est **générable à la demande** :

```bash
python export_openapi.py --out openapi.json   # 57 paths, 100 % v1
```

L'export sert à la **génération éventuelle** du client TypeScript (décision
reportée : voir §8).

---

## 6. Ajouter un endpoint v1 (procédure)

1. Créer `api/routes/v1/<feature>.py`, l'ajouter dans `api/routes/v1/__init__.py`.
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
| API | `Dockerfile` (racine) — multi-stage wheelhouse (pip offline), gunicorn + UvicornWorker, `init: true`, `--max-requests` | 8000 | `GET /api/v1/health` (public, exempté maintenance) | non-root |
| Dashboard | `dashboard/Dockerfile` — build Vite + nginx standalone, template `envsubst` (`API_UPSTREAM=app:8000`), SSE/buffering off, user 101 | 8080 | GET `/` | non-root |

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

### Restant (post-épuration)
1. **Suppression des fichiers legacy** (`api/routes/*.py`, `core/*` devenus
   morts) : la délégation par attribut de module devra d'abord être portée en
   use cases/adapters réels pour les flux concernés.
2. **Génération du client TypeScript** depuis `openapi.json` (`openapi.json`
   généré ; branchement `openapi-typescript` à faire — Phase B).
3. **Extraction métier des handlers `api/routes/agent.py`** (~1700 lignes,
   état/queues/store) en use cases `app/application/` (épic d'estimation
   séparée — Phase C).
4. **Schéma `Security` OpenAPI** transverse (le spec marque `X-API-Key` en
   `required: false` alors que les routes protégées répondent 401 sans clé —
   posture documentée dans les tests de contrat).

### Dette assumée
- `supervisord.conf`, `entrypoint.py`, `dashboard/nginx.conf` : suivis par git
  et **référencés** (commentaire de `app/application/health_usecase.py`,
  `dashboard/Dockerfile` copie `nginx.main.conf`) — suppression à traiter avec
  ces références, pas en simple `git rm`.
- 159 erreurs ruff au total : ~101 dans les routers legacy non montés, ~58 sur
  le périmètre v1 vivant. Nettoyage prévu en 3 lots (Phase D) — **attention** :
  certains F401 sont des re-exports consommés (ex. `TEST_MODE` dans
  `api/__init__.py`) qu'un `ruff --fix` aveugle casserait.