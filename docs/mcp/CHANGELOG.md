# MCP Changelog — ThinkTuning

> **Audit-trail** de la surface MCP (versionnée via `docs/mcp.version`).
> Chaque entrée documente le livrable S1/S2/…, les fichiers touchés et les
> garanties de migration (SemVer : breaking changes → major bump).

---

## v2.0.0 — 2026-09-09 (S6 — SamplingPort + Orchestrate, tâche 18)

> **⚠️ BREAKING CHANGE** — Cette version introduit `SamplingPort` et le tool
> `orchestrate`. Les clients MCP doivent mettre à jour leur gestion des
> capacités (voir `docs/mcp/migration/v1-to-v2.md`).

### Ajouts — SamplingPort (tâche 15, S6 v2.0.0)
- **Port domaine** : `SamplingPort` (`app/domain/ports/mcp_ports.py`) —
  reverse LLM inference (``sampling/create``) : le serveur MCP agit comme
  CLIENT de son propre LLM sur demande d'un client MCP. Deux niveaux :
  ``create_message(request)`` (canonique, ``SamplingRequest`` →
  ``SamplingResponse``) et ``create_text(...)`` (commodité, ``str`` direct).
- **Adaptateur** : `SamplingAdapter` (`app/infrastructure/mcp/sampling/`)
  — implémente `SamplingPort` via le `LLMClientPort` existant
  (``llm.call(messages)``), ``SamplingRequest``/``SamplingResponse``
  (entités ``app/domain/entities/mcp.py``).
- **Serveur** : `MCPServer._handle_sampling_create()` — dispatch JSON-RPC
  ``sampling/create`` (validation des paramètres, délégation au port,
  erreurs LLM → ``-32603 INTERNAL_ERROR``) ; capacité ``sampling`` annoncée
  à ``initialize`` quand le port est branché (fail-closed : sans port, la
  méthode est rejetée).
- **Fabrique** : `build_mcp_server(sampling_port=...)` — ``None`` →
  ``build_sampling_adapter()`` pour v2.0.0+ (sampling activé par défaut),
  ``None`` pour < v2.0.0 (pas de capacité sampling).

### Ajouts — Tool `orchestrate` (tâche 16, S6 v2.0.0)
- **Tool** : `orchestrate` (``app/infrastructure/mcp/tools/orchestrate_tool.py``)
  — orchestration agentique complète (``AgentCore.run`` via
  ``build_agent_core``), tool MCP DISTINCT des tools bruts, exposé sur la
  surface v2.0.0+ par ``build_mcp_server()``.

### Ajouts — Notifications email/Slack aux clients (tâche 18, S6 v2.0.0)
- **Service** : `NotificationService`
  (``app/infrastructure/mcp/notifications/notification_service.py``) —
  orchestrateur de diffusion des breaking changes aux clients MCP inscrits
  (``MCPClientStore.list()``) : compose le message (texte brut + HTML +
  blocks Slack) et l'envoie via les canaux configurés. Envoi **non bloquant**
  (échec loggé, jamais propagé) ; provider de clients injectable pour les
  tests, fallback singleton ``core.mcp_client_store.get_mcp_client_store()``.
- **Canaux** : `EmailNotifier` (SMTP — env
  ``MCP_NOTIFICATION_SMTP_HOST/PORT/USER/PASSWORD``, ``MCP_NOTIFICATION_FROM``)
  ciblant chaque client dont le ``client_id`` est une adresse email ;
  `SlackNotifier` (webhook — env ``MCP_NOTIFICATION_SLACK_WEBHOOK``), un seul
  message par diffusion. Canaux non configurés = désactivés (log info).
- **CLI** : ``scripts/notify_mcp_v2_breaking_change.py`` — déclenche la
  notification v2.0.0 (sujet, breaking changes, guide de migration) ;
  ``--dry-run`` prévisualise les clients ciblés et le message sans envoi.

### Breaking Changes
- **`SamplingPort` ajouté** : nouvelle capacité ``sampling/create`` — les
  clients doivent gérer la nouvelle méthode JSON-RPC (ou l'ignorer).
- **Capacité `sampling` annoncée** : `initialize` retourne
  ``capabilities.sampling: {}`` — les clients qui valident strictement les
  capacités doivent accepter cette clé.
- **Version bump** : `0.1.0` → `2.0.0` (SemVer : breaking changes → major
  bump) ; `DEFAULT_MCP_VERSION` = `2.0.0` (fallback si `pyproject.toml`
  absent).

### Tests (tâche 18)
- `tests/test_mcp_v2_conformance.py` + `..._part2.py` (nouveaux, 18 tests) :
  conformité v2.0.0 (``initialize`` annonce ``sampling``, ``sampling/create``
  via port branché, ``sampling/create`` rejeté sans port, validation des
  paramètres, erreurs LLM, tool ``orchestrate`` visible, version `2.0.0`,
  transport SSE).
- `tests/test_mcp_notifications.py` (nouveau, 20 tests) : notificateurs
  email/Slack (succès, échec non bloquant, payload), builders env,
  ``NotificationService`` (ciblage, compteurs, canaux désactivés, provider
  injecté vs registre défaut), singleton ``get_mcp_client_store()``.
- `tests/test_mcp_version.py` mis à jour : assertions `0.1.0` → `2.0.0`
  (wiring `pyproject.toml`, `DEFAULT_MCP_VERSION`, loader).
- Suite MCP/legacy complète : **593+ passed** (dont 38 nouveaux).

### Notes de migration
- **Breaking** : les clients MCP doivent mettre à jour leur gestion des
  capacités pour accepter ``sampling`` et la version ``2.0.0`` — voir le
  guide de migration ``docs/mcp/migration/v1-to-v2.md``.
- Les clients v1.x **continuent de fonctionner** (compatibilité ascendante) :
  les méthodes existantes restent inchangées, la capacité ``sampling`` est
  ignorée si le client ne la gère pas.
- ``docs/mcp/MANIFEST.md`` régénéré (tâche 18) : header ``v0.1.0`` → ``v2.0.0``
  (compteurs inchangés — 63 tools, 38 read-only — ``ia/tools/tools_config.json``
  n'a pas bougé) ; ``test_mcp_manifest.py`` dérive désormais l'attendu de la
  source de vérité (``load_mcp_version`` / manifeste compilé) pour survivre
  aux futurs bumps SemVer.

---

## v0.1.0 — 2026-09-08 (S1 — Bootstrap)

### Ajouts — MCP Server Layer (tâche 2)
- **Domaine** : `MCPScopeRole` (read_only < contributor < operator < admin)
  dans `app/domain/entities/mcp.py` — ordre de privilège + `granted()`
  (fail-closed).
- **Protocole** : `app/infrastructure/mcp/protocol.py` — enveloppes
  JSON-RPC 2.0, méthodes MCP (initialize, ping, tools/list, tools/call,
  resources/list, prompts/list, notifications/initialized), codes d'erreur.
- **Cœur** : `app/infrastructure/mcp/mcp_server.py` — `MCPServer`
  (dispatch JSON-RPC 2.0, stateless, thread-safe), `MCPTool` (métadonnées +
  handler + annotations read/write/idempotent + scope), `InMemoryToolProvider`
  (registre), `ToolProvider` (Protocol → futur `MCPToolRegistryPort`, tâche 3),
  `ToolError` (erreur métier → isError).
- **Fabrique** : `app/infrastructure/mcp/mcp_server_factory.py` —
  `build_mcp_server(scope=...)` ; tools bootstrap (`mcp_version`, `server_info`).
- **Transport SSE** : `app/infrastructure/mcp/mcp_server_sse.py` —
  `POST /mcp/sse` (flux `text/event-stream`, entête `Mcp-Session-Id`,
  interrupteur de rollback `MCP_SERVER_ENABLED=false` → 503).
- **Transport stdio** : `app/infrastructure/mcp/mcp_server_stdio.py` —
  entry point `thinktuning-mcp` (JSON-RPC ligne à ligne, stdout réservé au
  transport, logs sur stderr).
- **Wiring** : router MCP monté dans `api/main.py`.
- **Packaging** : `[project.scripts] thinktuning-mcp` dans `backend/pyproject.toml`.

### Tests
- `tests/test_mcp_server_basic.py` : initialize, tools/list, tools/call
  (+ isError), scope (filtrage/privilège), transports SSE et stdio, erreurs
  de protocole (parse, invalid request, method not found, batch).
- `tests/test_mcp_version.py` (tâche 1) : versionnement `[tool.mcp] version`.

### Conformité protocolaire
- `protocolVersion = "2025-06-18"` (streamable HTTP) ;
- aucune dépendance externe (ni SDK `mcp`, ni `sse-starlette`) : socle
  self-contained, conforme au contrat MCP des clients (Claude Desktop, …).

### Notes de migration
- Aucun breaking change : surface REST v1 et legacy inchangées ; la couche
  MCP est montée en parallèle.
- Le scope CLIENT complet (`MCPSecurityScope` : client_id, visible_tools,
  quotas, révocations) arrive en S4 (client store) ; le rôle S1 est fixé par
  construction (`build_mcp_server(scope=...)`).

### Ajouts — MCP Domain Ports (tâche 3)
- **Ports** : `app/domain/ports/mcp_ports.py` — 4 Protocols
  `@runtime_checkable` : `MCPToolRegistryPort` (`list_tools`/`call_tool`),
  `MCPResourceRegistryPort` (`list_resources`/`read_resource`),
  `MCPPromptRegistryPort` (`list_prompts`/`get_prompt`), `SamplingPort`
  (`create_text`). Règle d'or : le port rend la vérité non filtrée ; le
  filtrage par scope reste en infrastructure (`MCPServer._visible_tools`),
  les erreurs métier (404/422) passent par `app/domain/errors.py`.
- **Domaine** : `MCPTool` déplacé de `app/infrastructure/mcp/mcp_server.py`
  vers `app/domain/entities/mcp.py` (`required_scope`, `handler`,
  `to_dict()`) ; nouvelles entités gelées `MCPPromptArgument`,
  `MCPResourceTemplate`, `MCPPromptTemplate`, `MCPPromptMessage` (projections
  `to_dict()` alignées spec MCP), exportées via `app.domain.entities`.
- **Rétrocompatibilité** : alias `ToolProvider = MCPToolRegistryPort`
  conservé dans `mcp_server.py` (ré-exporté par `app/infrastructure/mcp`) ;
  `InMemoryToolProvider` satisfait le port via structural typing ;
  `build_mcp_server(tool_provider=...)` est typé contre le port.

### Tests (tâche 3)
- `tests/test_mcp_ports_contract.py` (22 tests) : `isinstance` sur les 4
  ports, `list_tools`/`call_tool` (+ `ToolError`), serveur end-to-end
  `tools/list` + `tools/call`, immutabilité + projections `to_dict`, contrats
  fake resource/prompt/sampling (404/422 via erreurs domaine), filtrage scope
  READ_ONLY vs ADMIN. Suite MCP complète : 76 passed
  (`test_mcp_ports_contract` + `test_mcp_server_basic` + `test_mcp_version`).

### Notes de migration (tâche 3)
- Aucun breaking change : `ToolProvider` et `MCPTool` restent importables
  depuis `app.infrastructure.mcp.mcp_server` (alias / ré-export domaine) ;
- les futures implémentations (adaptateur legacy `ToolRegistry` en S2,
  sampling en S6) se branchent sur les ports sans toucher au transport.

### Ajouts — Manifest Generator (tâche 4, S2 — v0.1.0 Tools)
- **Générateur** : `app/infrastructure/mcp/manifest_generator.py` — compile
  `ia/tools/tools_config.json` (57 entrées legacy TOOL_META) → manifeste MCP :
  - `from_meta_format` (normalisation standard `thinktuning.tool/v1`) ;
  - `to_json_schema` **réutilisé tel quel** — seul le bloc `parameters` devient
    l'`inputSchema` MCP (Rec. 3 du mapping : aucun schéma réinventé) ;
  - `safety_to_annotations` : mapping déterministe `safety` → annotations MCP
    (`readOnlyHint` / `destructiveHint` / `idempotentHint`) ;
  - `resolve_posture` : ordre documenté — `safety` déclarée >
    classification statique legacy `classify_tool()` (Rec. 7/11) >
    fail-closed (mutation + admin) ; exceptions NETWORK (`http_post`,
    `call_api` : mutation du serveur distant, cf. `sandbox_policy.decide` et
    TOOL_STANDARD §1) ;
  - `requiredScope` (hint design-time aligné `MCPScopeRole`) : lecture →
    read_only, write/delete → contributor, exec → operator, unknown → admin ;
  - mode tolérant (warnings collectés) vs `strict=True` (gating CI, lève
    `ManifestError`) — même convention que `version_loader` (tâche 1) ;
  - `entry_to_mcp_tool` : couture manifeste → entité domaine `MCPTool`
    (wiring des handlers en tâche 6, sans duplication de métadonnées).
- **Catalogue produit** : `docs/mcp/MANIFEST.md` — GÉNÉRÉ (ne pas éditer) :
  tableau des 57 tools (scope, annotations, description) + `inputSchema`
  détaillés + avertissements de compilation ; CLI de régénération
  `python -m app.infrastructure.mcp.manifest_generator` (depuis `backend/`).
- **Séparation** : manifeste = design-time (statique, sans arguments) ; la
  décision runtime par appel (chemins sensibles, anti-SSRF, SQL mutant) reste
  portée par `policy_adapter` (tâche 5).

### Tests (tâche 4)
- `tests/test_mcp_manifest.py` (87 tests) : mapping `safety` → annotations,
  ordre de résolution de posture, REUSE de `to_json_schema`, document
  manifeste (tri stable, compteurs, strict), contrat du catalogue réel
  (57 tools, sélection v0.1.0 read-only, tools mutatifs), projection
  `MCPTool` + port `MCPToolRegistryPort`, rendu Markdown déterministe,
  I/O tolérante/stricte, anti-divergence `MANIFEST.md` commité.
- Suite MCP complète : 168 passed
  (`test_mcp_version` + `test_mcp_server_basic` + `test_mcp_ports_contract`
  + `test_mcp_manifest` + `test_legacy_registry_adapter`).

### Notes de migration (tâche 4)
- Aucun breaking change : module additive, aucun ré-export dans
  `app.infrastructure.mcp.__init__` (même convention que `mcp_server_stdio`
  : évite la double-importation via `python -m`) ;
- les tools non classés par la policy legacy (`add`, `calc`, tools ML…)
  ressortent fail-closed (mutation + admin) avec un warning actionnable —
  la tâche 6 lèvera l'ambiguïté (déclarations `safety` standard v1) ;
- écart préexistant hors périmètre : certaines descriptions de
  `tools_config.json` sont en double-encodage UTF-8/Windows-1252 — le
  générateur compile la source fidèlement (nettoyage = chantier dédié).

### Ajouts — Legacy Tool Provider (tâche 6, S2 — v0.1.0 Tools)
- **Provider** : `app/infrastructure/mcp/legacy_tool_provider.py` — projection de
  la sélection read-only v0.1.0 (`V010_READ_ONLY_TOOLS`, 13 tools nommés par la
  checklist — le label « 12 » de la roadmap arrondissait le compte) du registre
  legacy `ia/tools/tool_registry.py` sur le port `MCPToolRegistryPort` (tâche 3) :
  - **REUSE total** : `compile_tool` (inputSchema + annotations) →
    `entry_to_mcp_tool` → handlers câblés par DÉLÉGATION aux implémentations
    legacy (`TOOLS[name](**args)`) ;
  - **sécurité par délégation** (zéro règle dupliquée) : `safe_resolve`
    (fichiers — aucune évasion), `url_scheme_allowed` + `enforce_host_policy`
    (réseau — anti-SSRF), AST whitelisté (`calc` — aucun exec/eval) ;
  - **fail-closed à la construction** : posture mutation / nom inconnu /
    implémentation absente → tool EXCLU (warning) ; à l'appel : args requis
    manquants et exceptions legacy → `ToolError` (MCP `isError`, jamais un crash).
- **Fabrique** : `build_mcp_server()` expose par défaut bootstrap S1 (2) +
  sélection v0.1.0 (13) = 15 tools ; `tool_provider=...` remplace entièrement
  le registre (régression `UnboundLocalError` sur l'injection explicite corrigée).
- **Déclaration** : `add`/`calc` ont reçu une `safety` standard v1 (`safe`) dans
  `ia/tools/tools_config.json` — lève le gap fail-closed documenté à la tâche 4
  (tools non classés → posture DÉCLARÉE read-only, jamais devinée).

### Tests (tâche 6)
- `tests/test_legacy_tool_provider.py` (40 tests) : sélection exacte (13, ordre
  alphabétique), contrat du port, annotations read-only/idempotent + scope
  READ_ONLY, alignement bit-à-bit manifeste compilé (anti-divergence), exécution
  réelle par délégation (sandbox tmp isolée : fichiers ; calculs purs ; validation
  réseau synchrone SANS I/O externe), erreurs métier (dont translucidité des
  `ToolError` legacy), fail-closed à la construction (exclusions tracées),
  intégration serveur end-to-end (`tools/call` add, count_lines).
- Suite MCP complète : **289 passed** (`test_mcp_version` + `test_mcp_server_basic`
  + `test_mcp_ports_contract` + `test_mcp_manifest` + `test_mcp_policy_adapter`
  + `test_legacy_registry_adapter` + `test_legacy_tool_provider`).

### Notes de migration (tâche 6)
- Aucun breaking change : surface REST v1 et registre legacy inchangés ; la couche
  MCP ajoute une projection read-only (les tools mutatifs legacy restent hors
  périmètre v0.1.0 — S3 étendra la lecture seule).
- Garde-fou structurel : la surface read-only ne peut JAMAIS exposer un tool
  classé mutation, même demandé explicitement en `selection` — la posture ne se
  devine pas, elle se compile depuis `tools_config.json` (source unique).

---

## v1.0.0 — 2026-09-08 (S3 — Public Beta, en cours)

### Ajouts — 25 Tools (extension read-only, tâche 7)
- **Sélection v1.0.0** : `app/infrastructure/mcp/legacy_tool_provider.py` —
  `V100_READ_ONLY_TOOLS` (25 tools uniques) = union des deux checklists
  (v0.1.0 : 13 noms ; tâche 7 : 13 noms → 24 uniques, `file_info`/
  `count_lines` en commun) + 2 lectures pures déjà classées READ
  (`read_json`, `search_in_files`) pour tenir le compte produit « 25 »
  (roadmap v1.0.0 Public Beta) :
  - métier ThinkTuning (lecture) : `job_list`, `job_get`, `model_versions`,
    `dataset_stats`, `predict_sentiment` ;
  - système/diagnostic (lecture) : `env_info`, `disk_usage`, `gpu_info`, `now` ;
  - fichiers (lecture pure) : `tail_file`, `read_json`, `search_in_files`.
- **Exclusion structurelle** : `touch` (checklist tâche 7) est une ÉCRITURE
  (WRITE dur, `sandbox_policy.classify_tool`) — la surface read-only ne
  l'expose JAMAIS ; elle rejoindra la surface write/exec (tâche 17).
- **Déclaration** : les 5 tools métier (auparavant non classés → fail-closed
  mutation + admin) ont reçu la `safety` standard v1 (`safe`) dans
  `ia/tools/tools_config.json` → posture read-only DÉCLARÉE dans le
  manifeste compilé (`MANIFEST.md` régénéré : 33 read-only / 24 mutation,
  avertissements 12 → 7).
- **Policy runtime** : `sandbox_policy.classify_tool` classe désormais
  `add`/`calc` en READ (calcul pur, aucune I/O) — alignement du verdict
  runtime (`decide_action()` → `AUTO_APPROVE`) sur la posture design-time
  déclarée ; les 25 tools sont auto-approuvés.
- **Fabrique** : `build_mcp_server()` expose par défaut bootstrap S1 (2) +
  sélection v1.0.0 (25) = 27 tools ; `build_v010_read_only_provider()`
  reste disponible (13 tools — déploiements restreints / tests v0.1.0).
- **Exports** : `V100_READ_ONLY_TOOLS` et `build_v100_read_only_provider`
  ré-exportés par `app.infrastructure.mcp` (parité avec v0.1.0).

### Tests (tâche 7)
- `tests/test_mcp_tools_25.py` (nouveau, 5 tests) : sélection exacte (25),
  serveur par défaut (27 = 2 bootstrap + 25), annotations cohérentes
  (`readOnlyHint`/`destructiveHint`/`idempotentHint` + scope READ_ONLY),
  `decide_action()` → `AUTO_APPROVE` pour chaque tool, visibilité scope
  `read_only` complète.
- `tests/test_legacy_tool_provider.py` étendu (40 → 48 tests) : checklist
  v1.0.0 (25 exact), provider v100 (ordre déterministe, annotations/scope),
  déclaration `safety: safe` des 5 tools métier, serveur par défaut 27
  tools ; le fournisseur v0.1.0 reste testé explicitement.
- Suite MCP complète : **307 passed**.

### Notes de migration (tâche 7)
- Breaking-behavior maîtrisé : `tools/list` par défaut passe de 15 à 27
  tools (extension additive read-only) — les clients MCP découvrent le
  catalogue dynamiquement (aucun code client à changer) ; la surface
  REST v1 et le registre legacy restent inchangés.
- Version de surface MCP inchangée (`[tool.mcp] version = 0.1.0`) : le
  bump 1.0.0 suivra le jalon Public Beta complet (resources + prompts,
  tâches 8/9 — cible roadmap 2026-09-30).
- Garde-fou inchangé : tout tool dont la posture compilée n'est pas
  read-only est EXCLU de la surface, même présent dans la sélection.

### Ajouts — 5 Resources `thinktuning://` (tâche 8)
- **Provider** : `app/infrastructure/mcp/resources/resource_provider.py` —
  `LegacyResourceProvider` implémente le port domaine
  `MCPResourceRegistryPort` (tâche 3) : chaque resource est une VUE
  lecture-seule résolue par DÉLÉGATION aux tools internes (zéro règle
  réimplémentée, même principe que `legacy_tool_provider`) :
  - `thinktuning://jobs` → `job_list()` → JSON (20 derniers jobs) ;
  - `thinktuning://jobs/{job_id}` → `job_get(job_id)` → JSON (payload complet) ;
  - `thinktuning://models` → `model_versions()` → JSON (versions sandbox) ;
  - `thinktuning://datasets/{path}/stats` → `dataset_stats(path)` → JSON
    (chemin RELATIF multi-segments sous la sandbox) ;
  - `thinktuning://config` → `agent_config()` → JSON (clés API masquées).
- **Domaine** : entité `MCPResource` (`uri`, `name`, `description`,
  `mimeType`, immuable + `to_dict()` aligné spec MCP `resources/list`) dans
  `app/domain/entities/mcp.py` ; le port `list_resources()` est désormais
  typé `list[MCPResource]` (le port spéculatif de la tâche 3 retourne
  l'entité réelle) ; bonus `list_resource_templates()` — les 2 URIs
  paramétrées exposées comme `MCPResourceTemplate` (préparation
  `resources/templates/list`, tâche 13).
- **Sécurité (défense en profondeur, fail-closed)** :
  - URI : parsing strict par routes regex ancrées (ordre déterministe) —
    URI inconnue → `NotFoundError` (aucun oracle d'inventaire) ; segments
    validés APRÈS un décodage percent-encoding unique : traversée (`..`),
    segments vides/`.`, backslash, caractères de contrôle et `%` résiduel
    (anti double-encodage) refusés AVANT toute I/O, longueurs plafonnées ;
  - chemins : `safe_resolve` porté PAR DÉLÉGATION (`ia/tools/sandbox.py`) —
    2ᵉ ligne de défense derrière la validation d'URI ;
  - SQL : jobs.db ouverte en `mode=ro` + `PRAGMA query_only`
    (`ia/tools/ml_tools.py`) — toute écriture refusée par SQLite ;
  - secrets : `thinktuning://config` retire les clés API (`openrouter`,
    HF) et les remplace par `has_*` + `*_masked` (convention dashboard
    `_settings_payload`) — JAMAIS en clair sur la surface MCP ;
  - résolution paresseuse des tools internes (premier `read_resource`) :
    construction du provider sans I/O ni import lourd (fastapi/requests ne
    sont chargés que si `thinktuning://config` est lu).
- **Serveur** : `resources/read` ajouté au protocole (`MCPMethod`) et au
  dispatch (`{contents: [{uri, mimeType?, text}]}`) ; `NotFoundError` →
  erreur JSON-RPC `Invalid params` (-32602, jamais un crash) ; autres
  exceptions → `Internal error` (fail-closed) ; capability `resources`
  annoncée à l'`initialize` quand un registre est branché ;
  `build_mcp_server()` branche `LegacyResourceProvider` par défaut
  (`resource_provider=...` remplace entièrement).
- **Exports** : `LegacyResourceProvider` + `build_legacy_resource_provider`
  ré-exportés par `app.infrastructure.mcp` (nouveau sous-paquet `resources/`).

### Tests (tâche 8)
- `tests/test_mcp_resources.py` (nouveau, 38 tests) : entité `MCPResource`
  (immutabilité + projection), `ListResources` = les 5 (port + JSON-RPC +
  fabrique par défaut), `ReadResource` statique/paramétré (délégation
  vérifiée, chemin multi-segments), masquage des clés API, 10 URIs
  malveillantes rejetées (traversée, double-encodage, backslash, null
  byte…), erreurs serveur (404 → -32602, `uri` manquant), capability
  `initialize`, intégration legacy RÉELLE en sandbox temporaire
  (`AGENT_SANDBOX_ROOT` → tmp_path) : jobs.db lue en `query_only` (INSERT
  refusé), dataset CSV profilé (pandas), évasion de chemin bloquée par
  `safe_resolve`, versions de modèles scannées.
- `tests/test_mcp_ports_contract.py` mis à jour : fake
  `_FakeResourceRegistry` retourne `MCPResource` (nouveau type du port).
- `tests/test_mcp_server_basic.py` mis à jour : `resources/list` par défaut
  = les 5 resources v1.0.0 (prompts toujours vides — tâche 9).
- Suite MCP complète : **340 passed**.

### Notes de migration (tâche 8)
- Breaking-behavior maîtrisé : `resources/list` par défaut passe de `[]` à
  5 resources (extension additive read-only) et `initialize` annonce la
  capability `resources` — les clients MCP conformes découvrent les
  resources dynamiquement (aucun code client à changer).
- Aucune écriture possible via les resources : toutes les lectures passent
  par des tools read-only (`AUTO_APPROVE`), SQLite en lecture stricte et
  la sandbox — MCP ne bypass jamais la security interne.

### Ajouts — 2 Prompts MCP (tâche 9)
- **Provider** : `app/infrastructure/mcp/prompts/prompt_provider.py` (nouveau
  sous-paquet `prompts/`) — `PromptProvider` hérite du port domaine
  `MCPPromptRegistryPort` (tâche 3, même convention que
  `LegacyResourceProvider`) : chaque prompt est un template nommé résolu
  LOCALEMENT (aucune I/O, aucun tool, aucun LLM) :
  - `analyze-sentiment` → « Analyse le sentiment de ce texte: {text} »
    (argument `text`, requis) ;
  - `plan-training` → « Planifie un entraînement pour: {dataset} »
    (argument `dataset`, requis).
- **Domaine** : entités posées à la tâche 3 et réutilisées telles quelles —
  `MCPPromptTemplate` (`name`, `description`, `arguments`),
  `MCPPromptArgument` (`name`, `description`, `required`),
  `MCPPromptMessage` (`role`, `content`), projections `to_dict()` alignées
  sur la spec MCP (`prompts/list`, `prompts/get`).
- **Sécurité (fail-closed)** :
  - nom : prompt non déclaré → `NotFoundError` (message actionnable — le
    catalogue est public via `prompts/list`, lister les noms ne fuit rien) ;
  - arguments : argument requis manquant → `ValidationError` (422,
    client-réparable) ; valeur non-string → `ValidationError` (la spec MCP
    ne transporte que des chaînes — on n'interpole JAMAIS un objet) ;
  - interpolation : les templates sont possédés par le SERVEUR, les
    arguments du client ne sont que des VALEURS substituées (`str.format`)
    — pas d'accès attribut/index contrôlé par le client, valeurs substituées
    non re-traitées comme des gabarits (pas de récursion) ;
  - arguments surnuméraires : ignorés (pas d'oracle d'erreur différentiel).
- **Serveur** : `prompts/get` ajouté au protocole (`MCPMethod`) et au
  dispatch (`{description?, messages: [{role, content: {type: text, text}}]}`)
  ; `NotFoundError`/`ValidationError` → erreur JSON-RPC `Invalid params`
  (-32602, message actionable préservé, jamais un crash) ; autres exceptions
  → `Internal error` (fail-closed) ; capability `prompts` annoncée à
  l'`initialize` quand un registre est branché (une surface sans prompts est
  indiscernable d'un prompt inconnu) ; `build_mcp_server()` branche
  `PromptProvider` par défaut (`prompt_provider=...` remplace entièrement).
- **Exports** : `PromptProvider` + `build_prompt_provider` ré-exportés par
  `app.infrastructure.mcp` (nouveau sous-paquet `prompts/`, parité avec
  `resources/`).

### Tests (tâche 9)
- `tests/test_mcp_prompts.py` (nouveau, 21 tests) : contrat de port
  (`isinstance`, héritage), `ListPrompts` = les 2 (ordre déterministe,
  métadonnées + projection spec), `GetPrompt` résolution exacte des 2
  templates (FR/EN), projection MCP du message, surplus d'arguments ignoré,
  valeurs substituées non re-traitées comme gabarits, erreurs fail-closed
  (inconnu → 404, requis manquant / `arguments=None` / valeur non-string →
  422), erreurs serveur (404/422 → -32602, `name` manquant, `arguments`
  non-objet, surface sans registre indiscernable d'un prompt inconnu),
  capability `initialize` (annoncée avec registre, absente sans).
- `tests/test_mcp_server_basic.py` mis à jour : `prompts/list` par défaut =
  les 2 prompts v1.0.0 (remplace l'attente « liste vide » de la tâche 8).
- Suite MCP complète : **356 passed**.

### Notes de migration (tâche 9)
- Breaking-behavior maîtrisé : `prompts/list` par défaut passe de `[]` à
  2 prompts et `initialize` annonce la capability `prompts` — les clients
  MCP conformes découvrent les prompts dynamiquement (aucun code client à
  changer) ; la surface REST v1, les tools et les resources restent
  inchangés.
- Aucun effet de bord : la résolution d'un prompt est une substitution de
  chaînes pure (aucune I/O, aucun appel tool/LLM, aucune écriture) ; la
  version de surface MCP reste `[tool.mcp] version = 0.1.0` (le bump 1.0.0
  suivra le jalon Public Beta complet).
---

## Unreleased — S4 (Security) — Scopes + Quotas + Rate Limiting (tâche 11)

### Ajouts
- **Enforceur** : `app/infrastructure/mcp/security/scope_enforcer.py` —
  `MCPScopeEnforcer` (scope/quota/rate limit composés, thread-safe, état en
  mémoire borné) + fonctions module `check_scope` / `check_quota` /
  `check_rate_limit` / `enforce`. Fail-closed : client inconnu, révoqué, rôle
  inconnu, tool hors whitelist/catalogue → `MCPAccessDeniedError` ; quota
  « manual approval » / heure (fenêtre glissante) →
  `MCPQuotaExceededError` ; débit per-client (`rate_limit_per_minute`) →
  `MCPRateLimitExceededError` (+ `retry_after`).
- **4 catalogues par rôle** (roadmap docs/mcp/MCP_SECURITY.md) : `read_only`
  (12 = V010 − `file_checksum`), `contributor` (25 = V100), `operator`
  (35 = +10 write/exec, tâche 17), `admin` (40 = +5 gestion mutante, tâche 19).
- **Intégration rate limit** : la primitive `TokenBucket` est déplacée dans
  `app/infrastructure/mcp/security/rate_limit_bucket.py` et ré-exportée par
  `api/middlewares/rate_limit.py` — une seule implémentation, deux
  consommateurs (REST : clé IP + limite globale ; MCP : clé client_id + limite
  du scope). L'enforceur n'importe JAMAIS `api` (la suite MCP reste légère).
- **Résolution paresseuse** : le `MCPClientStore` (`core/mcp_client_store`) est
  résolu via `default_scope_resolver` au moment de l'appel — aucun import
  lourd, aucune base créée au module import.

### Tests
- `tests/test_mcp_scope_enforcer.py` (nouveau, 29 tests) : tailles des
  catalogues (12/25/35/40) + ordre de privilège, chaque rôle (catalogue
  complet vu / frontières refusées), whitelist `visible_tools` vs catalogue,
  client inconnu / révoqué / rôle inconnu (fail-closed), quota (dépassement,
  fenêtre glissante, isolation par client, quota 0, tools de lecture jamais
  comptés), rate limit (burst, isolation, refill), portail `enforce`,
  partage de la primitive `TokenBucket` avec le middleware REST.
- Suite MCP/legacy : 424 passed (dont 29 nouveaux).

### Notes de migration
- Aucun breaking change : la surface REST v1, les tools/resources/prompts MCP
  et le registre legacy restent inchangés ; le middleware `rate_limit.py`
  expose la même primitive `TokenBucket` (refactor interne, comportement
  identique — tests API v1 predict : 18 passed).
- Le câblage du portail `enforce` au transport MCP (SSE/stdio) et l'audit
  `ACT_MCP_TOOL_CALL` arrivent avec la tâche 12.

---

## v2.1.0 — 2026-09-09 (S6 — Extension write/exec, tâche 17)

### Ajouts — 10 Tools write/exec (35 tools au total)
- **Provider** : `app/infrastructure/mcp/write_exec_tool_provider.py` —
  `WriteExecToolProvider` (hérite de `LegacyRegistryToolProvider`) projette la
  surface **write/exec filtrée** du registre legacy avec un fail-closed
  INVERSÉ : un tool résolu en posture LECTURE (déclaration `safety: safe`…)
  est EXCLU à la construction — la surface write/exec n'expose que de la
  mutation (miroir exact du provider read-only, tâches 6/7).
- **Sélection v2.1.0** : `V210_WRITE_EXEC_TOOLS` (10 tools, checklist exacte
  de la tâche 17) — écriture sandbox : `write_file`, `write_json`,
  `append_file`, `make_dir`, `copy_path` ; exécution : `run_command`,
  `run_python` ; pilotage ML : `start_training`, `cancel_training`,
  `stop_training`. Union avec la sélection read-only (25, tâche 7) = **35
  tools** (compte roadmap v2.1.0 « Orchestrate »), disjointe par construction.
- **Scope** : les 10 tools sont exposés en `MCPScopeRole.OPERATOR`
  (`WRITE_EXEC_TOOLS_SCOPE`) — aligné sur le catalogue par rôle
  (`scope_enforcer.OPERATOR_ROLE_TOOLS` = 35 tools) et sur l'échelle de
  privilège (docs/mcp/MCP_SECURITY.md) ; un client `read_only` ne les voit
  JAMAIS (filtre fail-closed du serveur).
- **Annotations** : les 10 tools compilent en posture mutante
  (`destructiveHint: true` / `idempotentHint: false` / `readOnlyHint: false`)
  — le client MCP peut avertir l'utilisateur avant l'appel.
- **Policy runtime** : `sandbox_policy.classify_tool` classe désormais
  `start_training`/`cancel_training`/`stop_training` en EXEC et
  `mcp_version`/`server_info`/`orchestrate` en SYSTEM : chaque appel
  write/exec passe par `decide_action()` → `APPROVE` = validation humaine
  obligatoire ; les règles dures (cibles sensibles `.git`/`.env`… → REJECT,
  jamais exécuté, audité) restent inchangées.
- **Fabrique** : `build_mcp_server()` ajoute l'extension à partir de la
  version 2.1.0 de la surface (`resolved_version >= MCPVersion(2, 1, 0)`) et
  enveloppe le registre d'un `PolicyGateToolProvider` (tâche 5) — un
  `tools/call` mutant ne peut JAMAIS atteindre l'implémentation legacy sans
  approbation (AUTO_APPROVE lecture/introspection → exécution directe). En
  dessous de v2.1.0, la surface reste inchangée (27 tools, sans gate).

### Tests (tâche 17)
- `tests/test_mcp_tools_35.py` (nouveau, 23 tests) : sélection exacte (10),
  provider (posture mutante + scope OPERATOR pour les 10), `decide_action()`
  → `APPROVE` pour chaque tool, `REJECT` des cibles sensibles (7 cas
  paramétrés), union 35 disjointe, fail-closed inversé (posture lecture et
  nom inconnu exclus à la construction), délégation legacy injectée, gating
  version/scope (v1.0.0 / v2.0.0 / `read_only` → invisible ; v2.1.0
  OPERATOR → 38 = 35 + 2 bootstrap + orchestrate), gate serveur
  (AUTO_APPROVE → exécution, APPROVE → « Manual approval required », REJECT
  → « Policy rejected »).
- Suite MCP/legacy complète : **578 passed** (dont 23 nouveaux).

### Notes de migration
- Breaking-behavior maîtrisé : `tools/list` en v2.1.0 OPERATOR passe de 28 à
  38 tools (extension additive filtrée) ; les clients `read_only` et les
  surfaces < v2.1.0 ne voient AUCUN changement (aucun code client à changer).
- Aucune écriture n'est exécutable sans approbation : la policy bloque AVANT
  tout handler (`APPROVE` → « Manual approval required », `REJECT` → refus).
- `docs/mcp/MANIFEST.md` inchangé : le catalogue design-time reflète
  `ia/tools/tools_config.json`, qui n'a pas bougé — la surface MCP est une
  projection filtrée du même manifeste compilé (`compile_tool`).

## v2.2.0 — 2026-09-09 (S7 — 40 Tools full catalogue, tâche 19)

### Ajouts — 5 Tools admin (40 tools au total, catalogue complet)
- **Provider** : `app/infrastructure/mcp/admin_tool_provider.py` —
  `AdminToolProvider` (hérite de `LegacyRegistryToolProvider`) projette la
  surface **admin filtrée** du registre legacy avec le fail-closed INVERSÉ du
  write/exec : un tool résolu en posture LECTURE (`safety: safe`…) est EXCLU à
  la construction — la surface admin n'expose que de la mutation.
- **Sélection v2.2.0** : `V220_ADMIN_TOOLS` (5 tools, checklist exacte de la
  tâche 19) — fichiers : `move_path`, `remove_path`, `split_file`,
  `dedupe_lines` ; archives : `unzip_file`. Union read-only (25, tâche 7) +
  write/exec (10, tâche 17) + admin (5) = **40 tools** — catalogue complet
  (compte roadmap v3.0.0 « MCP-First », docs/mcp/MCP_ROADMAP.md), disjointe
  par construction.
- **Scope** : les 5 tools sont exposés en `MCPScopeRole.ADMIN`
  (`ADMIN_TOOLS_SCOPE`) — aligné sur le catalogue par rôle
  (`scope_enforcer.ADMIN_ROLE_TOOLS` = 40 tools, déjà référencé par la tâche
  11) et sur l'échelle de privilège (docs/mcp/MCP_SECURITY.md) : `remove_path`
  (DELETE) et l'extraction d'archives ne sont JAMAIS visibles d'un rôle
  < admin (filtre fail-closed du serveur, `MCPScopeRole.granted`).
- **Annotations** : les 5 tools compilent en posture mutante
  (`destructiveHint: true` / `idempotentHint: false` / `readOnlyHint: false`)
  — `sandbox_policy.classify_tool` : WRITE (`move_path`, `split_file`,
  `dedupe_lines`, `unzip_file`), DELETE (`remove_path`).
- **Policy runtime** : chaque appel admin passe par `sandbox_policy.decide_action()`
  → `APPROVE` = validation humaine obligatoire (gate `PolicyGateToolProvider`,
  actif dès la v2.1.0) ; les règles dures restent inchangées (cibles
  sensibles `.git`/`.env`/`id_rsa`… → `REJECT`, jamais exécuté, audité).
  `MCPSecurityScope` (S4) : la portée effective d'un client `admin`
  (`effective_tools`, whitelist vide = catalogue) couvre les 40 tools ; la
  whitelist explicite `visible_tools` reste soustractive.
- **Fabrique** : `build_mcp_server()` ajoute l'extension à partir de la
  version 2.2.0 de la surface (`resolved_version >= MCPVersion(2, 2, 0)`) —
  43 tools visibles en v2.2.0 ADMIN (40 legacy + 2 bootstrap + orchestrate).
  En dessous de v2.2.0, la surface reste inchangée (38 tools max en v2.1.0).

### Tests (tâche 19)
- `tests/test_mcp_tools_40.py` (nouveau, 30 tests) : sélection exacte (5),
  provider (posture mutante + scope ADMIN), `decide_action()` → `APPROVE`
  pour chaque tool, `REJECT` des cibles sensibles (7 cas paramétrés),
  unions disjointes 25+10+5 = 40 et égalité avec `ADMIN_ROLE_TOOLS`,
  fail-closed inversé (posture lecture et nom inconnu exclus), délégation
  legacy injectée, `MCPSecurityScope` (portée effective admin/operator,
  whitelist soustractive, projection par rôle), gating version/scope
  (v2.1.0 / `read_only` / `contributor` / `operator` → invisible ; v2.2.0
  ADMIN → 43), gate serveur (AUTO_APPROVE → exécution, APPROVE → « Manual
  approval required », REJECT → « Policy rejected »).
- Suite MCP ciblée : **279 passed** (manifeste, scopes, policy, serveur,
  tools 25/35/40, version) — zéro régression.

### Notes de migration
- Breaking-behavior maîtrisé : `tools/list` en v2.2.0 ADMIN passe de 38 à
  43 tools (extension additive filtrée) ; les clients `operator` et
  inférieurs ainsi que les surfaces < v2.2.0 ne voient AUCUN changement.
- Aucune suppression/déplacement n'est exécutable sans approbation : la
  policy bloque AVANT tout handler (`APPROVE` → « Manual approval required »,
  `REJECT` → refus ; cibles sensibles jamais exécutées).
- `docs/mcp/MANIFEST.md` inchangé : le catalogue design-time reflète
  `ia/tools/tools_config.json`, qui n'a pas bougé — la surface admin est une
  projection filtrée du même manifeste compilé (`compile_tool`).
