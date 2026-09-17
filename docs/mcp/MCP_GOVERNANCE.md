# MCP Governance — Product Council + Processus

> **Statut (septembre 2026)** — Processus de gouvernance actif pour les
> changements MCP ; les artefacts de réunions mentionnés peuvent être
> historiques ou à créer.

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

---

## 🗑️ §5 — Politique de retrait des tools (MCP 2.3.0)

> Source de vérité du cycle de vie d'un tool : `tools_config.json`
> (standard `thinktuning.tool/v1`). Le manifeste compilé
> (`docs/mcp/MANIFEST.md`) et la surface `tools/list` la projettent.

### 5.1 Cycle de vie

```
ACTIVE → DEPRECATED → SUNSET → RETIRÉ
        (audité)      (retrait effectif)
```

| Étape | Où | Effet |
|---|---|---|
| **ACTIVE** | `tools_config.json` sans métadonnées de retrait | Usage normal (aucun champ de retrait dans le détail d'audit). |
| **DEPRECATED** | `deprecated: true` (+ `deprecationMessage`, `sunsetAt`) | Le tool reste **appelable** (compatibilité clients) : `tools/list` expose `deprecated: true` / `deprecationMessage` / `sunsetAt`, un avertissement est loggé à chaque appel et un événement d'audit dédié `mcp_tool_deprecated` est émis (en plus de `mcp_tool_call` enrichi). |
| **SUNSET** | `sunsetAt` dépassé | Le mainteneur retire le tool (RFC + changelog) ; `tools/call` répond alors `Unknown tool` (erreur indiscernable d'un tool absent — aucune fuite d'oracle). |
| **RETIRÉ** | entrée supprimée de `tools_config.json` | Le catalogue est régénéré ; les clients ayant suivi le `deprecationMessage` ont migré. |

### 5.2 Règles de dépréciation

1. **Toujours annoncer avant de retirer** : un tool ne passe JAMAIS d'`ACTIVE`
   à « retiré » en une release. Période de dépréciation minimale : **2 releases
   mineures** ou **90 jours**, le plus long des deux.
2. **Toujours fournir un chemin de migration** : `deprecationMessage` nomme le
   remplacement (`"Utiliser <nouveau_tool>"`) et, si applicable, le guide de
   migration (`docs/mcp/migration/`).
3. **Toujours fixer une date** : `sunsetAt` (ISO 8601) rend le retrait
   prévisible ; un tool déprécié sans date est rappelé au Council (warning de
   compilation : « date non fixée »).
4. **Compatibilité d'abord** : la dépréciation ne change NI le contrat
   d'appel, NI la réponse (aucune erreur nouvelle pour les clients existants).
5. **Auditabilité** : chaque usage d'un tool déprécié est tracé
   (`mcp_tool_deprecated`) — la courbe d'usage décroissante confirme que les
   clients ont migré avant le retrait.

### 5.3 Checklist de retrait effectif

- [ ] `sunsetAt` dépassé ET usage audité ≈ 0 sur les 30 derniers jours
- [ ] RFC de retrait acceptée (breaking change)
- [ ] Changelog (section `Removed`) + notification clients (email)
- [ ] `tools_config.json` nettoyé → `MANIFEST.md` régénéré
  (`python -m app.infrastructure.mcp.manifest_generator`, depuis `backend/`)
- [ ] Guide de migration archivé dans `docs/mcp/migration/`

### 5.4 Déclaration (exemple)

```json
{
  "tools": {
    "old_tool": {
      "name": "old_tool",
      "description": "Ancien tool",
      "deprecated": true,
      "deprecationMessage": "Utiliser new_tool (guide : docs/mcp/migration/old_tool.md)",
      "sunsetAt": "2027-06-01"
    }
  }
}
```
