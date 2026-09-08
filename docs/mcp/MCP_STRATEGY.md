# MCP Strategy — ThinkTuning Capabilities Platform

> **Décision de positionnement** (signée par le MCP Product Council — 2026-09-08)
>
> ThinkTuning n’est plus un backend ML pour un dashboard.  
> ThinkTuning est une **capabilities platform** exposée via MCP.

---

## 🎯 Mission MCP

> Penser MCP comme **la surface officielle de ThinkTuning**.  
> L’API HTTP devient un **backend interne**.  
> MCP est la **source de vérité pour la surface**.

---

## 🔑 Les 12 Recommandations Décisionnaires

| # | Recommandation | Decision |
|---|---|---|
| 1 | **Assume ThinkTuning comme capabilities platform** | ✅ Oui — fin du statut “backend ML” |
| 2 | **MCP = surface principale** | ✅ Oui — manifeste MCP = catalogue produit officiel |
| 3 | **Protocole interne = configuration interne** | ✅ Oui — `thinktuning.tool/v1` → format de configuration (sécurité, sandbox) |
| 4 | **MCP Product Council** | ✅ 3 rôles + 2 processus (RFC + Changelog) |
| 5 | **MCP versionné comme API publique** | ✅ SemVer — chaque breaking change → version majeure |
| 6 | **Orchestrateur interne exposé via `orchestrate`** | ✅ Tool MCP `orchestrate(prompt)` → délègue à `AgentCore` |
| 7 | **Scopes / quotas / revocations** | ✅ MCPSecurityScope obligatoire |
| 8 | **Audit MCP complet** | ✅ ACT_MCP_TOOL_CALL / _RESOURCE_READ / _PROMPT_GET |
| 9 | **Branding MCP-first** | ✅ “ThinkTuning MCP”, descriptions humaines, catégories claires |
| 10 | **Roadmap MCP-first** | ✅ v1 (tools) → v2 (resources) → v3 (prompts/sampling) → v4 (orchestrate) → v5 (MCP-first) |
| 11 | **Avantage compétitif MCP** | ✅ ML spécialisé + fail-closed + orchestrateur + sandbox |
| 12 | **Mutation existentielle assumée** | ✅ MCP = mutation, pas extension |

---

## 🚫 Ce qui est interdit

- Exposer un tool sans RFC MCP
- Casser un tool MCP sans deprecation
- Ajouter un resource sans `thinktuning://` URI standardisé
- Exposer un tool `dangerous` via MCP
- Publier une version MCP sans changelog

---

## 📦 Architecture cible (post-MCP-first)

```
MCP Clients (Claude, Cursor, Agents)
       │
       ▼
┌─────────────────────────────────────┐
│  MCP Server Layer (app/infra/mcp)  │ ◀── Surface officielle
│  ├─ manifest_generator.py           │
│  ├─ mcp_server_sse.py              │
│  ├─ mcp_server_stdio.py            │
│  ├─ policy_adapter.py              │
│  ├─ client_store.py                │
│  └─ audit_trail.py                 │
└─────────────┬──────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Domain Ports (app/domain/ports)    │ ◀── Vérité interne
│  ├─ MCPToolRegistryPort            │
│  ├─ MCPResourceRegistryPort        │
│  ├─ MCPPromptRegistryPort          │
│  └─ SamplingPort                   │
└─────────────┬──────────────────────┘
              │
              ▼
┌─────────────────────────────────────┐
│  Internal (ia/, core/, src/)         │
│  thinktuning.tool/v1 (config)       │
│  AgentCore (orchestrateur)          │
│  Sandbox / Approvals / Budget       │
└─────────────────────────────────────┘
```

→ Le `MCP Server Layer` est **la source de vérité pour la surface**.
→ Le `Domain` est **la source de vérité pour la logique**.
→ Le `MCP Server Layer` **projette** le domaine, **ne dérive pas**.
