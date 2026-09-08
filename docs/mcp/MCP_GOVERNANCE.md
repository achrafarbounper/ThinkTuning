# MCP Governance — Product Council + Processus

> **Document de gouvernance** (versionné en parallèle du code MCP)

---

## 👥 Le MCP Product Council

| Rôle | Responsabilité | Décision clé |
|---|---|---|
| **MCP Product Owner** | Manifeste MCP, versioning, roadmap | Quel tool/resource/prompt est exposé? Quand une version majeure? |
| **MCP Security Officer** | Scopes, quotas, revocation, audit | Qui peut faire quoi? Quand un client est révoqué? |
| **MCP Client Liaison** | Feedback clients, migration, documentation | Quels clients sont impactés? Qui migre? |

→ **Un seul décideur par décision.** Rôle = décision finale.
→ Le Council se réunit **chaque mardi 10h**. Procès-vercal public dans `docs/mcp/meetings/`.

---

## 🔄 Processus Obligatoires

### 1. RFC MCP (`docs/mcp/rfc/`)

Chaque **nouveau tool**, **resource**, **prompt** ou **breaking change** → **RFC**.

**Template** : `docs/mcp/rfc/RFC_TEMPLATE.md`

Exemple de RFC : `docs/mcp/rfc/0001-expose-predict-sentiment-tool.md`

→ Merge en Draft → Review par le Council → Accepted/Rejected.

---

### 2. Changelog MCP (`docs/mcp/CHANGELOG.md`)

**Format** : Keep a Changelog + SemVer.

```markdown
## v1.1.0 — 2026-09-15
### Added
- Resource: `thinktuning://jobs/{job_id}`
- Prompt: `summarize-job`
### Changed
- Tool `predict_sentiment` → `analyze_sentiment` (alias deprecated)
### Security
- Scope `read_only` → 12 tools visibles (was 8)
- Client `cursor-dev-abc` → rate limit 600/min (was 60)
```

→ Chaque release MCP → **changelog publié**.
→ Le changelog est **vérifié par CI** (`tests/test_mcp_changelog.py`).

---

### 3. Client Registry (`docs/mcp/CLIENT_REGISTRY.md`)

```
| Client | ID | Tenant | Role | Visible Tools | Quotas | Last Seen | Status |
|---|---|---|---|---|---|---|---|
| Claude Desktop | claude-desktop-prod | default | contributor | 25 | 100/min | 2026-09-08 | 🟢 active |
| Cursor CI | cursor-ci-staging | staging | read_only | 12 | 600/min | 2026-09-07 | 🟡 warning |
| Agent Custom | custom-dev-abc | default | operator | 35 | 5 destructive/h | 2026-09-08 | 🔴 revoked |
```

→ Le registry est **géré via API MCP** (`POST /mcp/clients`) — authentifié par API key admin.
→ Un client **révoqué** → **401 sur le prochain appel**.

---

## 📋 Checklist Avant Chaque Release MCP

- [ ] Changelog rédigé et validé
- [ ] RFC merge pour chaque nouveau tool/resource/prompt
- [ ] Tests de conformité MCP passent (`tests/test_mcp_manifest.py`)
- [ ] Audit trail vérifié (pas de fuite de données)
- [ ] Clients impactés notifiés (CHANGELOG + email)
- [ ] Migration guides publiés (`docs/mcp/migration/`)
