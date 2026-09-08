# MCP Changelog — ThinkTuning

> **Audit-trail** de la surface MCP (versionnée via `docs/mcp.version`).
> Chaque entrée documente le livrable S1/S2/…, les fichiers touchés et les
> garanties de migration (SemVer : breaking changes → major bump).

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