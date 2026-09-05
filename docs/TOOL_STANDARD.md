# Standard des tools personnalisés — `thinktuning.tool/v1` (SCRUM-99)

Ce document spécifie le format de déclaration, le cycle de vie et les
garanties de sécurité des **tools personnalisés** du multi-agents
ThinkTuning. Tout ce qui est décrit ici est implémenté et testé :

| Module | Rôle |
|---|---|
| `ia/tools/tool_schema.py` | Standard v1 : validation, conversions, JSON Schema |
| `ia/tools/registry.py` | `ToolRegistry` — source de vérité unique |
| `ia/tools/custom_tools.py` | Tools d'exemple `run_shell` / `call_api` |
| `ia/agent/plan_validator.py` | Pseudo-rôle `propose_tool` (plans) |
| `ia/agent/orchestrator.py` | Pipeline proposer → relire (reviewer) |
| `ia/agent/approvals.py` | Gate : approval dérivé de `safety` |
| `api/routes/agent.py` | `GET/POST/DELETE /api/agent/tools/custom` |
| `core/feature_flags.py` | Flag `AGENT_CUSTOM_TOOLS_API` |

---

## 1. Définition (design-time)

```json
{
  "$schema": "thinktuning.tool/v1",
  "name": "get_weather",
  "description": "Météo courante d'une ville.",
  "version": "1.0",
  "category": "api",
  "required_args": ["city"],
  "parameters": {
    "city":  {"type": "string",  "required": true,  "description": "Ville."},
    "unit":  {"type": "string",  "required": false, "enum": ["c", "f"]}
  },
  "allowed_binaries": ["git", "python"],
  "safety": {"level": "restricted", "requires_approval": true}
}
```

Règles de validation (`validate_tool_definition`, déterministes) :

- `name` : regex `^[a-z][a-z0-9_]{1,63}$` (snake_case, 2–64) ;
- `description` : non vide ;
- `parameters` : types `string | number | integer | boolean | object | array` ;
- `required_args` : sous-ensemble des clés de `parameters` ;
- `version` : chaîne non vide ; `$schema` : `thinktuning.tool/v1` ;
- `category` : `os | api | db | ml | file | shell | network | custom | builtin` ;
- `safety.level` : `safe | restricted | dangerous` — `restricted`/`dangerous`
  exigent `requires_approval: true`.

**Séparation design-time / runtime** : `enabled`, `experimental`,
`deprecated`, `owner`, `source_file`, `dynamic` sont portés par
`RegisteredTool` et **jamais** sérialisés dans la définition.




## 2. Dérivation de sûreté (`safety` → `approval`)

| `safety.level` | `requires_approval` | `approval` effectif |
|---|---|---|
| `safe` | `false` | `auto` |
| `safe` | `true` (défaut) | `manual` |
| `restricted` | `true` | `manual` |
| `dangerous` | — | `blocked` (REJECT, insurmontable) |

Le gate `ia/agent/approvals.py` consulte cette classification pour tout tool
**dynamique** (priorité juste après l'override du manifeste, avant les
listes statiques) ; les tools **natifs** gardent leur politique historique.

## 3. `ToolRegistry` — source de vérité unique

Avant SCRUM-99, les dicts `TOOLS` / `TOOL_META` / `REQUIRED_ARGS` étaient
mutés en direct. Désormais :

1. la `ToolRegistry` **hydrate** sa collection depuis les dicts natifs au
   démarrage (outils marqués `dynamic=False` : non écrasables, non
   retirables) ;
2. `add_tool` / `remove_tool` sont les **seules** voies de mutation, et elles
   **projetent** l'état dans les dicts historiques (mutés par référence) :
   system_prompt, API `/tools`, AgentCore et `core/agent_cache` voient
   immédiatement les tools dynamiques sans modification ;
3. `plugin.py` enregistre désormais via la registry (fin de la mutation
   directe).

Garanties fail-closed de `add_tool` :

- un tool dynamique est `manual` **par défaut** — même s'il déclare
  `safe`/`auto` — sauf `allow_auto_approval=True` (réservé aux sources
  humaines : API) ;
- `safety.level = "dangerous"` ⇒ `blocked`, quelle que soit la source ;
- un tool natif n'est jamais écrasé ni retiré ; un duplicata exige
  `overwrite=True` ;
- plafond `max_dynamic_tools` (16 par défaut) contre l'inflation.

Écriture thread-safe (`RLock`) : les workers peuvent s'exécuter en parallèle
pendant un enregistrement.

## 4. Cycle de vie d'un tool personnalisé

```
Planner (lead)                    Reviewer (worker)          Humain (API)
──────────────                    ─────────────────          ────────────
plan {role: propose_tool,  ──▶   verdict JSON           ┌─▶ POST /tools/custom
 tool: {définition v1}}           approve | reject       │   (code + définition)
     │                                │                  │        │
     ▼                                ▼                  │        ▼
 agent.tool.proposed          agent.tool.reviewed      │   ToolRegistry
                              approved → décision      │   .add_tool()
                              HUMAINE (jamais auto) ───┘   agent.tool.registered
                              rejected → ToolProposalRejected (bucket failed)
```

- Le validateur de plan extrait les tâches `propose_tool` (plafond
  `max_tool_proposals` par plan, défaut 1) — elles ne sont **jamais**
  dispatchées comme workers ; un plan ne contenant que des propositions est
  valide.
- L'orchestrateur fait relire chaque proposition par le rôle `reviewer`
  (**aucun outil** : il juge, il ne peut rien exécuter) ; tout verdict
  illisible vaut `reject` (fail-closed).
- **L'orchestrateur n'enregistre jamais un tool.** Une proposition relue
  « approve » est retournée à l'humain dans `outcome["tool_proposals"]` ;
  l'enregistrement effectif (définition + code exécutable) passe par
  `POST /api/agent/tools/custom`, derrière le flag `AGENT_CUSTOM_TOOLS_API`
  et l'authentification API, avec audit. Codes de traçabilité :
  `ToolProposalRejected`, `ToolProposalLimited` (bucket `failed` : le plan
  continue sans le tool).
- Interrupteur global `USE_DYNAMIC_TOOLS=false` : mode legacy strict —
  aucun prompt de proposition, aucun traitement, aucun événement.

## 5. API (phase 1, flag `AGENT_CUSTOM_TOOLS_API`)

- `GET /api/agent/tools/custom` — tools dynamiques + état runtime ;
- `POST /api/agent/tools/custom` — corps : `{definition, code, owner,
  overwrite, allow_auto_approval}` ; le `code` Python doit définir une
  fonction du même nom que le tool ; erreurs : 422 (définition/code),
  409 (natif / duplicata sans `overwrite`), 400 (plafond) ;
- `DELETE /api/agent/tools/custom/{name}` — retrait d'un dynamique
  uniquement (409 pour un natif).

## 6. Rôles dédiés

| Rôle | Outils | Fonction |
|---|---|---|
| `operator` | `run_shell`, `call_api` | exécute des tools validés, jamais d'écriture de code |
| `developer` | — (aucun) | rédige code/définitions dans sa réponse ; n'enregistre jamais |
| `reviewer` | — (aucun) | relit une définition et rend un verdict argumenté |

## 7. Tools d'exemple

`read_file` et `write_file` (déjà natifs) sont documentés au standard v1 ;
`run_shell` (commande en liste d'arguments, allowlist
`AGENT_ALLOWED_BINARIES`, jamais de shell, timeout plafonné, `dry_run`) et
`call_api` (HTTP GET/POST générique, schéma http/https, sortie tronquée)
sont fournis dans `ia/tools/custom_tools.py` avec leurs définitions v1.

## 8. Tests

`tests/test_tool_schema.py`, `tests/test_tool_registry.py`,
`tests/test_custom_tools.py`, `tests/test_tool_proposal.py`,
`tests/test_multi_agent_tools.py` — offline, sans réseau, avec fakes LLM.
