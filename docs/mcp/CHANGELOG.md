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