# MCP Security — Scopes, Quotas, Revocation, Audit

> **Document de sécurité** (versionné et audit-trail)

---

## 🔐 MCPSecurityScope

Chaque client MCP est **toujours associé** à un scope.  
Le scope **limite** ce que le client peut voir et faire.

```python
# app/domain/ports/mcp_ports.py (nouveau)
class MCPSecurityScope(BaseModel):
    client_id: str             # ex: "claude-desktop-prod"
    tenant_id: str             # default | staging | production
    role: Literal[
        "read_only",           # 12 tools — lecture seulement
        "contributor",         # 25 tools — write filtré (approval)
        "operator",            # 35 tools — exec filtré (approval)
        "admin",               # 40 tools — full access
    ]
    visible_tools: list[str]   # whitelist des tools visibles
    visible_resources: list[str]  # whitelist des URI patterns
    visible_prompts: list[str]
    sampling_enabled: bool    # False par défaut → True pour "operator"
    rate_limit_per_minute: int # 60 default | 600 admin | 1200 CI
    destructive_quota: int    # tools "manual approval" max/heure (5 default)
    revoked: bool = False
    revoked_at: datetime | None = None
    revoked_reason: str = ""
```

---

## 📊 Audit MCP

Chaque appel MCP est **tracé** via `core/audit_store.py`.

```python
# Nouveaux events
ACT_MCP_TOOL_CALL = "mcp_tool_call"
ACT_MCP_RESOURCE_READ = "mcp_resource_read"
ACT_MCP_PROMPT_GET = "mcp_prompt_get"
ACT_MCP_SAMPLING = "mcp_sampling"
ACT_MCP_ORCHESTRATE = "mcp_orchestrate"

# Exemple d'audit
audit_log(ACT_MCP_TOOL_CALL,
    subject=client_id,
    detail={
        "tool": tool_name,
        "scope": scope_id,
        "args_hash": sha256(args),
        "decision": policy_decision,  # auto / manual / rejected
    },
    run_id=mcp_request_id)
```

→ Loggué dans la même table `agent_audit` que `core/audit_store.py`.
→ **Traçabilité** : client_id → tool → args → policy → run_id.

---

## 🔁 Revocation Client MCP

```python
# core/mcp_client_store.py
class MCPClientStore:
    def revoke(self, client_id: str, reason: str): ...
    def list(self) -> list[MCPClient]: ...
    def metrics(self, client_id: str) -> dict: ...
    # → call_count, error_rate, scope_usage, last_requests
```

**Triggers de révocation automatique** :
- 10 rejections de policy par minute (tentative d’abuser)
- 50 erreurs de sandbox par minute
- Taux d’erreur > 20% pendant 5 minutes

→ Un client **compromis ou abusif** → **révoqué en 1 appel** → 401 sur le prochain appel.

---

## 🛡️ Politique de Sécurité Interne (fail-closed)

| Client Scope | Tools visibles | Approval policy | Notes |
|---|---|---|---|
| `read_only` | 12 tools lecture | AUTO_APPROVE | pas de sampling |
| `contributor` | 25 tools + write | APPROVE (humain) | sampling = false |
| `operator` | 35 tools + exec | APPROVE (humain) | sampling = true |
| `admin` | 40 tools full | AUTO (sauf dangerous) | sampling = true |

→ **Aucun client** ne peut appeler un tool `dangerous` (shell, SQL mutate).
→ **Tous** les tools passent par `app/agent/policies/sandbox_policy.py`.
→ MCP n’est **pas** un bypass de la security interne.

---

## 📈 Observabilité MCP (Dashboard interne)

| Métrique | Source | Alerte |
|---|---|---|
| MCP error rate | `audit_store` | > 5% → Slack `#mcp-ops` |
| MCP call volume | `audit_store` | spike 5x → alert |
| Revoked clients | `mcp_client_store` | auto → email Security Officer |
| Scope violations | `sandbox_policy` | REJECT → log + alert |
| Sampling abuse | `AuditSampling` | > 100 samplings/h → quota réduit |
