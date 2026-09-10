# Architecture — ThinkTuning

> Backend ML d'analyse de sentiments FR/EN avec couche agentique
> (API FastAPI + agent **Intent → Plan → Action**).

Ce document décrit l'architecture cible en cours de mise en place
(architecture hexagonale), la coexistence avec le code historique
(`api/`, `core/`, `ia/`, `src/`), et les conventions à respecter
pour toute évolution.

---

## 1. Vue d'ensemble

```
                                          MCP (S7) = SURFACE D'ENTRÉE
                                          POST /mcp/sse (JSON-RPC 2.0 ⇄ SSE)
                                          + stdio `thinktuning-mcp`
                                                     │ tools/call orchestrate
                                                     ▼
  HTTP/WS/SSE ─▶ │  api/  (FastAPI) — adapters d'entrée (déprécié → MCP,
                 │   read-only sous MCP_FIRST=true, sauf approbations)
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
          │  (aucune dépendance)    │  │  legacy (ia/, core/)      │
          └─────────────────────────┘  └────────┬──────────────────┘
                                                │
                              ┌─────────────────▼──────────────────┐
                              │  legacy : ia/ (LLM, outils, sandbox)│
                              │  core/ (stores SQLite, scheduler)   │
                              │  src/ (ML : dataset, model, infer)  │
                              └────────────────────────────────────┘
```

### Règle d'or des dépendances

- `app/domain/**` ne dépend de **rien** (ni FastAPI, ni SQLite, ni LLM).
- `app/agent/**` et `app/application/**` ne dépendent que de `app/domain`.
- `app/infrastructure/**` implémente les ports en déléguant au legacy.
- `api/**` assemble et expose ; aucune logique métier.

### Surface d'entrée (S7 — MCP-First)

- **MCP est la surface privilégiée** : `POST /mcp/sse` (transport streamable
  HTTP, JSON-RPC 2.0 ⇄ SSE) et `thinktuning-mcp` (stdio). Le serveur vit dans
  `app/infrastructure/mcp/` (bootstrap en lecture pour tout scope + tool
  `orchestrate` S6 — la boucle agentique complète).
- L'**HTTP legacy** (`api/routes/agent.py`) est marqué `@deprecated` (tâche 20)
  et conservé uniquement comme adaptateur strangler des délégations v1 ; sous
  `MCP_FIRST=true` il répond 405 (`mcp_first_read_only`) sur toute mutation,
  sauf l'approbation humaine (`/approvals/*/approve|reject`) qui reste le
  canal whitelisté débloquant les runs MCP `pending_approval`.

---

## 2. La couche agentique

### Flux d'un run (`POST /api/agent/ask/core` — idem via `POST /mcp/sse`, tools/call `orchestrate`)

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
   demande persistée (`core/approval_store`) ; le client approuve via
   `POST /api/agent/approvals/{id}/approve` puis relance avec
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
    ├── legacy_registry.py        # ia/tools/tool_registry → ToolRegistryPort
    └── legacy_approval_store.py  # core/approval_store → ApprovalStorePort

api/routes/agent.py               # POST /ask/core (flag AGENT_NEW_CORE)
                                  # @deprecated (tâche 20) : read-only MCP_FIRST
app/infrastructure/mcp/           # SURFACE MCP (S7) : serveur SSE (POST /mcp/sse),
                                  # tools/ (bootstrap, orchestrate), manifest, security
core/ ia/ src/                    # legacy — migré progressivement
```

### Les 7 ports (`app/domain/ports/ports.py`)

| Port | Legacy implémentant déjà le contrat |
|---|---|
| `LLMClientPort` | `ia/agent/llm_client.py` (retry + circuit breaker + streaming) |
| `ToolRegistryPort` | `ia/tools/tool_registry.py` (manifeste `tools_config.json`) |
| `SessionStorePort` | `core/session_store.py` (messages + mémoire long-term) |
| `AuditStorePort` | `core/audit_store.py` |
| `RunStorePort` | `core/run_store.py` |
| `ApprovalStorePort` | `core/approval_store.py` |
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
- **Agent (module de configuration de l'IHM)** : `core/agent_settings.py`
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
  `websocket`, `multi_agent`) — même convention que `core/feature_flags.py`.
- Bascule du noyau : **v2 activé par défaut depuis la bascule en production** ;
  `AGENT_NEW_CORE=0` force le repli legacy (`/ask/core` répond alors 503).
- Bascule du client LLM : **`HttpLLMClient` activé par défaut** ;
  `AGENT_LLM_V2=0` force le repli legacy (ia/agent/llm_client.py).
- Surface MCP (S7) : **`MCP_FIRST=true`** gèle l'API HTTP legacy de l'agent en
  lecture seule (405 `mcp_first_read_only` ; approbations whitelistées) ;
  `MCP_SERVER_ENABLED` (défaut `true`) active/désactive le serveur MCP
  (`POST /mcp/sse` + stdio `thinktuning-mcp`).
- Pour les tests : `get_settings.cache_clear()` après modification de l'env.

---

## 5. Conventions de développement

### Ajouter un outil agent

1. Fonction dans `ia/tools/<domaine>_tools.py` ;
2. Enregistrement dans `TOOLS` (`ia/tools/tool_registry.py`) ;
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

1. ~~Migration physique des stores legacy vers `app/infrastructure/persistence/`
   (SQLAlchemy + Alembic pour le schéma SQLite)~~ **Annulé (décision projet)** :
   les stores restent dans `core/` ; les wrappers `app/infrastructure/persistence/`
   demeurent des délégations permanentes (ports + adaptateurs, sans remplacement
   physique du stockage).
2. ~~Suppression progressive des hacks `sys.path`~~ **FAIT (Phase 2)** :
   tous les imports passent par les paquets réels (`ia.agent.*`, `ia.tools.*`,
   `ia.copilot.*`, `ia.logging_setup`) — plus aucun insert `sys.path` dans
   `api/`, `core/`, `app/` ni les tests. Garde-fous CI :
   `tests/test_sys_path_guard.py` (statique AST + dynamique sous-processus).
3. Étendre ruff/mypy à `api/`, `core/`, `ia/`, `tests/`.
4. ~~Streaming SSE de `/ask/core` (événements tool_start/tool_result
   réutilisant l'event bus legacy)~~ **Port `EventBusPort` câblé sur le SSE** :
   le port pub/sub est en place (`app/infrastructure/events/`) et le flux
   `/ask/core/stream` s'y appuie — le noyau publie, la route s'abonne via un
   bus PAR RUN (`InMemoryEventBus`) ; see `tests/test_event_bus_wiring.py`.
5. Baseline GPU : `gpu_info` et `nvidia-smi` restent hors sandbox Windows CI.
6. **Machine à états du run (domaine)** : `RunStatus` déplacé de `app/agent/core.py`
   vers `app/domain/entities/run.py` (source de vérité unique, ré-exporté par le
   moteur pour rétro-compatibilité) ; `RunStateMachine` valide PUREMENT les
   transitions de la durée de vie persistée — dont l'invariant central : la
   reprise `awaiting_approval -> running` (empreinte validée) est la SEULE façon
   de relancer un run, jamais depuis un état terminal. `run_lifecycle.finish_run_status`
   passe par la FSM avant chaque `run_store.finish_run` (`tests/test_run_state_machine.py`).
7. ~~Décommission du chemin v1 HTTP/WS~~ **FAIT (bascule `AGENT_NEW_CORE` par
   défaut)** : routes `/ask` et `/ask/stream` supprimées (remplacées par
   `/ask/core` et `/ask/core/stream`), worker WebSocket legacy retiré (le canal
   `/ws` passe toujours par le noyau v2), use-case `run_legacy_ask` et ponts
   `ask_agent_decision` / `ask_agent_decision_streaming` supprimés ; tests
   portés sur le noyau v2 à contrat SSE/WS/sessions inchangé.
8. **Reste v1** : chat `/api/ai` (`core.agent_cache.ask_agent_detailed_streaming`),
   `/complete` + summarizer de session (v1 `AgentRunner`), coordinateur
   multi-agents (`MultiAgentCoordinator`) — tous construits sur
   `ia/agent/agent_core.py` + `ia/agent/llm_client.py` ; leur migration vers le
   client/noyau v2 conditionne la suppression de `ia/agent/llm_client.py`.
9. **MCP-First (S7 — tâche 20) FAIT** : surface MCP (`POST /mcp/sse`, JSON-RPC
   2.0 ⇄ SSE, serveur `app/infrastructure/mcp/mcp_server_sse.py`) ; module
   legacy `api/routes/agent.py` marqué `@deprecated` (DeprecationWarning à
   l'import + en-têtes `Deprecation`/`Sunset`/`Warning: 299` sur chaque
   réponse) ; feature flag **`MCP_FIRST=true`** → HTTP legacy read-only (405
   `mcp_first_read_only`, approbations humaines whitelistées) ; dashboard
   migré vers MCP-over-SSE (mode « MCP » du chat, `mcpClient.ts`) ;
   verrouillage par `tests/test_mcp_first.py` (HTTP + MCP actifs ensemble).

### Avancée Phase 3 (client LLM v2 + contexte)

- **Port `ContextPort` absorbé** : `ia/agent/context.py` → wrapper
  `app/infrastructure/context/legacy_context.py`, faux déterministe
  `null_context.py`, bascule `AGENT_CONTEXT` (`tests/test_context_port.py`).
- **`HttpLLMClient` (v2) implémenté** : client HTTP httpx propre derrière
  `LLMClientPort` — streaming NDJSON/SSE, payloads `ollama`/`openrouter`/`hf`,
  retry + circuit breaker réutilisés de `ia/agent/reliability.py` (classifieur
  d'erreurs httpx dans `errors.py`), thinking + réparation d'encodage.
  Bascule via `AGENT_LLM_V2` → `build_llm_client()` (`tests/test_llm_http_client.py`,
  transport `httpx.MockTransport` hors réseau). Le stub déterministe
  (`StubLLMClient`) reste disponible pour les tests de use-cases.
  **Bascule en production réalisée** : `HttpLLMClient` est l'implémentation
  par défaut (`flag_llm_v2=True`, repli legacy via `AGENT_LLM_V2=0` ;
  `tests/test_llm_v2.py`, `tests/test_agent_factory.py`).
  **Reste à faire** : décommissionner `ia/agent/llm_client.py` — il reste
  importé par le chemin v1 résiduel (`core/agent_cache.py` : coordinateur
  multi-agents + runners du chat `/api/ai`, cf. backlog item 8).
- **Port `EventBusPort` (8e) ajouté puis câblé sur le SSE** : contrat pub/sub
  aligné sur `ia/agent/event_bus` ; deux adaptateurs — `LegacyEventBus` (wrapper
  strangler vers le singleton, isolation d'erreurs préservée) et
  `InMemoryEventBus` (faux déterministe async, avec `history`) — dans
  `app/infrastructure/events/` (`tests/test_event_bus_port.py`).
  **Câblage** : le noyau (`AgentCore`) accepte un `EventBusPort` optionnel et
  publie son cycle de vie (`agent.run_start`, `agent.tool_start`/`tool_end`,
  `agent.thinking`, `agent.approval_pending`, `agent.run_finished`) via
  `_safe_emit` (défensif, jamais bloquant). `/ask/core/stream` injecte un bus
  PAR RUN (`InMemoryEventBus`) et s'abonne pour régénérer les frames SSE
  `core_tool` / `thinking_delta` à l'identique — aucun cross-talk entre flux
  concurrents, et la route n'est plus qu'un abonné
  (`tests/test_event_bus_wiring.py`). Les callbacks legacy `on_tool_event` /
  `on_thinking` restent pris en charge (compatibilité `/ask/core` et tests).
