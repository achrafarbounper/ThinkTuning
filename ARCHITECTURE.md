# Architecture — ThinkTuning

> **Statut (septembre 2026)** — État actuel : backend FastAPI (Python 3.11 en
> développement/CI, image Python 3.13) et frontend React 19 + TypeScript 6 +
> Vite 8 séparés. Les sections « migration », « backlog » et « réalisé »
> décrivent l'historique et ne sont pas des composants actifs supplémentaires.

> Backend ML d'analyse de sentiments FR/EN avec couche agentique
> (API FastAPI + agent **Intent → Plan → Action**).

Ce document décrit l'architecture cible en cours de mise en place
(architecture hexagonale), l'absorption du code historique
(`api/`, `ia/`, `core/`, `src/` — désormais absorbés ou encapsulés sous
`backend/app/`), et
les conventions à respecter pour toute évolution.

---

## 0. Carte de navigation pour agents IA

Cette section est l'index court à consulter avant toute recherche ou
contribution. Les chemins sont relatifs à la racine du dépôt.

### Entrées et responsabilités

| Couche | Rôle | Entrée principale | Dépendances autorisées |
| --- | --- | --- | --- |
| Backend / API | Composition HTTP, middleware, routes `/api/v1` et cycle de vie | [`backend/app/api/main.py`](backend/app/api/main.py) — `app` | `app/api` peut assembler `domain`, `application`, `agent` et `infrastructure`; aucune logique métier nouvelle dans les routes |
| Backend / domaine | Entités, erreurs et ports stables | [`backend/app/domain/`](backend/app/domain/) | Bibliothèque standard et types de domaine uniquement; jamais FastAPI, stockage, LLM ou framework d'infrastructure |
| Backend / application | Cas d'usage, orchestration applicative et services | [`backend/app/application/`](backend/app/application/) | `app/domain`; les détails externes passent par des ports ou des adaptateurs injectés |
| Backend / agent | Boucle Intent → Plan → Policy → Budget → Action | [`backend/app/agent/core.py`](backend/app/agent/core.py) — `AgentCore` | `app/domain` et les ports; la configuration et les adaptateurs sont fournis par la composition |
| Backend / infrastructure | Adaptateurs ML, LLM, persistance, sécurité, outils et transports | [`backend/app/infrastructure/`](backend/app/infrastructure/) | Peut dépendre de `app/domain`, `app/application` et des bibliothèques externes; implémente les ports, sans faire remonter ses détails |
| MCP / HTTP | Transport JSON-RPC streamable HTTP + SSE, discovery et orchestration | [`backend/app/infrastructure/mcp/mcp_server_sse.py`](backend/app/infrastructure/mcp/mcp_server_sse.py) — `POST /mcp/sse` | Réutilise `domain`, `agent` et les adaptateurs MCP; ne modifie pas le contrat OpenAPI REST |
| MCP / stdio | Serveur MCP ligne par ligne pour clients locaux | [`backend/app/infrastructure/mcp/mcp_server_stdio.py`](backend/app/infrastructure/mcp/mcp_server_stdio.py) — `thinktuning-mcp` | Même serveur MCP et mêmes policies que le transport HTTP; stdout est réservé au JSON-RPC, les logs vont sur stderr |
| Frontend | Composition UI, navigation hashée et état de session | [`frontend/src/main.tsx`](frontend/src/main.tsx) puis [`frontend/src/App.tsx`](frontend/src/App.tsx) | `api/` pour les appels réseau; composants, pages et hooks ne font pas d'appel `fetch` direct |
| Contrats générés | Types TypeScript dérivés du contrat REST | [`frontend/src/api/generated/schema.d.ts`](frontend/src/api/generated/schema.d.ts) | Source unique [`backend/openapi.json`](backend/openapi.json); régénérer plutôt qu'éditer manuellement |

### Parcours de recherche recommandé

1. Identifier la surface appelée : route REST `/api/v1`, MCP `/mcp/sse` /
   `thinktuning-mcp`, ou écran frontend.
2. Partir de l'entrée ci-dessus, puis suivre le cas d'usage vers `domain`
   (port/entité) et enfin l'adaptateur `infrastructure`.
3. Pour un appel frontend, commencer par `frontend/src/api/`, puis remonter
   vers la page ou le composant; pour un flux SSE, commencer par
   `frontend/src/components/chat/streamSse.ts`.
4. Chercher d'abord le symbole (`rg`, recherche de définition), puis ses tests
   (`backend/tests/` ou `frontend/src/**/*.test.*`). Ne pas commencer par les
   répertoires historiques `_migration/`, `experiments/` ou `outputs/`.

### Convention de nommage et d'exports

- Python : modules et fonctions en `snake_case`, classes et exceptions en
  `PascalCase`, constantes en `UPPER_SNAKE_CASE`. Les ports portent le suffixe
  `Port`; les implémentations/adaptateurs indiquent leur technologie
  (`Mongo...Store`, `Http...Client`, `...Adapter`).
- TypeScript/React : composants, pages et types en `PascalCase`; hooks en
  `useX`; clients et helpers en `camelCase`; tests adjacents portent le suffixe
  `.test.ts` ou `.test.tsx`.
- Les façades publiques déclarent explicitement `__all__` côté Python et
  réexportent depuis un `index.ts` côté frontend lorsque le dossier en possède
  un. Ne pas contourner une façade pour importer un détail privé.
- Les nouveaux exports doivent rester minimaux et nommés; ne pas ajouter
  d'export global « pratique » ni d'import circulaire. Un changement de
  contrat public exige la mise à jour conjointe de l'OpenAPI, des types générés,
  des clients et des tests.

### Commandes de validation par couche

| Surface modifiée | Validation ciblée |
| --- | --- |
| Backend Python | `cd backend; ruff check app/; ruff format --check app/; mypy app/; pytest tests/ -q` |
| Backend MCP | `cd backend; pytest tests/test_mcp_*.py -q` puis `pytest tests/test_mcp_sqlite_migration.py tests/test_mcp_mongo_run_store.py -q` |
| Frontend | `cd frontend; npm run typecheck; npm run lint; npm test; npm run build` |
| Types REST | `cd frontend; npm run generate:api-types`, puis la validation frontend |
| Documentation seule | Vérifier les liens et la cohérence avec les chemins réels; aucun contrat ou build applicatif n'est requis |

Les mêmes étapes sont exécutées par [`.github/workflows/ci.yml`](.github/workflows/ci.yml);
la CI reste l'autorité pour les versions d'outils et le seuil de couverture.

### Règles de contribution

- Une modification reste dans sa couche; si elle doit franchir une frontière,
  passer par un port, un cas d'usage ou un client dédié plutôt que par un
  import direct.
- Préserver les contrats REST v1, MCP et frontend. Toute évolution de contrat
  doit être explicitement accompagnée de ses artefacts générés et de tests.
- Ajouter ou déplacer un point d'entrée implique de mettre à jour cette carte,
  les README concernés et la commande de validation associée.
- Décrire les erreurs via les types et enveloppes existants; ne pas avaler une
  exception ni fournir un fallback silencieux.

---

## 1. Vue d'ensemble

```
                                          MCP (S7) = SURFACE D'ENTRÉE
                                          POST /mcp/sse (JSON-RPC 2.0 ⇄ SSE)
                                          + stdio `thinktuning-mcp`
                                                     │ tools/call orchestrate
                                                     ▼
  HTTP/WS/SSE ─▶ │  backend/app/api/ (FastAPI) — routes versionnées /api/v1
                 └──────────────┬─────────────────────────────┘
                                │ Depends / use-cases
                 ┌──────────────▼─────────────────────────────┐
                 │  app/agent/core.py — AgentCore             │
                 │  Intent → Plan → Policy → Budget → Action  │
                 └───────┬──────────────────────┬─────────────┘
                         │ ports (interfaces)   │
          ┌──────────────▼──────────┐  ┌────────▼──────────────────┐
          │  app/domain/            │  │  app/infrastructure/      │
          │  entités, erreurs, ports│  │  adaptateurs vers le      │
          │  (aucune dépendance)    │  │  legacy (absorbé sous app/)│
          └─────────────────────────┘  └────────┬──────────────────┘
                                                │
                              ┌─────────────────▼──────────────────┐
                              │  legacy absorbé sous app/ : agent/legacy (LLM, outils)│
                              │  app/infrastructure/persistence     │
                              │    (stores, versionnage, crypto)    │
                              │  app/infrastructure/ml (ex-src/ ML)  │
                              └────────────────────────────────────┘
```

### Règle d'or des dépendances

- `app/domain/**` ne dépend de **rien** (ni FastAPI, ni SQLite, ni LLM).
- `app/agent/**` et `app/application/**` ne dépendent que de `app/domain`.
- `app/infrastructure/**` implémente les ports en déléguant au legacy.
- `api/**` assemble et expose ; aucune logique métier.

### Surface d'entrée (API v1 + MCP)

- **MCP est la surface privilégiée** : `POST /mcp/sse` (transport streamable
  HTTP, JSON-RPC 2.0 ⇄ SSE) et `thinktuning-mcp` (stdio). Le serveur vit dans
  `app/infrastructure/mcp/` (bootstrap en lecture pour tout scope + tool
  `orchestrate` S6 — la boucle agentique complète).
- L'API HTTP applicative est montée sous `/api/v1`. Les modules legacy
  internes ne constituent pas un contrat public : ils servent uniquement de
  cibles de délégation et de compatibilité des tests.

---

## 2. La couche agentique

### Flux d'un run (`POST /api/v1/agent/ask/core` — idem via `POST /mcp/sse`, tools/call `orchestrate`)

1. **Intent** — validé à l'entrée (`app/domain/entities/plan.py`) : prompt,
   session, rôle, budget max_rounds.
2. **Plan** — le planner (LLM) propose un plan JSON ; parsing tolérant
   (`extract_plan`) : JSON direct, liste, fences markdown, prose autour.
   Une réponse **sans JSON est une réponse finale légitime**.
3. **Policy** — par action, `app/agent/policies/sandbox_policy.py` décide :
   - `AUTO_APPROVE` → exécution immédiate (lecture, réseau lisible) ;
   - `APPROVE` → validation humaine obligatoire (écriture, exécution) ;
   - `REJECT` → règle dure, jamais exécutée : SQL mutant (seuls
     `SELECT/WITH/EXPLAIN/PRAGMA` passent), POST vers hôte privé (anti-SSRF),
     chemins sensibles (`.git`, `.env`, clés privées) en écriture/suppression.
   - Anti-boucle : une même action rejetée deux fois (empreinte) arrête le run.
4. **Budget** — `app/agent/policies/budget.py` : rounds LLM et appels d'outils
   plafonnés (Settings `agent_max_llm_rounds` / `agent_max_tool_calls`).
5. **Action** — exécution via `ToolRegistryPort` ; erreur outil → renvoyée au
   LLM (auto-correction, jusqu'à épuisement du budget).
6. **Approbation** — action `APPROVE` sans gateway → `PENDING_APPROVAL` +
   demande persistée (`app/infrastructure/persistence/approval_store`) ; le client approuve via
   `POST /api/v1/agent/approvals/{id}/approve` puis relance avec
   `resume_request_id`. La reprise n'accorde que l'action dont l'empreinte
   SHA-256 des arguments correspond **exactement** à la demande approuvée.
7. **Mémoire** — short-term : fenêtre glissante sur la session
   (`app/agent/memory/short_term.py`, le premier message utilisateur est
   toujours réinjecté) ; long-term : résumés key/value (`long_term.py`).

### Statuts terminaux d'un run

| Statut | Signification |
|---|---|
| `completed` | réponse finale produite |
| `pending_approval` | action en attente de validation humaine |
| `rejected_loop` | le LLM reformule une action rejetée (anti-boucle) |
| `budget_exhausted` | plafond LLM/outils atteint sans réponse |
| `failed` | erreur non récupérable (LLM indisponible…) |

## 3. Arborescence

```
app/
├── config/settings.py            # Settings Pydantic — réglages INFRA uniquement
├── domain/
│   ├── entities/plan.py          # Intent, Plan, Action, ApprovalDecision…
│   ├── errors.py                 # hiérarchie typée (code stable + HTTP status)
│   └── ports/ports.py            # 6 Protocols : LLM, Registry, Stores…
├── agent/
│   ├── core.py                   # boucle AgentCore (ne lève jamais : statut)
│   ├── memory/                   # short_term.py, long_term.py
│   ├── policies/                 # budget.py, sandbox_policy.py
│   ├── settings.py               # AgentConfig — config agent (base IHM/Mongo)
│   └── factory.py                # composition root + flag AGENT_NEW_CORE
└── infrastructure/
    ├── legacy_registry.py        # registre historique encapsulé → ToolRegistryPort
    └── legacy_approval_store.py  # app/infrastructure/persistence/approval_store → ApprovalStorePort

backend/app/api/routes/v1/agent.py # POST /api/v1/agent/ask/core
                                  # @deprecated (tâche 20) : read-only MCP_FIRST
app/infrastructure/mcp/           # SURFACE MCP (S7) : serveur SSE (POST /mcp/sse),
                                  # tools/ (bootstrap, orchestrate), manifest, security
app/infrastructure/ml/           # ML (ex-src/) : dataset, model, inference — migré
```

### Les 7 ports (`app/domain/ports/ports.py`)

| Port | Legacy implémentant déjà le contrat |
|---|---|
| `LLMClientPort` | `app/infrastructure/llm/http_client.py` (retry + circuit breaker + streaming) |
| `ToolRegistryPort` | `app/infrastructure/tools/` (manifeste `tools_config.json`) |
| `SessionStorePort` | `app/infrastructure/persistence/session_store.py` (messages + mémoire long-term) |
| `AuditStorePort` | `app/infrastructure/persistence/audit_store.py` |
| `RunStorePort` | `app/infrastructure/persistence/run_store.py` |
| `ApprovalStorePort` | `app/infrastructure/persistence/approval_store.py` |
| `ContextPort` | `ia/agent/context.py` — wrapper `app/infrastructure/context/` |

Les tests de conformité (`tests/test_domain_ports.py`, `tests/test_context_port.py`)
vérifient que les classes legacy **satisfont les Protocols** : impossible de faire
dériver un contrat de l'implémentation sans casser un test.

**Bascule client LLM (`AGENT_LLM_V2`)** : deux implémentations derrière
`LLMClientPort` — `HttpLLMClient` (**défaut** depuis la bascule v2 en
production) et le client legacy en repli (`AGENT_LLM_V2=0`, tant que le
chemin v1 vit). `build_llm_client()` sélectionne via le flag sans changer les
use-cases (`tests/test_llm_v2.py`).

**Bascule contexte (`AGENT_CONTEXT`)** : `default_context_provider()` choisit le
wrapper legacy (comportement v1) ou `NullContextProvider` (profil `AGENT_CONTEXT=0`,
aucune I/O, aucune mutation de l'historique) — `tests/test_context_port.py`.

---

## 4. Configuration

**Deux sources, responsabilités séparées (SCRUM-138)** :

- **Infrastructure** : `app/config/settings.py` (Pydantic Settings, `.env`) —
  UNIQUEMENT `API_KEY`, `CORS_ALLOWED_ORIGINS`, `DASHBOARD_WS_TOKEN`,
  `PERSISTENCE_BACKEND` / `MONGODB_*`, `TRAIN_STREAM_STALL_MINUTES`,
  `MODEL_SANITY_MIN_CONFIDENCE`. AUCUNE configuration d'agent n'y réside.
- **Agent (module de configuration de l'IHM)** : `app/infrastructure/persistence/agent_settings.py`
  (store persistant — collection MongoDB `agent_settings` en mode
  `PERSISTENCE_BACKEND=mongodb`, SQLite sinon) + modèle typé
  `app/agent/settings.py` (`AgentConfig`, `get_agent_config()`). Toute la
  config agent (provider LLM, modèle, URLs, clés API, timeout/contexte,
  budgets, log level, surface MCP, feature flags) y vit et est
  entièrement stockée/chargée depuis la base — priorité décroissante :
  base (dashboard) → env `AGENT_*`/`MCP_*` (repli CI) → défauts du module.

- Fail-fast au chargement : `provider=openrouter` exige
  `OPENROUTER_API_KEY` ; `hf` exige `HF_API_KEY`/`HF_TOKEN`.
- Feature flags agent : `AGENT_<NOM>` = 1/true/yes/on
  (`reliability`, `audit`, `tool_analytics`, `context`, `copilot`,
  `websocket`, `multi_agent`) — même convention que `app/application/feature_flags.py`.
- Bascule du noyau : **v2 activé par défaut depuis la bascule en production** ;
  `AGENT_NEW_CORE=0` force le repli legacy (`/ask/core` répond alors 503).
- Bascule du client LLM : **`HttpLLMClient` activé par défaut** ;
  `AGENT_LLM_V2=0` force le repli de compatibilité encapsulé sous
  `app/agent/legacy/`.
- Surface MCP (S7) : **`MCP_FIRST=true`** gèle l'API HTTP legacy de l'agent en
  lecture seule (405 `mcp_first_read_only` ; approbations whitelistées) ;
  `MCP_SERVER_ENABLED` (défaut `true`) active/désactive le serveur MCP
  (`POST /mcp/sse` + stdio `thinktuning-mcp`).
- Pour les tests : `get_settings.cache_clear()` après modification de l'env.

---

## 5. Conventions de développement

### Ajouter un outil agent

1. Fonction dans `ia/tools/<domaine>_tools.py` ;
2. Enregistrement dans `TOOLS`
   (`app/infrastructure/tools/tools_config.json`) ;
3. Entrée déclarative dans `ia/tools/tools_config.json`
   (le test anti-divergence vérifie la cohérence) ;
4. Si l'outil est risqué : catégorie dans `classify_tool`
   (`app/agent/policies/sandbox_policy.py`) + règle éventuelle.

### Erreurs

Toute erreur métier hérite de `app/domain/errors.DomainError`
(`code` stable + `http_status` + `to_payload()`). Les erreurs agentiques
(`PlanRejectedError`, `SandboxViolationError`, `BudgetExceededError`…)
permettent au runner de distinguer retry / recovery / rejet.

### Tests

- Pyramid : unit (`tests/test_domain_*.py`), intégration API
  (`tests/test_api_v1_agent.py`), régression legacy conservée.
- **Zéro réseau, zéro SQLite** dans les tests nouveaux : fakes en mémoire
  vérifiés contre les Protocols ; monkeypatch de la factory.
- Les contrats verrouillent l'alignement legacy ↔ nouveau (ex.
  `test_decision_values_match_legacy`).

### Qualité

- `ruff check app/` doit passer (étendu progressivement au legacy) ;
- `mypy app/` progressif (cf. `pyproject.toml`) ;
- CI : `.github/workflows/ci.yml` (torch CPU → deps → ruff → mypy → pytest).

---

## 6. Migration restante (backlog)

Les éléments marqués **livrés** sont conservés ici pour la traçabilité; seuls
les points non livrés constituent le backlog actif.

### Livré

1. **Découplage des stores** — les stores et adaptateurs sont sous
   `backend/app/infrastructure/persistence/`; le stockage SQLite/MongoDB reste
   compatible avec les ports du domaine.
2. **Suppression des hacks d'import** — le garde-fou
   `backend/tests/test_sys_path_guard.py` protège l'absence d'injection
   `sys.path` dans l'application.
3. **API v1** — la surface HTTP publique est montée sous `/api/v1`, avec
   enveloppe d'erreur et contrats client/backend testés.
4. **Noyau agentique et transport LLM** — `AgentCore`, `HttpLLMClient`,
   `StubLLMClient` et `EventBusPort` sont câblés via des ports/adaptateurs et
   couverts par les tests ciblés.
5. **MCP** — SSE HTTP (`/mcp/sse`), stdio `thinktuning-mcp`, outils, ressources,
   sampling, auth et conformance sont couverts par les tests MCP.

### Backlog actif

1. **Réduire la compatibilité legacy** : supprimer progressivement les imports
   et adaptateurs encore nécessaires sous `backend/app/agent/legacy/`, après
   migration de leurs derniers consommateurs. Aucun nouveau code ne doit
   dépendre directement de ces modules.
2. **Finaliser la machine à états des runs** : faire de
   `backend/app/domain/entities/run.py` l'unique source de vérité des
   transitions persistées, notamment pour la reprise
   `awaiting_approval → running` avec empreinte validée.
3. **Étendre la qualité statique** : élargir progressivement `ruff` et `mypy`
   aux modules legacy et aux tests, sans réduire les garde-fous existants de
   la CI.
4. **Découpler la diffusion des événements d'entraînement** : conserver
   `TrainingEventsSource` comme seam et évaluer un bus push (Redis/NATS) si le
   polling MongoDB devient un goulot en multi-worker.
5. **Valider les environnements matériels** : garder les parcours GPU
   optionnels; `gpu_info`/`nvidia-smi` ne doivent jamais bloquer la CI Windows
   CPU.

### Règles de maintenance

- Toute migration doit mettre à jour le contrat `/api/v1`, l'OpenAPI généré,
  le client frontend et les tests associés dans le même changement.
- Les références aux anciens chemins doivent rester dans les documents
  explicitement historiques (`_migration/` ou sections d'historique), jamais
  dans les instructions d'utilisation courante.
- Un élément passe de **backlog actif** à **livré** uniquement après un test
  automatisé ou une preuve de déploiement vérifiable.
