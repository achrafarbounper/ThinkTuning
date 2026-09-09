# MCP Implementation Plan — Checklist Opérationnelle

> **Produit par** : MCP Product Council  
> **Date** : 2026-09-08  
> **Statut** : Ready for Engineering

---

## 🔄 Chronologie d’Exécution (7 semaines)

| Semaine | Sprint | Livrable | Owner | Tests |
|---|---|---|---|---|
| **S1** | MCP Bootstrap | `MCPServerLayer` (SSE + stdio) + `MCPVersion` | Backend Lead | `test_mcp_server.py` |
| **S2** | v0.1.0 Tools | 12 tools read-only exposés via MCP | Agent Lead | `test_mcp_manifest.py` |
| **S3** | v1.0.0 Beta | 25 tools + 5 resources + 2 prompts | Full MCP Team | `test_mcp_conformance.py` |
| **S4** | Security | Scopes + quotas + audit + client store | Security Officer | `test_mcp_security.py` |
| **S5** | v1.1.0 Resources | 10 resources + 3 prompts + sampling port | Agent Lead | `test_mcp_sampling.py` |
| **S6** | v2.0.0 Sampling | SamplingPort + orchestrate tool | ML Lead | `test_mcp_orchestrate.py` |
| **S7** | v3.0.0 MCP-First | HTTP API legacy + CI/CD MCP | Platform Lead | `test_mcp_first.py` |

---

## 🧩 Tâches par Semaine (S1 — Bootstrap)

### Tâche 1 : MCP Version (`MCPVersion`)
- [x] `app/domain/entities/mcp.py` → `MCPVersion(major.minor.patch)`
- [x] Lire depuis `pyproject.toml` → `[tool.mcp.version] = "0.1.0"`
- [x] Test : `test_mcp_version.py`

### Tâche 2 : MCP Server Layer (SSE + stdio)
- [x] `app/infrastructure/mcp/mcp_server_sse.py` — FastAPI SSE endpoint (`POST /mcp/sse`)
- [x] `app/infrastructure/mcp/mcp_server_stdio.py` — stdio entry point (`thinktuning-mcp`)
- [x] `app/infrastructure/mcp/mcp_server_factory.py` — build server with scope
- [x] Test : `test_mcp_server_basic.py` (ListTools, CallTool)

### Tâche 3 : MCP Domain Ports
- [x] `app/domain/ports/mcp_ports.py` :
  - `MCPToolRegistryPort` (interface de projection des tools)
  - `MCPResourceRegistryPort` (URI templates ↔ tools)
  - `MCPPromptRegistryPort` (templates MCP)
  - `SamplingPort` (reverse LLM inference)
- [x] Test : `test_mcp_ports_contract.py` (verify legacy implements the ports)

---

## 🧩 Tâches par Semaine (S2 — v0.1.0 Tools)

### Tâche 4 : Manifest Generator
- [x] `app/infrastructure/mcp/manifest_generator.py` :
  - Compile `tools_config.json` → MCP manifest
  - Mappe `thinktuning.tool/v1 safety` → MCP `annotations`
  - `to_json_schema()` déjà existant → reuse
- [x] `docs/mcp/MANIFEST.md` — catalogue produit généré
- [x] Test : `tests/test_mcp_manifest.py` (87 tests)

> **Livré (S2)** : compilation déterministe (57 tools, 26 read-only) —
> `from_meta_format` (standard v1) → `to_json_schema` (REUSE exact →
> `inputSchema`) → posture résolue `safety` déclarée > `classify_tool()`
> (Rec. 7/11) > fail-closed. Exceptions NETWORK (`http_post`, `call_api`) en
> posture mutation. `entry_to_mcp_tool` = couture vers `MCPTool` (tâche 6).
> Mode `strict` (gating CI) + warnings actionnables (tools non classés :
> `add`, `calc`, tools ML…). Anti-divergence : `MANIFEST.md` régénéré et
> commité, vérifié par `test_committed_catalog_is_in_sync`.


### Tâche 5 : Policy Adapter
- [x] `app/infrastructure/mcp/policy_adapter.py` :
  - `decide_action()` (sandbox_policy) → MCP annotations
  - `readOnlyHint` / `destructiveHint` / `idempotentHint`
  - Filter par scope (`visible_tools`)

> **Livré (S2)** : projection RUNTIME de la policy — `decide`/`decide_action`
> délèguent à `sandbox_policy.decide` (source de vérité unique, zéro règle
> dupliquée) et projettent le verdict via `decision_to_annotations`
> (AUTO_APPROVE → read-only ; APPROVE/REJECT → mutation fail-closed, le
> blocage porté par `PolicyVerdict.decision`, les hints restant des
> indications). `PolicyVerdict` porte les prédicats
> `allowed`/`requires_approval`/`blocked` + une raison auditée (`to_dict`
> prêt pour `ACT_MCP_TOOL_CALL`, tâche 12). Filtre de scope :
> `visible_tools()` (rôle `MCPScopeRole.granted` fail-closed + whitelist
> explicite pour `MCPSecurityScope` S4) ; deux providers du port
> `MCPToolRegistryPort` : `ScopeFilteredToolProvider` (projection sécurisée
> `tools/list`, tool invisible = tool absent) et `PolicyGateToolProvider`
> (gate `tools/call` : auto → exécution, approve → validation humaine,
> reject → refus — MCP n'est pas un bypass de la security interne). Test :
> `tests/test_mcp_policy_adapter.py`.

### Tâche 6 : 12 Tools Read-Only
- [x] Sélectionner : `add`, `calc`, `web_search`, `web_fetch`, `web_read`, `http_get`
- [x] `read_file`, `list_dir`, `find_file`, `file_info`, `file_checksum`, `head_file`, `count_lines`
- [x] Tous passent par `check_command_allowed` + `safe_resolve` + `enforce_host_policy`
- [x] Annotations : `readOnlyHint: true`, `idempotentHint: true`

> **Livré (S2)** : ``app/infrastructure/mcp/legacy_tool_provider.py`` — projection
> de la sélection read-only ``V010_READ_ONLY_TOOLS`` (les 13 tools nommés par la
> checklist ; le label « 12 » de la roadmap arrondissait le compte) du registre
> legacy ``ia/tools/tool_registry.py`` sur le port ``MCPToolRegistryPort``
> (tâche 3). REUSE total : ``compile_tool`` → ``entry_to_mcp_tool`` → handlers
> délégués aux implémentations legacy (``TOOLS[name](**args)``). Garanties de
> sécurité portées PAR DÉLÉGATION (zéro règle dupliquée) : ``safe_resolve``
> (fichiers), ``url_scheme_allowed`` + ``enforce_host_policy`` (réseau, anti-SSRF),
> AST whitelisté (``calc``) ; ``check_command_allowed`` n'a pas d'objet ici
> (``run_command``/``run_python`` sont ABSENTS de la sélection read-only).
> Fail-closed : posture mutation / nom inconnu / implémentation absente → tool
> EXCLU à la construction (warning tracé) ; à l'appel, args manquants ou
> exceptions legacy → ``ToolError`` (MCP ``isError``). ``add``/``calc`` :
> posture DÉCLARÉE ``safety: safe`` dans ``tools_config.json`` (lève le gap
> tâche 4). ``build_mcp_server()`` expose par défaut bootstrap S1 (2) + la
> sélection (13) = 15 tools. Test : ``tests/test_legacy_tool_provider.py``
> (40 tests) ; suite MCP complète : 289 passed.

---

## 🧩 Tâches par Semaine (S3 — v1.0.0 Beta)

### Tâche 7 : 25 Tools (extension read-only)
- [x] Ajouter 13 tools read-only supplémentaires :
  - `job_list`, `job_get`, `model_versions`, `dataset_stats`, `predict_sentiment`
  - `env_info`, `disk_usage`, `gpu_info`, `now`
  - `touch`, `file_info`, `count_lines`, `tail_file`
- [x] Chaque tool passe par `decide_action()` → `AUTO_APPROVE` (read-only)
- [x] Annotations cohérentes : `readOnlyHint: true`
- [x] Test : `test_mcp_tools_25.py` — vérifie les 25 tools + annotations

> **Livré (S3)** : ``V100_READ_ONLY_TOOLS`` (25 tools uniques) dans
> ``app/infrastructure/mcp/legacy_tool_provider.py`` — l'union des deux
> checklists (tâche 6 : 13 noms ; tâche 7 : 13 noms) donne 24 uniques
> (``file_info``/``count_lines`` déjà v0.1.0) ; ``touch`` est une ÉCRITURE
> (WRITE dur, ``sandbox_policy.classify_tool``) — JAMAIS exposée par la
> surface read-only, elle rejoindra la surface write/exec (tâche 17) ;
> deux lectures pures déjà classées READ (``read_json``, ``search_in_files``)
> complètent le compte produit « 25 » (roadmap v1.0.0 Public Beta). Les 5
> tools métier (``job_list``/``job_get``/``model_versions``/``dataset_stats``/
> ``predict_sentiment``), auparavant UNKNOWN → fail-closed (mutation + admin),
> ont reçu la déclaration ``safety: safe`` (tools_config.json, standard v1) →
> posture read-only DÉCLARÉE dans le manifeste compilé (``MANIFEST.md``
> régénéré : 33 read-only / 24 mutation, warnings 12 → 7). ``classify_tool`` :
> ``add``/``calc`` → READ (alignement runtime du design-time « safe » —
> ``decide_action()`` → ``AUTO_APPROVE`` pour les 25). ``build_mcp_server()``
> expose par défaut bootstrap S1 (2) + sélection v1.0.0 (25) = 27 tools ;
> ``build_v010_read_only_provider()`` reste disponible (13 tools,
> déploiements restreints). Tests : ``tests/test_mcp_tools_25.py`` (nouveau,
> 5) + ``test_legacy_tool_provider.py`` étendu (48) ; suite MCP complète :
> 307 passed.

### Tâche 8 : 5 Resources `thinktuning://`
- [x] `app/infrastructure/mcp/resources/resource_provider.py` :
  - `thinktuning://jobs` → `job_list()` → JSON
  - `thinktuning://jobs/{job_id}` → `job_get(job_id)` → JSON
  - `thinktuning://models` → `model_versions()` → JSON
  - `thinktuning://datasets/{path}/stats` → `dataset_stats(path)` → JSON
  - `thinktuning://config` → `agent_config()` → JSON
- [x] `MCPResource` entity : `uri`, `name`, `description`, `mimeType`
- [x] `ListResources` → retourne les 5 resources
- [x] `ReadResource(uri)` → résout l'URI → appelle le tool interne
- [x] Sécurité : `safe_resolve` pour les chemins, `query_only` pour SQL
- [x] Test : `test_mcp_resources.py` — `ListResources` + `ReadResource`

> **Livré (S3)** : ``LegacyResourceProvider`` (port ``MCPResourceRegistryPort``,
> tâche 3) dans ``app/infrastructure/mcp/resources/resource_provider.py`` —
> 3 resources statiques + 2 paramétrées (``jobs/{job_id}``,
> ``datasets/{path}/stats``), résolues par DÉLÉGATION aux tools internes
> read-only (``job_list``/``job_get``/``model_versions``/``dataset_stats``
> legacy + ``core.agent_cache.agent_config``) : zéro règle réimplémentée.
> Sécurité défense en profondeur : (1) parsing strict des URI par routes
> regex ancrées — traversée (``..``), backslash, caractères de contrôle,
> double-encodage (``%`` résiduel post-``unquote``) et segments vides refusés
> AVANT toute I/O ; (2) ``safe_resolve`` porté par délégation pour les chemins
> (2ᵉ ligne de défense) ; (3) SQLite ``mode=ro`` + ``PRAGMA query_only`` pour
> le SQL (jobs.db) ; (4) ``thinktuning://config`` masque les clés API
> (``has_*`` + ``*_masked``, convention dashboard) — JAMAIS en clair.
> Serveur : ``resources/list`` (5) + ``resources/read`` (contenu
> ``{uri, mimeType?, text}``, ``NotFoundError`` → JSON-RPC -32602) +
> capability ``resources`` annoncée à l'initialize ; ``build_mcp_server()``
> branche le registre par défaut (tools internes résolus paresseusement —
> construction sans I/O ni import lourd). Entité ``MCPResource`` (uri, name,
> description, mimeType) dans le domaine ; port ``list_resources()`` typé
> ``list[MCPResource]`` ; bonus ``list_resource_templates()`` (gabarits MCP,
> préparation tâche 13). Tests : ``tests/test_mcp_resources.py`` (38 :
> ListResources, ReadResource statique/paramétrée, 10 URIs malveillantes,
> intégration legacy réelle en sandbox tmp — jobs.db query_only, dataset CSV,
> évasion ``safe_resolve`` bloquée) ; ``test_mcp_ports_contract.py`` mis à
> jour (fake → ``MCPResource``) ; ``test_mcp_server_basic.py`` (surface v1.0.0 :
> 5 resources). Suite MCP complète : 340 passed.

### Tâche 9 : 2 Prompts MCP
- [x] `app/infrastructure/mcp/prompts/prompt_provider.py` :
  - `analyze-sentiment` → template : "Analyse le sentiment de ce texte: {text}"
  - `plan-training` → template : "Planifie un entraînement pour: {dataset}"
- [x] `MCPPrompt` entity : `name`, `description`, `arguments`
- [x] `ListPrompts` → retourne les 2 prompts
- [x] `GetPrompt(name, arguments)` → résout le template → retourne les messages
- [x] Test : `test_mcp_prompts.py` — `ListPrompts` + `GetPrompt`

> **Livré (S3)** : ``PromptProvider`` (port ``MCPPromptRegistryPort``,
> tâche 3) dans ``app/infrastructure/mcp/prompts/prompt_provider.py`` —
> 2 prompts résolus LOCALEMENT (aucune I/O, aucun tool, aucun LLM) :
> ``analyze-sentiment`` (argument ``text``) et ``plan-training`` (argument
> ``dataset``), requis. Entités du domaine réutilisées telles quelles
> (``MCPPromptTemplate`` / ``MCPPromptArgument`` / ``MCPPromptMessage``,
> tâche 3). Sécurité fail-closed : nom inconnu → ``NotFoundError``,
> argument requis manquant / valeur non-string → ``ValidationError`` ;
> templates possédés par le SERVEUR (les arguments du client ne sont que
> des VALEURS substituées — pas d'accès attribut, pas de ré-interpolation),
> arguments surnuméraires ignorés. Serveur : ``prompts/get`` ajouté au
> protocole et au dispatch ({description?, messages: [{role, content:
> {type: text, text}}]}), ``NotFoundError``/``ValidationError`` →
> ``Invalid params`` (-32602, jamais un crash), capability ``prompts``
> annoncée à l'``initialize`` ; ``build_mcp_server()`` branche le registre
> par défaut (``prompt_provider=...`` le remplace entièrement).
> Suite MCP complète : 356 passed.

---

## 🧩 Tâches par Semaine (S4 — Security)

### Tâche 10 : MCPSecurityScope + Client Store
- [x] `app/domain/ports/mcp_ports.py` → `MCPSecurityScope` (14 champs) :
  - `client_id`, `tenant_id`, `role`, `visible_tools`, `visible_resources`
  - `visible_prompts`, `sampling_enabled`, `rate_limit_per_minute`
  - `destructive_quota`, `revoked`, `revoked_at`, `revoked_reason`
- [x] `core/mcp_client_store.py` → `MCPClientStore` :
  - `register(client_id, secret, scope)` → crée un client
  - `revoke(client_id, reason)` → révoque un client
  - `list()` → liste les clients
  - `metrics(client_id)` → call_count, error_rate, scope_usage
- [x] Test : `test_mcp_client_store.py` — CRUD + révocation

### Tâche 11 : Scopes + Quotas + Rate Limiting
- [x] `app/infrastructure/mcp/security/scope_enforcer.py` :
  - `check_scope(client_id, tool_name)` → vérifie `visible_tools`
  - `check_quota(client_id, tool_name)` → vérifie `destructive_quota`
  - `check_rate_limit(client_id)` → vérifie `rate_limit_per_minute`
- [x] Intégrer avec `api/middlewares/rate_limit.py` existant
- [x] 4 rôles : `read_only` (12 tools), `contributor` (25 tools), `operator` (35 tools), `admin` (40 tools)
- [x] Test : `test_mcp_scope_enforcer.py` — chaque rôle + cas de dépassement

> **Livré (S4)** : `app/infrastructure/mcp/security/scope_enforcer.py` —
> `MCPScopeEnforcer` (scope/quota/rate limit composés, thread-safe, état en
> mémoire borné) + fonctions module `check_scope` / `check_quota` /
> `check_rate_limit` / `enforce`. Fail-closed : client inconnu, révoqué, rôle
> inconnu, tool hors whitelist/catalogue → `MCPAccessDeniedError` ; quota
> « manual approval » / heure (fenêtre glissante) → `MCPQuotaExceededError` ;
> débit per-client → `MCPRateLimitExceededError` (+ `retry_after`). 4 catalogues
> roadmap : read_only 12 (= V010 − file_checksum, label roadmap), contributor 25
> (= V100), operator 35 (= +10 write/exec tâche 17), admin 40 (= +5 tâche 19).
> **Intégration rate limit** : la primitive `TokenBucket` est déplacée dans
> `security/rate_limit_bucket.py` et ré-exportée par `api/middlewares/rate_limit.py`
> (même classe REST + MCP, zéro duplication ; l'enforceur n'importe jamais `api`).
> Résolution paresseuse du `MCPClientStore` (`default_scope_resolver`) : aucun
> import lourd, aucune base créée au module import. Test :
> `tests/test_mcp_scope_enforcer.py` — 29 tests (chaque rôle, whitelist vs
> catalogue, révoqué/inconnu/rôle inconnu, quota fenêtre + isolation + quota 0,
> rate limit burst + isolation + refill, partage de la primitive).

### Tâche 12 : Audit Trail MCP
- [x] `core/audit_store.py` → ajouter les events :
  - `ACT_MCP_TOOL_CALL = "mcp_tool_call"`
  - `ACT_MCP_RESOURCE_READ = "mcp_resource_read"`
  - `ACT_MCP_PROMPT_GET = "mcp_prompt_get"`
  - `ACT_MCP_SAMPLING = "mcp_sampling"`
  - `ACT_MCP_ORCHESTRATE = "mcp_orchestrate"`
- [x] Chaque appel MCP → `audit_log(ACT_MCP_*, subject=client_id, detail={...})`
- [x] Dashboard interne : métriques MCP (error rate, call volume, revoked clients)
- [x] Test : `test_mcp_audit.py` — vérifie que chaque call est auditée

> **Livré (S4)** : `core/audit_store.py` — les 5 actions normalisées MCP +
> regroupement `MCP_ACTIONS` (ordre stable d'agrégation) + `mcp_metrics()`
> (volume total, répartition par action, erreurs `is_error: true`, error rate —
> molécule stable, jamais de clé manquante). Infrastructure :
> `app/infrastructure/mcp/mcp_audit.py` — `audit_mcp_call()` NON BLOQUANT
> (un échec d'audit ne fait JAMAIS tomber un appel MCP, incident loggé),
> `actor="mcp"`, interrupteur `MCP_AUDIT_ENABLED` (défaut true, rollback
> explicite), import paresseux du store (aucune base créée à l'import).
> Serveur (`mcp_server.py`) : hook d'audit injecté à la construction,
> `_audit_method()` centralisé — `tools/call` tranche `mcp_tool_call` vs
> `mcp_orchestrate` par nom de tool, `detail` = méthode + tool/URI/prompt +
> arguments (anonymisés par `redact()` du store) + `is_error` + scope,
> `run_id` = id JSON-RPC ; les ÉCHECS sont audités comme les succès et les
> méthodes de catalogue/handshake (initialize, ping, lists) ne produisent
> aucune entrée. Transports SSE (`X-Client-Id` → subject, repli session id /
> anonymous) et stdio (anonymous) branchent le hook via
> `build_mcp_server(audit=...)`. Dashboard : `api/routes/mcp.py`
> (`GET /mcp/metrics` — call volume, error_rate MCP, clients total/active/
> revoked + détail par client trié par volume) délégué par la surface v1
> (`api/routes/v1/mcp.py`, protégée `require_api_key` — strangler, parité par
> construction). Test : `tests/test_mcp_audit.py` — 14 tests (chaque action
> auditée avec subject/client_id, échecs tracés `is_error`, handshake non
> audité, hook fautif non bloquant, interrupteur, métriques agrégées,
> dashboard 401 + volume/error rate/revoked). Suite MCP : 438 passed.

---

## 🧩 Tâches par Semaine (S5 — v1.1.0 Resources + Prompts)

### Tâche 13 : 10 Resources (extension)
- [x] Ajouter 5 resources supplémentaires :
  - `thinktuning://jobs/{job_id}/logs` → logs d'un job
  - `thinktuning://models/{version}/info` → métadonnées d'un modèle
  - `thinktuning://datasets/{path}/preview` → aperçu d'un dataset
  - `thinktuning://metrics/{job_id}` → métriques d'entraînement
  - `thinktuning://health` → santé du système
- [x] `resource_provider.py` → résout les URI dynamiques (regex/path params)
- [x] Test : `test_mcp_resources_10.py` — 10 resources + URI dynamiques

> **Livré (S5)** : ``LegacyResourceProvider`` étendu à 10 resources
> (4 statiques + 6 paramétrées) via table de routes ``_ROUTES`` — regex
> ancrées, ordre déterministe, validation des segments (anti-traversée
> ``..``/``.``/``''``, anti double-encodage, backslash, octets nul/contrôle,
> plafond 200 chars) AVANT toute I/O. Chaque nouvelle route DÉLÈGUE aux
> sources internes (zéro règle réimplémentée) : ``jobs/{job_id}/logs`` →
> existence via ``job_get`` + lignes via ``core.job_logs`` (même source
> mémoire que le WS ``/train/stream``) ; ``models/{version}/info`` →
> présence via ``model_versions`` + drapeau actif, artefacts +
> ``training_report.json``/``id2label.json`` lus sous racine sandbox
> revalidée ``safe_resolve`` ; ``datasets/{path}/preview`` → ``head_file``
> plafonnée à 50 lignes, même règle de format que ``dataset_stats``
> (``.env`` refusé AVANT lecture) ; ``metrics/{job_id}`` → existence via
> ``job_get`` puis SELECT miroir de ``core/job_store.py`` sur connexion
> ``mode=ro`` + ``PRAGMA query_only`` (ne crée JAMAIS la base) ;
> ``health`` → délégation exacte au use case hexagonal
> ``run_health_check`` + adaptateurs legacy par défaut (shape
> ``HealthSnapshot``, identique au /health legacy). Erreurs métier
> (``ValueError``/``OSError``) → ``NotFoundError`` fail-closed ;
> ``RuntimeError`` propage (serveur → ``Internal error``). Construction
> toujours sans I/O ni import lourd (résolveurs paresseux ;
> ``list_resources()`` = métadonnée pure). Correctif au passage :
> ``system_status_adapter`` — import circulaire réel (``api.middlewares``
> au niveau module → ``api/__init__`` → ``composition.bootstrap()``)
> devenu paresseux (cassait aussi le transport stdio en production).
> Tests : ``tests/test_mcp_resources_10.py`` (nouveau, 52 tests : fakes
> unitaires — contrat, routes, sécurité, gaps JSON-RPC ``resources/read``
> + templates — + intégration legacy réelle en sandbox tmp) ;
> ``test_mcp_resources.py`` + ``test_mcp_server_basic.py`` mis à jour
> (liste 5 → 10, garantie de sous-ensemble tâche 8).

### Tâche 14 : 3 Prompts (extension)
- [x] Ajouter 3 prompts supplémentaires :
  - `summarize-job` → "Résume le job {job_id}"
  - `compare-models` → "Compare les modèles {v1} et {v2}"
  - `explain-prediction` → "Explique la prédiction pour: {text}"
- [x] `prompt_provider.py` → 5 prompts totaux
- [x] Test : `test_mcp_prompts_5.py` — 5 prompts + arguments

> **Livré (S5)** : ``PromptProvider`` étendu à 5 prompts (2 tâche 9 +
> 3 tâche 14) — catalogue statique, ordre déterministe, résolution pure
> locale. Sécurité fail-closed inchangée : nom inconnu → ``NotFoundError``,
> argument requis manquant / valeur non-string → ``ValidationError``,
> surplus ignoré, valeurs non re-formatées. ``compare-models`` porte 2
> arguments requis (``v1``, ``v2``). ``build_mcp_server()`` branche les 5
> par défaut. Tests : ``tests/test_mcp_prompts_5.py`` (nouveau, 21 tests :
> contrat, catalogue, arguments, résolutions, sécurité, JSON-RPC
> ``prompts/list``/``prompts/get`` + capability) ; ``test_mcp_prompts.py``
> + ``test_mcp_server_basic.py`` mis à jour (garantie de sous-ensemble /
> préfixe tâche 9, miroir du pattern resources tâche 13).

### Tâche 15 : SamplingPort (reverse LLM)
- [x] `app/domain/ports/mcp_ports.py` → `SamplingPort` :
  - `create_message(messages, max_tokens)` → demande LLM inference au client
- [x] `app/infrastructure/mcp/sampling/sampling_adapter.py` :
  - Implémente `SamplingPort` via `HttpLLMClient` existant
  - `create_message()` → `llm.call(messages)` → retourne la complétion
- [x] `SamplingRequest` / `SamplingResponse` entities
- [x] Test : `test_mcp_sampling.py` — `create_message` + vérification de la réponse

> **Livré (S5)** : ``SamplingPort`` étendu en contrat canonique
> ``create_message(request: SamplingRequest) -> SamplingResponse``
> (``mcp_ports.py``) avec forme legacy ``create_message(messages,
> max_tokens)`` normalisée + commodité S1 ``create_text(...)`` conservée
> (rétrocompatible, via helper ``_sampling_create_text``). Entités pures
> ``SamplingRequest`` / ``SamplingResponse`` (``entities/mcp.py`` : Pydantic
> v2 frozen, validation fail-fast ``role``/``content``/``max_tokens``,
> ``effective_messages()`` préfixant ``system_prompt``,
> ``to_dict()`` → projection MCP ``createMessage``). Adaptateur
> ``app/infrastructure/mcp/sampling/sampling_adapter.py`` :
> ``SamplingAdapter(llm: LLMClientPort)`` — délégation stricte
> ``effective_messages() -> llm.call(messages) -> SamplingResponse``
> (``HttpLLMClient`` en prod via ``build_sampling_adapter()``,
> ``StubLLMClient`` en tests) ; réponse vide → ``LLMClientError``,
> erreur provider enveloppée (domaine) ; ``model`` repris de
> ``getattr(llm, "model", "")``. Tests :
> ``tests/test_mcp_sampling.py`` (19 tests : entités + ``create_message``
> canonique/legacy + vérification de la réponse + erreurs + fabrique) ;
> ``test_mcp_ports_contract.py`` mis à jour (fake S1 + ``create_message``).

---

## 🧩 Tâches par Semaine (S6 — v2.0.0 Sampling + Orchestrate)

### Tâche 16 : Tool MCP `orchestrate`
- [x] `app/infrastructure/mcp/tools/orchestrate_tool.py` :
  - `orchestrate(prompt, session_id, scope)` → wrap `AgentCore.run()`
  - Appelle `build_agent_core()` → `core.run(Intent(prompt=prompt))`
  - Retourne `AgentRunResult.answer` + traces
- [x] Sécurité : passe par `decide_action()` → `APPROVE` (mutation → validation humaine)
- [x] `orchestrate` est un tool MCP **distinct** des tools bruts
- [x] Test : `test_mcp_orchestrate.py` — `orchestrate("analyse ce dataset")` → réponse

> **Livré (S6)** : `orchestrate_tool.py` expose `orchestrate()` (wrap
> d'`AgentCore.run` via `build_agent_core`, `core_factory` injectable pour les
> tests) et `build_orchestrate_tool()` → `MCPTool` DISTINCT (annotations
> mutation, `required_scope=contributor`+), branché par `build_mcp_server`
> sur la surface **v2.0.0+** (`orchestrate_tool=` pour injection). Sécurité :
> chaque action du run passe par `sandbox_policy.decide_action()` —
> `APPROVE` (mutation) → run `pending_approval` surface par
> `awaiting_approval` (validation humaine), `REJECT` jamais exécuté.
> Audit : `tools/call orchestrate` → `ACT_MCP_ORCHESTRATE`. 13 tests :
> `test_mcp_orchestrate.py`.

### Tâche 17 : 35 Tools (extension avec write/exec)
- [x] Ajouter 10 tools avec `APPROVE` (write/exec filtré) :
  - `write_file`, `write_json`, `append_file`, `make_dir`, `copy_path`
  - `run_command`, `run_python`, `start_training`, `cancel_training`, `stop_training`
- [x] Chaque tool passe par `decide_action()` → `APPROVE` → validation humaine
- [x] Annotations : `destructiveHint: true`, `idempotentHint: false`
- [x] Test : `test_mcp_tools_35.py` — 35 tools + policy APPROVE

### Tâche 18 : v2.0.0 Breaking Change + Migration
- [x] `docs/mcp/migration/v1-to-v2.md` — guide de migration clients
- [x] Breaking change : `SamplingPort` ajouté → clients doivent mettre à jour
- [x] Changelog : `v2.0.0` — "Added SamplingPort + orchestrate tool"
- [x] Notification : email/Slack aux clients enregistrés
- [x] Test : `test_mcp_v2_conformance.py` — conformité v2

> **Livré (S6)** : guide de migration complet (`docs/mcp/migration/v1-to-v2.md`
> — résumé des changements, `sampling/create` avec options A/B, capacité
> `sampling` à `initialize`, checklist, compatibilité ascendante). Changelog
> `v2.0.0` publié (breaking changes + orchestrate + migration). Notifications
> email/Slack : `EmailNotifier` (SMTP, env `MCP_NOTIFICATION_SMTP_*`),
> `SlackNotifier` (webhook `MCP_NOTIFICATION_SLACK_WEBHOOK`),
> `NotificationService` (provider de clients injectable, ciblage
> client_id = adresse email, envoi non bloquant) —
> `app/infrastructure/mcp/notifications/` + singleton
> `get_mcp_client_store()` (core) + CLI `scripts/notify_mcp_v2_breaking_change.py`
> (`--dry-run` pour prévisualiser). Tests : `test_mcp_v2_conformance.py`
> (+ part2, 18 tests) + `test_mcp_notifications.py` (20 tests).
>
> **Fix suite manifeste (post-livraison)** : les 3 échecs pré-existants de
> `test_mcp_manifest.py` (`defaults_version_from_pyproject`,
> `markdown_header_contains_product_metadata`, `committed_catalog_is_in_sync`)
> résolus — attendus dérivés de la source de vérité (``load_mcp_version`` /
> manifeste compilé) et ``docs/mcp/MANIFEST.md`` régénéré (header ``v2.0.0``
> + reclassification task 17 : `cancel/start/stop_training` admin → operator,
> warnings 8 → 5). Suite ``test_mcp_*.py`` verte (89 tests manifeste).

---

## 🧩 Tâches par Semaine (S7 — v3.0.0 MCP-First)

### Tâche 19 : 40 Tools (full catalogue)
- [ ] Ajouter 5 tools supplémentaires :
  - `move_path`, `remove_path`, `split_file`, `dedupe_lines`, `unzip_file`
- [ ] 40 tools totaux — catalogue complet
- [ ] Tous les tools passent par `decide_action()` + `MCPSecurityScope`
- [ ] Test : `test_mcp_tools_40.py` — 40 tools + scopes

### Tâche 20 : HTTP API Legacy + MCP-First
- [ ] `api/routes/agent.py` → marquer `@deprecated` (HTTP API)
- [ ] Feature flag `MCP_FIRST=true` → HTTP API en mode read-only
- [x] Dashboard migre vers MCP-over-SSE (`POST /mcp/sse`) pour le mode
      d’orchestration de l’Assistant IA
- [ ] `ARCHITECTURE.md` → mettre à jour le diagramme (MCP = surface)
- [ ] Test : `test_mcp_first.py` — HTTP API legacy + MCP actif

### Tâche 21 : CI/CD MCP + Production Readiness
- [ ] `.github/workflows/ci.yml` → ajouter `test_mcp_*.py` dans la pipeline
- [ ] `docker-compose.yml` → service `thinktuning-mcp` (SSE + stdio)
- [ ] `render.yaml` → profil MCP pour le déploiement
- [ ] `docs/mcp/CLIENT_REGISTRY.md` — publier le registre des clients
- [ ] `docs/mcp/CHANGELOG.md` — v3.0.0 publié
- [ ] Test final : `test_mcp_production.py` — 40 tools + 15 resources + 8 prompts + sampling + orchestrate

---

## 🛡 Checklist de Validation MCP (CI)

Chaque PR MCP doit passer :

- [ ] `python -m pytest tests/test_mcp_*.py -q` — vert
- [ ] `python -c "from mcp import Client; c = Client(); print(c.list_tools())"` — 12+ tools
- [ ] `docs/mcp/CHANGELOG.md` — mis à jour
- [ ] RFC mergé dans `docs/mcp/rfc/` — si nouveau tool/resource/prompt
- [ ] `docs/mcp/MANIFEST.md` — régénéré et commité
- [ ] Audit trail vérifié (`ACT_MCP_*` events dans `agent_audit`)
- [ ] Scope/security testé (`contrib` blocked, `read_only` allowed)
- [ ] Versioning bumpé (SemVer)

---

## 🚨 Rollback Plan

Si MCP cause un incident critique :
- `MCP_SERVER_ENABLED=false` → désactive le serveur MCP
- Le Council est notifié automatiquement (audit trail)
- Les clients MCP reçoivent `503 Service Unavailable` + lien vers le changelog

---

## 📌 Lien vers le code existant

→ **Traçabilité complète** : voir `docs/mcp/MCP_IMPLEMENTATION_MAPPING.md`
→ **Architecture hexagonale** : voir `ARCHITECTURE.md`
→ **Standard thinktuning.tool/v1** : voir `docs/TOOL_STANDARD.md`
