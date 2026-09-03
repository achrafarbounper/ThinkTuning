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
                 ┌────────────────────────────────────────────┐
  HTTP/WS/SSE ─▶ │  api/  (FastAPI) — adapters d'entrée       │
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

---

## 2. La couche agentique

### Flux d'un run (`POST /api/agent/ask/core`)

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
├── config/settings.py            # Settings Pydantic — SOURCE UNIQUE de config
├── domain/
│   ├── entities/plan.py          # Intent, Plan, Action, ApprovalDecision…
│   ├── errors.py                 # hiérarchie typée (code stable + HTTP status)
│   └── ports/ports.py            # 6 Protocols : LLM, Registry, Stores…
├── agent/
│   ├── core.py                   # boucle AgentCore (ne lève jamais : statut)
│   ├── memory/                   # short_term.py, long_term.py
│   ├── policies/                 # budget.py, sandbox_policy.py
│   └── factory.py                # composition root + flag AGENT_NEW_CORE
└── infrastructure/
    ├── legacy_registry.py        # ia/tools/tool_registry → ToolRegistryPort
    └── legacy_approval_store.py  # core/approval_store → ApprovalStorePort

api/routes/agent.py               # POST /ask/core (flag AGENT_NEW_CORE)
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
`LLMClientPort` — le client legacy (défaut) et `app/infrastructure/llm/`
(stub déterministe aujourd'hui, futur client HTTP propre). `build_llm_client()`
sélectionne via le flag sans changer les use-cases (`tests/test_llm_v2.py`).

**Bascule contexte (`AGENT_CONTEXT`)** : `default_context_provider()` choisit le
wrapper legacy (comportement v1) ou `NullContextProvider` (profil `AGENT_CONTEXT=0`,
aucune I/O, aucune mutation de l'historique) — `tests/test_context_port.py`.

---

## 4. Configuration

**Source unique : `app/config/settings.py`** (Pydantic Settings, `.env`).

- Fail-fast au chargement : `AGENT_PROVIDER=openrouter` exige
  `OPENROUTER_API_KEY` ; `hf` exige `HF_API_KEY`/`HF_TOKEN`.
- Feature flags agent : `AGENT_<NOM>` = 1/true/yes/on
  (`reliability`, `audit`, `tool_analytics`, `context`, `copilot`,
  `websocket`, `multi_agent`) — même convention que `core/feature_flags.py`.
- Bascule du noyau : **`AGENT_NEW_CORE=1`** active `/ask/core` (503 sinon).
- Bascule du client LLM : **`AGENT_LLM_V2=1`** utilise la 2e implémentation de
  `LLMClientPort` (désactivé par défaut).
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
  (`tests/test_api_ask_core.py`), régression legacy conservée.
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

1. Migration physique des stores legacy vers `app/infrastructure/persistence/`
   (SQLAlchemy + Alembic pour le schéma SQLite).
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
  **Reste à faire** : basculer `/ask/core` en production sur l'implémentation v2
  puis décommissionner `ia/agent/llm_client.py` (le vrai client HTTP propre est
  en place ; il s'agit désormais d'un choix de flag et de la suppression du
  legacy).
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
