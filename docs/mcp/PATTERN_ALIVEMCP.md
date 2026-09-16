# Patterns AliveMCP — état d'implémentation ThinkTuning

> **Statut (septembre 2026 — L4, SCRUM-155)** : ce document est le **tableau de
> bord de conformité** aux patterns AliveMCP. Il référence, pour chaque pattern,
> la **preuve** dans le code et le **test** qui la verrouille. Les statuts sont
> volontairement honnêtes : un pattern non implémenté est marqué comme tel.

## Comment lire ce document

| Colonne | Signification |
|---|---|
| `pattern` | Pattern AliveMCP (annexe en fin de document pour la description d'origine) |
| `statut` | `✅ implémenté` · `◐ partiel` (couverture réduite) · `⏳ roadmap` (non implémenté) |
| `preuve` | Fichier(s) de code portant le comportement — le contrat est **dans le code** |
| `test` | Test(s) qui échouent si la garantie disparaît (anti-régression) |

| Symbole | Sens |
|---|---|
| ✅ | Garanti par le code **et** verrouillé par un test |
| ◐ | Couvert partiellement : la mécanique existe mais une dimension manque (documentée) |
| ⏳ | Non implémenté : aucun code, aucun test — dette assumée |

---

## 1. Master table — pattern / statut / preuve / test

### 1.1 Agentic Patterns (5)

| pattern | statut | preuve | test |
|---|---|---|---|
| **Tool Discovery** | ✅ | `app/infrastructure/mcp/manifest_generator.py` (inputSchema compilé depuis `tools_config.json`), `app/infrastructure/mcp/policy_adapter.py` (`visible_tools()` + `decision_to_annotations` → `readOnlyHint` / `destructiveHint` / `idempotentHint`), `docs/mcp/MANIFEST.md` (catalogue généré) | `tests/test_mcp_manifest.py`, `tests/test_mcp_policy_adapter.py`, `tests/test_mcp_tools_25.py` |
| **Long-Running Tasks** | ✅ | `app/infrastructure/mcp/mcp_server_sse.py` (`_stream_orchestrate` : worker bloquant + pont SSE, heartbeat 10 s), `app/infrastructure/mcp/run_sweeper.py` (signal de santé « jobs actifs bloqués » : récolte des runs périmés) | `tests/test_mcp_stream_stability.py`, `tests/test_mcp_resilience.py` |
| **State Machines** | ✅ | `app/domain/ports/mcp_ports.py` (`MCPDurableRunState`, `_MCP_RUN_TRANSITIONS`, checkpoints monotones, lease), `app/application/mcp_orchestration.py` (transitions `pending → running → completed`/`partial_success`/`failed`/`cancelled`) | `tests/test_mcp_durable_run_store.py`, `tests/test_multi_agent_resume.py`, `tests/test_mcp_mongo_run_store.py` |
| **Human-in-the-Loop Approval Gates** | ✅ | `agent.worker.approval` (payload `request_id` + motif), état durable `awaiting_approval` **non terminal**, reprise ciblée `resume_request_id`, grâce d'approbation dédiée dans le sweeper | `tests/test_mcp_hitl_sse.py`, `tests/test_mcp_orchestrate.py` |
| **Guardrails** | ✅ | `app/infrastructure/mcp/policy_adapter.py` (`PolicyGateToolProvider` : auto/approve/reject), `app/infrastructure/mcp/security/` (scope enforcer, quotas, rate limit), auth transport fail-closed (`mcp_auth_required()` → défaut `True`) | `tests/test_mcp_scope_enforcer.py`, `tests/test_mcp_policy_adapter.py`, `tests/test_mcp_transport_auth.py` |


### 1.2 Production Resilience Patterns (6)

| pattern | statut | preuve | test |
|---|---|---|---|
| **Idempotency** | ✅ | `app/infrastructure/mcp/idempotency.py` : en-tête `Idempotency-Key` (prioritaire sur `params.arguments.idempotency_key`), verdicts `new` / `replay` / `inflight` / `conflict`, TTL distincts (in-flight court), éviction LRU bornée, empreinte **excluant la clé** | `tests/test_mcp_resilience.py` (§2, 13 tests) |
| **Backpressure** | ✅ | `app/infrastructure/mcp/backpressure.py` : `BoundedSemaphore` en acquisition **non bloquante**, plafonds global + par client, quota d'ouverture via `TokenBucket`, `CapacityRejection` → `503`/`429` + `Retry-After` + `error.code` | `tests/test_mcp_resilience.py` (§1, 6 tests) |
| **Schema Evolution** | ◐ | `MANIFEST.md` **généré** et vérifié en synchronisation (anti-divergence), version SemVer `[tool.mcp].version` (`backend/pyproject.toml` = `2.3.0`), changements additifs documentés dans `CHANGELOG.md`. **Manque** : dual-accept automatique et règle « suppression après 30 jours sans appels » | `tests/test_mcp_manifest.py` (`test_committed_catalog_is_in_sync`), `tests/test_mcp_version.py` |
| **Canary Deployment** | ◐ | **Aucun canary de déploiement.** Substitut : **feature gate par version** — `mcp_server_factory._bootstrap_tools` n'expose `orchestrate` qu'à partir de `MCPVersion(2, 0, 0)` et l'extension admin qu'à partir de `2.2.0` | `tests/test_mcp_version.py` (`test_ordering_matches_roadmap_milestones`) |
| **Graceful Degradation** | ✅ | `app/infrastructure/mcp/mcp_events.py` (`build_meta` / `degraded_meta` : `_meta.degraded` **toujours** présent + `reason` + `failure_phase`), `orchestrate.degraded` persisté, repli synthèse sans bulle vide, repli mono-agent explicite (`orchestration_fallback`) | `tests/test_mcp_resilience.py` (§3/§4), `tests/test_mcp_stream_stability.py` |
| **Request Batching** | ✅ | `app/application/dynamic_batcher.py` (regroupement des prédictions), consommé par `app/api/routes/predict.py` | `tests/test_async_batching.py` |

### 1.3 Multi-Agent Orchestration Patterns (5)

| pattern | statut | preuve | test |
|---|---|---|---|
| **Topology** (orchestrator-dispatcher) | ✅ | Lead/Worker via `MultiAgentMCPAdapter` + `MultiAgentOrchestratorPort` ; serveur **stateless per-call** (l'état vit dans le store durable, pas en RAM) ; dispatch parallèle contrôlé par `parallel` | `tests/test_multi_agent.py`, `tests/test_multi_agent_usecase.py` |
| **Shared State** | ✅ | Store durable **source de vérité** (SQLite/Mongo), `version` optimiste incrémenté à chaque transition, **exclusivité de lease** (`acquire_lease` refuse un second propriétaire) | `tests/test_mcp_durable_run_store.py` (`test_durable_run_store_allows_only_one_concurrent_owner`, `test_durable_run_store_renews_only_for_current_owner`) |
| **Tool Composition** | ✅ | Pipeline serveur typé (workers) avec erreurs partielles remontées en `worker_errors` + `failure_phase`, sans interrompre les autres workers | `tests/test_multi_agent_tools.py` |
| **Agent Handoffs** | ✅ | Enveloppe normale d'événement (`event_id`, `timestamp`, `parent_task_id`, `worker_id`, `phase`, `sequence`) + checkpoint durable + **clé d'idempotence** (déduplication d'un handoff rejoué) | `tests/test_mcp_resilience.py` (§2), `tests/test_mcp_durable_run_store.py` |
| **Conversation Context** | ✅ | Store de sessions (`sessions`) + mémoire court terme / long terme ; fenêtre glissante + résumé ; aucun contexte critique en mémoire de processus | `tests/test_agent_memory.py`, `tests/test_agent_context.py` |

### 1.4 Synthèse

| Domaine | ✅ | ◐ | ⏳ | Total |
|---|---|---|---|---|
| Agentic | 5 | 0 | 0 | 5 |
| Production Resilience | 4 | 2 | 0 | 6 |
| Multi-Agent Orchestration | 5 | 0 | 0 | 5 |
| **Total** | **14** | **2** | **0** | **16** |

Les deux patterns `◐` sont explicitement documentés : **Schema Evolution**
(absence de dual-accept automatique) et **Canary Deployment** (remplacé par un
feature gate de version).

---

## 2. Fenêtres de compatibilité

Une « fenêtre de compatibilité » est la durée pendant laquelle une surface
**dépréciée reste fonctionnelle**. Toute fenêtre ouverte est un engagement : la
surface ne peut pas disparaître sans bump majeur + RFC (`MCP_GOVERNANCE.md`).

| Surface / comportement | Statut | Depuis | Fenêtre | Fin planifiée | Contournement recommandé |
|---|---|---|---|---|---|
| `orchestrate.done` (canal de fin) | Déprécié | L2 (SCRUM-153) | **Ouverte, non bornée** | Aucune date | Lire `event: message` / `result` JSON-RPC ; `agent.done` pour le payload métier |
| Noms d'événements legacy `agent.*` | Maintenus | Antérieur à L1 | **Ouverte, non bornée** | Aucune date | Aucun requis : restent la source de vérité de l'adaptateur multi-agent (`SSE_DEFAULT_EVENT_KINDS`) |
| HTTP legacy `/api/agent` (MCP-First) | Déprécié | v3.0.0 roadmap (S7) | **Bornée** | `Sat, 31 Dec 2026 23:59:59 GMT` (en-tête `Sunset`) | `POST /mcp/sse` (`tools/call orchestrate`) |
| `MCP_FIRST=true` (gel lecture seule du legacy) | Actif | S7 | Tant que le flag est posé | — | Rollback : retirer le flag |
| MCP `v1.x` → `v2.0.0` (sampling) | **Breaking** | `docs/mcp/migration/v1-to-v2.md` | Fenêtre de migration ouverte | Aucune date | Ignorer `capabilities.sampling`, ou implémenter `sampling/create` |
| Alias de tools (`predict_sentiment` → `analyze_sentiment`) | Déprécié | `MCP_GOVERNANCE.md` | Ouverte | Aucune date | Utiliser le nom canonique retourné par `tools/list` |
| `orchestrate` (tool) | Actif | `MCPVersion >= 2.0.0` | — | — | — |
| 5 tools admin (extension) | Actif | `MCPVersion >= 2.2.0` | — | — | Absent de la surface si version < 2.2.0 |
| Paramètre `stream` omis | Compatible | Antérieur à L1 | — | — | `enable_thinking: true` force aussi le streaming |

**Règle de lecture** : une fenêtre « ouverte, non bornée » signifie qu'un retrait
exigerait une décision de gouvernance (RFC + bump majeur) — ce n'est **pas** une
incitation à s'y appuyer pour du nouveau code.

### Durcissement du transport (septembre 2026)

| Comportement | Avant | Maintenant | Compatibilité |
|---|---|---|---|
| Paramètres JSON-RPC non-objet | Interprétés comme vides | `-32602` explicite | **Breaking** (corrige un contrat laxiste) |
| Timeout client `orchestrate` | Absolu 120 s (coupait un run long) | **Inactivité** 120 s, réarmée à chaque chunk et heartbeat | Amélioration stricte |
| Fin de flux | `[DONE]` si succès | `[DONE]` **toujours** (erreur comprise) | Durcissement |
| Événements nommés + `event: message` | `message` seul | Les deux (clients génériques intacts) | Additif |

---

## 3. Dépréciation de `orchestrate.done`

`orchestrate.done` reste **émis** et **terminal** (jamais filtré), mais son rôle
de **canal de fin normatif** est déprécié.

| Aspect | Contrat |
|---|---|
| Émission | Toujours émise en fin de run (jamais supprimée) |
| Filtrage | Jamais filtrée — membre de `TERMINAL_EVENT_KINDS` (`app/infrastructure/mcp/mcp_events.py`) |
| Rôle actuel | Alias transport : porte le résultat JSON-RPC sérialisé |
| Rôle cible | `agent.done` (payload métier) + `event: message` (réponse JSON-RPC) |
| Impact client | Un client qui parse `orchestrate.done` continue de fonctionner ; un client qui dépend de son **contenu** au lieu de `event: message` s'expose à un retrait futur |
| Migration | Lire le `result` JSON-RPC de `event: message` ; ne jamais décider du statut d'un run à partir du seul `orchestrate.done` |
| Retrait | Aucune date ; exigerait un bump majeur de `[tool.mcp].version` + RFC acceptée |
| Test de verrouillage | `tests/test_mcp_resilience.py::test_terminal_events_are_never_filtered` |

### Exemple de migration client

```typescript
// AVANT — dépend du contenu de orchestrate.done (canal déprécié)
if (event.event === 'orchestrate.done') {
  answer = JSON.parse(event.data).result?.content?.[0]?.text
}

// APRÈS — contrat cible : message (JSON-RPC) + variante discriminée `rpc`
if (event.event === 'message') {
  const rpc = JSON.parse(event.data) as JsonRpcResponse
  answer = (rpc.result as McpToolCallResult).content?.find(b => b.type === 'text')?.text
}
```

Côté frontend ThinkTuning, c'est déjà ce que fait `orchestrateViaMcpStream`
(`frontend/src/api/mcpClient.ts`) : les enveloppes `message` /
`orchestrate.done` / `orchestrate.error` sont capturées comme réponse du tool et
relayées via la variante `rpc` de l'union discriminée `McpOrchestrateEvent`.

---

## 4. Évolution du manifeste

Le manifeste MCP est **généré**, jamais écrit à la main — c'est la garantie
anti-divergence entre la surface exposée et le catalogue publié.

### Pipeline

```
backend/app/infrastructure/tools/tools_config.json     (standard thinktuning.tool/v1)
        │
        │  manifest_generator.compile_tool()      → inputSchema (REUSE to_json_schema)
        │  manifest_generator.build_manifest()    → tri déterministe + comptage + warnings
        ▼
docs/mcp/MANIFEST.md                                   (FICHIER GÉNÉRÉ, committé)
```

Régénération :

```bash
cd backend
PYTHONIOENCODING=utf-8 python -m app.infrastructure.mcp.manifest_generator
```

### Version de surface

| Élément | Emplacement | Rôle |
|---|---|---|
| `[tool.mcp].version` | `backend/pyproject.toml` | **Source unique** de la version de surface (indépendante de la version du package) |
| `MCPVersion` | `app/domain/entities/mcp.py` | Value object ordonnable/hashable, parsé depuis TOML |
| `DEFAULT_MCP_VERSION` | `app/domain/entities/mcp.py` = `2.3.0` | Repli si `pyproject.toml` absent (image Docker allégée) |
| `load_mcp_version` | `app/infrastructure/mcp/version_loader.py` | Seule I/O du versioning (hexagonale) ; mode `strict=True` pour la CI |

**Tolérance** : sans `pyproject.toml`, le serveur démarre quand même (warning +
`DEFAULT_MCP_VERSION`) — un manifeste ne doit jamais être une condition de
démarrage. `strict=True` propage l'erreur (tests de contrat, CI).

### Jalons de version

| Version | Contenu de surface | Gate dans le code |
|---|---|---|
| `0.1.0` | Bootstrap : `mcp_version`, `server_info` | Base |
| `2.0.0` | Tool `orchestrate` distinct des tools bruts + capacité `sampling` | `version >= MCPVersion(2, 0, 0)` |
| `2.2.0` | Extension admin (5 tools : `dedupe_lines`, `unzip_file`, …) | `version >= MCPVersion(2, 2, 0)` |

### État publié (`docs/mcp/MANIFEST.md`)

| Attribut | Valeur |
|---|---|
| Serveur | `thinktuning-mcp` |
| Version surface | `2.3.0` |
| Protocole MCP | `2025-06-18` |
| Tools | 62 (37 read-only · 25 mutation) |
| Avertissements | **5** — 3 tools non classés par la policy legacy (`find_duplicates`, `run_shell`, `train_model`, posture fail-closed), 1 description manquante (`add`), 1 posture `dangerous` (`git_commit`, jamais exécuté). Le mode `strict` transforme ces avertissements en échec (gating CI). |

### Verrouillages

| Garantie | Test |
|---|---|
| Le catalogue committé est **synchronisé** avec le code | `tests/test_mcp_manifest.py::test_committed_catalog_is_in_sync` |
| Le manifeste **couvre** tous les tools déclarés | `test_real_manifest_covers_all_declared_tools` |
| Sortie **déterministe** (ordre stable, comptage cohérent) | `test_build_manifest_deterministic_output`, `test_real_manifest_tool_count_consistent` |
| Le mode `strict` **échoue** sur une entrée non conforme | `test_build_manifest_strict_raises_on_non_conform_definition` |
| Version de surface cohérente avec le manifeste | `tests/test_mcp_version.py::test_mcp_release_version_is_consistent_with_manifest` |

**Règle de contribution** : tout changement de `tools_config.json` **doit**
régénérer `MANIFEST.md` dans le même commit — sinon la CI échoue.

---

## Annexe — synthèse AliveMCP d'origine (référence)

> Le contenu ci-dessous est la **note de veille** d'origine, conservée comme
> référence des patterns. C'est la description théorique ; l'état réel
> d'implémentation est celui du §1.

Je synthétise ici les 3 grands ensembles de patterns trouvés dans les sources AliveMCP :


5 Agentic Patterns (serveurs MCP pour agents autonomes) 

6 Production Resilience Patterns (serveurs MCP en production) 

5 Multi‑Agent Orchestration Patterns (serveurs MCP multi‑agents) 

🧩 1) Les 5 Agentic Patterns (AliveMCP)
1. Tool Discovery
Empêche les agents de choisir le mauvais tool.

Schémas explicites

Paramètres typés

Tests de sélection

Health signal : tool selection accuracy 

2. Long‑Running Tasks
Pour les jobs async qui ne doivent pas “stall” silencieusement.

Queue + worker

Polling robuste

Health signal : stuck active jobs 

3. State Machines
Pour les workflows multi‑étapes qui doivent survivre aux interruptions.

États persistés

Reprise après crash

Health signal : non‑terminal states idle > 1h 

4. Human‑in‑the‑Loop Approval Gates
Pour les actions destructives.

Slack / webhook d’approbation

Timeout + retry

Health signal : stale pending approvals 

5. Guardrails
Sécurité contre prompt injection, SSRF, PII leaks.

Validation des arguments

Filtrage des URLs

Health signal : security rejection rate 

🏭 2) Les 6 Production Resilience Patterns
1. Idempotency
Empêche les effets doublons lors des retries.

Idempotency-Key

Cache Redis (1h / 24h / 7j selon type) 

2. Backpressure
Empêche la saturation du serveur.

Semaphore / BoundedSemaphore

HTTP 503 + Retry-After

Limites globales + par client 

3. Schema Evolution
Évite de casser les agents qui ont mis en cache le schema.

Changements uniquement additifs

Dual-accept pour les breaking changes

Suppression après 30 jours sans appels 

4. Canary Deployment
Déploiement progressif et rollback automatique.

5% → 25% → 50% → 100%

Rollback si erreur > 2× stable pendant 5 min 

5. Graceful Degradation
Retourne des résultats partiels plutôt que d’échouer.

5 niveaux : full → stale → partial → IDs → informative error

_meta.degraded pour informer l’agent 

6. Request Batching
Réduit les N+1 queries.

DataLoader

Batch DB queries concurrentes 

🤖 3) Les 5 Multi‑Agent Orchestration Patterns
1. Topology
Deux modèles :

Orchestrator‑dispatcher (hiérarchique, DAG clair)

Swarm (peer‑to‑peer, auto‑organisation)
Serveur MCP doit être stateless per-call. 

2. Shared State
Empêche la corruption de données en écriture parallèle.

Pas de state en mémoire Node.js

SQLite WAL ou Redis + Lua CAS

Versioning optimiste sur chaque record 

3. Tool Composition
Déplace les pipelines multi‑étapes côté serveur.

Pipeline typé

StepError avec contexte partiel

Promise.allSettled pour map‑reduce 

4. Agent Handoffs
Transfert propre entre agents.

HandoffEnvelope (session, token, contexte, next-tool)

Checkpoint durable

Déduplication via idempotency token 

5. Conversation Context
Contexte persistant hors mémoire.

Redis pour multi‑instance

Sliding window + summarization

Tool context.clear 

📌 Synthèse ultra‑pratique
Domaine	Patterns clés	Objectif
Agentic	Tool discovery, long tasks, state machines, approvals, guardrails	Agents fiables et sûrs
Production	Idempotency, backpressure, schema evolution, canary, degradation, batching	Résilience et scalabilité
Multi‑agent	Topology, shared state, composition, handoffs, context	Orchestration parallèle robuste