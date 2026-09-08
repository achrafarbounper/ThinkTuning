# MCP Roadmap — Versionnée (SemVer)

> **Trajectoire** : `v0.1.0` → `v3.0.0`  
> **Target** : MCP = surface principale de ThinkTuning

---

## 📆 Plan de Version

| Version | Nom | Outils | Resources | Prompts | Sampling | Orchestrate | Date Cible |
|---|---|---|---|---|---|---|---|
| **v0.1.0** | Bootstrap | 12 read-only tools | 0 | 0 | ✗ | ✗ | 2026-09-08 |
| **v1.0.0** | Public Beta | 25 tools (read-only) | 5 `thinktuning://` | 2 prompts | ✗ | ✗ | 2026-09-30 |
| **v1.1.0** | Resources | 25 tools | 10 resources | 3 prompts | ✗ | ✗ | 2026-10-15 |
| **v1.2.0** | Prompts | 25 tools | 10 resources | 5 prompts | ✗ | ✗ | 2026-10-30 |
| **v2.0.0** | Sampling | 30 tools | 12 resources | 6 prompts | ✅ | ✗ | 2026-11-15 |
| **v2.1.0** | Orchestrate | 35 tools | 12 resources | 6 prompts | ✅ | ✅ | 2026-12-01 |
| **v3.0.0** | MCP-First | 40 tools | 15 resources | 8 prompts | ✅ | ✅ | 2027-01-15 |

---

## 🎯 Détails par Version

### v1.0.0 — Public Beta (première surface officielle)
- **Outils** : 25 tools read-only (web_search, read_file, calc, job_list, predict_sentiment, model_versions, etc.)
- **Resources** : 5 `thinktuning://` URIs (jobs, models, datasets, config, metrics)
- **Prompts** : `analyze-sentiment`, `plan-training`
- **Scope** : `read_only` + `contributor` (write = blocked par défaut)
- **Clients** : inscription manuelle via `POST /mcp/clients`
- **Audit** : `ACT_MCP_*` activé

### v2.0.0 — Sampling (breaking change)
- **Nouveau** : `SamplingPort` — clients MCP peuvent demander LLM inference → `HttpLLMClient`
- **Breaking** : clients doivent mettre à jour pour le nouveau capability
- **Migration guide** : `docs/mcp/migration/v1-to-v2.md`

### v3.0.0 — MCP-First
- **HTTP API /api/agent/* devient legacy** (mode read-only support)
- **Dashboard migre vers MCP-over-SSE**
- **MCP = source de vérité pour la surface**

---

## ⚠️ Conditions de Passage de Version

| De → Vers | Condition |
|---|---|
| v1.0 → v1.1 | 0 divergence (`MCPCapabilitySync`) pendant 30 jours + 3 migration guides |
| v1.2 → v2.0 | 0 incident de sécurité MCP + 80% des clients notifiés |
| v2.1 → v3.0 | 80% du traffic via MCP + HTTP API en mode legacy validé |

→ Chaque **breaking change** = **version majeure**.
→ Chaque **feature additive** = **version mineure**.
→ Chaque **bug fix** = **version patch**.

---

## 🌱 Innovation Pipeline

Chaque mois, le Council évalue :
- 3 nouvelles features MCP candidates
- 1 breaking change à planifier
- 1 client à révoquer (policy abuse)

→ Priorisé via **impact client** × **complexité sécurité**.
