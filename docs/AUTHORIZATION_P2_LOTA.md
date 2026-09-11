# Autorisation agentique — P2 Lot A (PDP Casbin embarquée)

Ce document décrit la **refonte de l'autorisation métier des outils
agentiques** (Lot A du plan P2 « Auth moderne »). Il est livré indépendamment
de l'authentification (Lot B) et des secrets (Lot C) : le lot A ne change
PAS le mode d'authentification existant, il introduit un **point de décision
de politique (PDP)** deny-by-default derrière un feature flag, avec rollback
réversible.

## Objectif

Aucune identité authentifiée (ni l'agent, ni un service account, ni un client
MCP) ne pourra exécuter un outil sans une **décision d'autorisation explicite,
contextualisée et auditée**. En mode `strict`, toute capacité non déclarée est
refusée ; en mode `legacy_permissive` (défaut), le comportement historique est
conservé et la PDP est évaluée en *shadow* (auditée, non contraignante) pour
mesurer l'impact avant le basculement.

## Activation / configuration

| Variable | Rôle | Défaut |
|---|---|---|
| `AGENT_SECURITY_AUTHZ_CASBIN` | Active la PDP (1/true/yes/on) | désactivé |
| `AGENT_AUTHZ_TENANT` | Tenant des runs agent (isolation par env) | `default` |

Rollback = désactiver la variable (`AGENT_SECURITY_AUTHZ_CASBIN` absent ou
`0`) et redémarrer le worker : aucune donnée persistante à purger, la PDP est
purement en mémoire.

## Architecture

```
app/domain/authorization.py            # valeur objets PURS (AuthzRequest,
                                       #   AuthzDecision, TenantMode) — aucun
                                       #   couplage Casbin
app/domain/ports/authorization_ports.py# port PolicyDecisionPoint (domaine)
app/infrastructure/security/authz/
  policy_document.py                   # format de politique VERSIONNÉ + validation
  policies/default_policy.json         # politique par défaut (versionnée en git)
  tool_capabilities.py                 # outils -> catégorie (source : sandbox_policy)
  casbin_pdp.py                        # PDP Casbin embarquée (par process)
  enforcer.py                          # modes tenant + audit unifié
  factory.py                           # singleton par process, flag-driven
```

Règles d'or (fail-closed) :

1. toute action **non déclarée** → `tool.execute:unknown` → refus en `strict` ;
2. une PDP **indisponible** n'autorise jamais (déni explicite, audité) ;
3. les décisions portent `policy_version` : l'audit sait quelle politique a
   jugé, et le rollback de politique est traçable ;
4. les **règles dures sandbox** (chemins sensibles, SQL mutant, anti-SSRF)
   restent actives quelle que soit la PDP — défense en profondeur.

## Modes de tenant

| Mode | Effet |
## Format de politique (versionné)

```json
{
  "policy_version": 1,
  "default_tenant_mode": "legacy_permissive",
  "tenant_modes": { "default": "legacy_permissive", "prod": "strict" },
  "role_aliases": { "read_only": "viewer", "contributor": "operator" },
  "roles": {
    "viewer":   ["tool.list", "tool.read:*", "tool.execute:read", "tool.execute:system"],
    "operator": ["tool.list", "tool.read:*", "tool.execute:*"],
    "admin":    ["tool.list", "tool.read:*", "tool.execute:*", "admin.*"],
    "agent":    ["tool.list", "tool.read:*", "tool.execute:read", "tool.execute:system",
                 "tool.execute:network", "tool.execute:write", "tool.execute:delete",
                 "tool.execute:exec"],
    "service":  ["tool.list", "tool.read:*", "tool.execute:read", "tool.execute:system"]
  },
  "denies": [
    { "role": "*", "action": "tool.execute:unknown", "resource": "*",
      "reason": "capacité d'outil non déclarée (deny-by-default)" }
  ]
}
```

- `policy_version` s'incrémente à chaque évolution (audit + rollback) ;
- les patterns supportent les jokers glob (`*`, `tool.execute:*`) ;
- les `denies` sont **prioritaires** sur toute autorisation ;
- les rôles MCP (`read_only`, `contributor`, …) sont mappés vers les rôles
  canoniques via `role_aliases` (aucune duplication de règles).

## Registre de capacités des outils

La source de vérité du classement outil→catégorie est `sandbox_policy.
classify_tool` (déjà audité, déjà testé, cache LRU). `tools_config.json`
déclare les outils ; un outil **non déclaré** retombe dans `tool.execute:
unknown` (jamais autorisé en strict). Le champ optionnel `safety.level:
safe` est vérifié en cohérence : un outil déclaré `safe` mais classé
write/delete/exec par la policy est signalé (dérive) — la policy gagne.

## Boucle agent (couture)

Dans `app/agent/core.py::_execute_plan`, après le verdict `sandbox_policy`
(`decide_action`) et avant exécution :

- le gate d'autorisation est résolu **une fois par run**
  (`_resolve_authz_gate`), flag-driven ;
- en `strict`, un refus PDP force `Decision.REJECT` : la trace porte
  `policy_version` + `authz_reason`, l'anti-boucle `rejected_prints`
  s'applique, et l'outil n'est **jamais** exécuté ;
- en `legacy_permissive`, la décision est auditée mais non contraignante.

## Audit

Chaque décision émet `agent.authz_decision` sur l'event bus :

```json
{ "tool": "write_file", "subject": "agent", "tenant": "default",
  "action": "tool.execute:write", "category": "write", "args_hash": "…",
  "source": "agent", "allowed": true, "fail_closed": false, "enforced": false,
  "mode": "legacy_permissive", "policy_version": 1, "rule": "role:agent",
  "reason": "autorisation accordée par la politique" }
```

Seul le **hash** des arguments est journalisé (jamais les valeurs) — l'audit
d'autorisation est anonymisé par construction.

## Performance

La PDP est **embarquée par process** (aucune I/O réseau par décision). Le
budget cible du plan P2 est un p95 local < 5 ms ; mesuré en moyenne ≈ 0,3 ms
par décision (voir `tests/test_authz_casbin_pdp.py::test_decision_latency_
under_budget`).

## Tests

| Fichier | Couvre |
|---|---|
| `tests/test_authz_policy_document.py` | validation strict du format (version, mode, denies), fingerprint |
| `tests/test_authz_casbin_pdp.py` | matrice rôles×actions, fail-closed, déterminisme, latence |
| `tests/test_authz_enforcer.py` | modes tenant, shadow, audit, gate par run, alias MCP |
| `tests/test_agent_authz_integration.py` | couture `core.py` : flag off/on, strict, shadow, règles dures |

## Hors périmètre (lots suivants)

- JWT / sessions / service accounts (Lot B) ;
- `SecretProvider` / Infisical / audit de secrets (Lot C) ;
- PDP distante OPA/Rego (alternative documentée, non implémentée).
|---|---|
| `legacy_permissive` | Comportement historique conservé. La PDP est évaluée et auditée (event `agent.authz_decision`) mais **non contraignante**. |
| `strict` | deny-by-default **effectif** : seule une règle explicite de la politique autorise l'action. |

Le mode est résolu par tenant (`tenant_modes`), le tenant inconnu hérite de
`default_tenant_mode`. Un mode strict par défaut rend le système fail-closed
même pour un tenant non provisionné.