# MCP Competitive Advantage — Pourquoi ThinkTuning MCP ?

> ThinkTuning n’est pas un serveur MCP générique.  
> ThinkTuning est **le seul serveur MCP avec un orchestrateur ML agentique + fail-closed + sandbox**.

---

## 🎯 Positionnement

> **ThinkTuning MCP** = un serveur MCP qui **orchestre des flux ML agentiques** avec une **sécurité fail-closed**.

---

## 🆚 Comparaison Concurrentielle

| Serveur MCP | Ce qu’il fait | ThinkTuning différencie par |
|---|---|---|
| `filesystem` MCP | read/write files | ✅ Sandbox fail-closed + anti-évasion (`.git`, `.env` bloqués) |
| `sqlite` MCP | SQL query | ✅ `PRAGMA query_only` + REJECT mutatif (INSERT/UPDATE/DELETE) |
| `git` MCP | git commands | ✅ Allowlist binaires + timeout + sandbox path resolution |
| `brave-search` MCP | web search | ✅ SearXNG + DDG + anti-SSRF + retry + circuit breaker LLM |
| `postgres` MCP | SQL query | ✅ Lecture seule par défaut + anti-SSRF |
| `math` MCP | calculator | ✅ `calc` (safe eval) + `add` (exact) |
| **`thinktuning-mcp`** | **ML agentic platform** | ✅ **Orchestrateur + fail-closed + ML tools + sandbox + multi-tenant + sampling** |

---

## 💎 Les 3 Différenciateurs Uniques

### 1. Fail-Closed Security (inégalé)
- Sandbox path resolution — **aucune sortie de racine** (`safe_resolve`)
- Binary allowlist — `AGENT_ALLOWED_BINARIES` (pas de shell)
- Anti-SSRF — `AGENT_BLOCK_PRIVATE_HOSTS` + private host allowlist
- Mutating SQL blocked — seuls `SELECT/WITH/EXPLAIN/PRAGMA` passent
- Dangerous paths blocked — `.git`, `.env`, `id_rsa`, `node_modules` en écriture/refus
- Budget plafonné — `agent_max_llm_rounds` + `agent_max_tool_calls` + anti-boucle SHA-256

> Aucun serveur MCP concurrent n’a une telle couche de sécurité intégrée.

### 2. Orchestrateur ML Agentique Interne
- Tool MCP `orchestrate(prompt)` → déclencheur `AgentCore` → `Intent → Plan → Policy → Budget → Action`
- Workflow complexe : un agent MCP peut déléguer une tâche → ThinkTuning orchestre en interne
- Exemple : un agent MCP dit “analyze ce dataset” → MCP tool `orchestrate` → ThinkTuning lance le pipeline EDA + predict + rapport

> Aucun serveur MCP concurrent n’a un orchestrateur interne aussi puissant.

### 3. Tools ML Spécialisés (uniques)
- `predict_sentiment` — DistilBERT fine-tuné FR/EN (CPU OK)
- `model_versions` — versions trackées + active pointer
- `dataset_stats` — profiling CSV/TSV/JSONL (pandas intégré)
- `start_training` / `stop_training` — jobs d’entraînement en background
- `job_list` / `job_get` — monitoring SQLite read-only strict

> Aucun serveur MCP concurrent n’expose des **tools ML spécialisés** dans un format standardisé.

---

## 🧭 Vision Produit

> ThinkTuning MCP = la **plateforme agentique ML** qu’un LLM (Claude, Cursor, agent custom) peut **composer** via MCP.

Pas un outil. Pas un backend.  
Une **platform de capabilities ML agents**.

---

## 📢 Message pour les Users MCP

> “ThinkTuning MCP = le seul serveur MCP qui vous permet de **faire de l’IA générative appliquée à du ML réel** — avec une sécurité industrialisée.”

→ Pas de prompt hacking → le LLM ne peut **jamais** faire d’écrire sur `.git`.  
→ Pas de SQL injection → le LLM ne peut **jamais** faire `DROP TABLE`.  
→ Pas de shell → le LLM ne peut **jamais** exécuter `rm -rf`.  
→ Et pourtant, il peut lancer un entraînement, prédire, analyser.
