# MCP Implementation Mapping — Lien Code Existant ↔ MCP

> **Traçabilité** : chaque recommandation MCP ↔ composant existant dans ThinkTuning.
> Ce document prouve que **rien n’est réinventé** — MCP est **un nouveau protocole sur du code existant**.

---

## 🔗 Strategy → Code

| Recommandation MCP | Composant existant | Action MCP |
|---|---|---|
| 1. Capabilities platform | `app/agent/core.py` (AgentCore) | Expose comme `orchestrate` tool MCP |
| 2. MCP = surface principale | `api/routes/agent.py` (`GET /tools`) | Remplace par `ListTools` MCP |
| 3. Protocol interne = config | `ia/tools/tools_config.json` + `tool_schema.py` | Source de configuration → génère le manifeste MCP |
| 4. Product Council | `core/audit_store.py` + `core/mcp_client_store.py` (nouveau) | Gouvernance + tracking clients |
| 5. Versioning | `app/config/settings.py` (Pydantic Settings) | Ajouter `MCP_VERSION` |
| 6. Orchestrate tool | `app/agent/factory.py` (`build_agent_core`) | Wrap `AgentCore.run()` → tool MCP |
| 7. Security scope | `ia/agent/approvals.py` + `sandbox_policy.py` | `decide_action()` → MCP annotations + scope filter |
| 8. Audit MCP | `core/audit_store.py` (`ACT_TOOL`, `ACT_RUN`) | Ajouter `ACT_MCP_TOOL_CALL` etc. |
| 9. Branding MCP | `ia/agent/system_prompt.py` (descriptions) | `build_tools_section()` → descriptions MCP humaines |
| 10. Roadmap | `ARCHITECTURE.md` §6 (backlog) | Aligné sur la roadmap MCP |
| 11. Avantage compétitif | `ia/tools/sandbox.py` (fail-closed) | Export comme `annotations.destructiveHint` |
| 12. Mutation existentielle | `ARCHITECTURE_DECOUPLAGE.md` §8 | MCP devient l’interface v1 (au lieu de `/api/v1/*`) |

---

## 🛠️ Implémentation par Recommandation

### Rec. 1 + 6 : Exposer `AgentCore` comme tool MCP `orchestrate`
- **Existant** : `app/agent/core.py` → `AgentCore.run(prompt)` → `AgentRunResult`
- **Nouveau** : `app/infrastructure/mcp/tools/orchestrate_tool.py` → wrap `build_agent_core().run()`
- **Sécurité** : passe par `decide_action()` → `APPROVE` (mutation → validation humaine)

### Rec. 2 : MCP = surface principale
- **Existant** : `api/routes/agent.py` → `GET /tools` (liste 25 tools)
- **Nouveau** : `app/infrastructure/mcp/mcp_server_sse.py` → `ListToolsRequest` → interroge `MCPToolRegistryPort` (port domaine, tâche 3)

### Rec. 3 : Protocol interne = configuration
- **Existant** : `ia/tools/tools_config.json` (manifeste thinktuning.tool/v1)
- **Existant** : `ia/tools/tool_schema.py` → `to_json_schema()` (déjà produit le JSON Schema MCP)
- **Nouveau** : `app/infrastructure/mcp/manifest_generator.py` → compile `tools_config.json` → MCP manifest

### Rec. 7 : Security scope
- **Existant** : `ia/agent/approvals.py` → `classify(tool, args)` → `Decision` (auto_approve/approve/reject)
- **Existant** : `ia/agent/policies/sandbox_policy.py` → `decide(tool, args)` → `Decision`
- **Nouveau** : `app/infrastructure/mcp/policy_adapter.py` → mappe `Decision` → MCP `annotations.readOnlyHint` / `destructiveHint`
- **Fait (tâche 3)** : `app/domain/ports/mcp_ports.py` → 4 ports
  `@runtime_checkable` (`MCPToolRegistryPort`, `MCPResourceRegistryPort`,
  `MCPPromptRegistryPort`, `SamplingPort`) ; `MCPSecurityScope`
  (client store → visible_tools) arrive en S4.

### Rec. 8 : Audit
- **Existant** : `core/audit_store.py` → `ACT_TOOL`, `ACT_RUN`, `ACT_APPROVAL`
- **Nouveau** : `ACT_MCP_TOOL_CALL`, `ACT_MCP_RESOURCE_READ`, etc. → dans la même table

### Rec. 9 : Branding
- **Existant** : `ia/agent/system_prompt.py` → `_resolve_description()` (description JSON → docstring fallback)
- **Nouveau** : `manifest_generator.py` réécrit les descriptions en **langage humain** (pas docstring technique)
