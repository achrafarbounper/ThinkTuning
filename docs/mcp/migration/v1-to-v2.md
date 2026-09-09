# Migration Guide: MCP v1.x → v2.0.0

> **Breaking change** — cette version introduit une nouvelle capacité
> (`sampling/create`) et un nouveau tool (`orchestrate`) qui nécessitent une
> mise à jour des clients MCP existants.

---

## Résumé des changements

| Changement | Type | Impact client |
|---|---|---|
| `SamplingPort` ajouté | **Breaking** — nouvelle capacité `sampling/create` | Les clients doivent gérer la nouvelle méthode JSON-RPC |
| Tool `orchestrate` ajouté | Additif — nouvel outil d'orchestration agentique | Découverte via `tools/list` (aucun code à changer) |
| Capacité `sampling` annoncée | **Breaking** — `initialize` retourne `capabilities.sampling` | Les clients doivent ignorer ou gérer cette capacité |
| Version bump `0.1.0` → `2.0.0` | **Breaking** — major version bump (SemVer) | Les clients versionnés doivent accepter `2.0.0` |

---

## 1. Breaking Change : `SamplingPort` / `sampling/create`

### Ce qui change

Le serveur MCP v2.0.0 agit désormais comme **client de son propre LLM** via la
méthode JSON-RPC `sampling/create`. Un client MCP peut demander une génération
de texte au serveur (reverse LLM inference).

### Impact

- **`initialize`** retourne désormais `capabilities: { sampling: {} }` — les
  clients qui valident strictement les capacités doivent accepter cette clé.
- **Nouvelle méthode** : `sampling/create` — les clients qui forwardent les
  requêtes doivent gérer cette méthode (ou la rejeter proprement).

### Migration

#### Option A : Ignorer le sampling (recommandé pour les clients simples)

Si votre client n'a pas besoin de la capacité sampling, ignorez simplement la
capacité `sampling` dans la réponse `initialize` et ne pas appeler
`sampling/create`.

```python
# Exemple : client Python qui ignore le sampling
def handle_initialize(response):
    capabilities = response.get("capabilities", {})
    # Ignorer la capacité sampling si présente
    if "sampling" in capabilities:
        logger.info("Le serveur supporte sampling/create (non utilisé par ce client)")
    # Continuer avec tools/resources/prompts
```

#### Option B : Supporter le sampling (clients avancés)

Si votre client souhaite utiliser la capacité sampling, implémentez la méthode
`sampling/create` :

```python
# Exemple : appel sampling/create
request = {
    "jsonrpc": "2.0",
    "id": 42,
    "method": "sampling/create",
    "params": {
        "messages": [
            {"role": "user", "content": "Analyse ce sentiment: J'aime ce produit"}
        ],
        "maxTokens": 256,
        "temperature": 0.7,
        "systemPrompt": "Tu es un assistant d'analyse de sentiments."
    }
}
response = mcp_client.send(request)
# response.result = {"content": {"text": "...", "type": "text"}, "model": "..."}
```

### Paramètres de `sampling/create`

| Paramètre | Type | Requis | Description |
|---|---|---|---|
| `messages` | `array` | **Oui** | Liste de messages `{role, content}` |
| `maxTokens` | `integer` | Non | Nombre max de tokens (défaut: 1024) |
| `temperature` | `number` | Non | Température 0.0-2.0 (défaut: 0.7) |
| `systemPrompt` | `string` | Non | Prompt système injecté en préfixe |

### Erreurs possibles

| Code | Message | Cause |
|---|---|---|
| `-32602` | `Invalid params: 'messages' (array) is required` | Paramètre manquant |
| `-32602` | `Invalid params: 'maxTokens' must be a positive integer` | Valeur invalide |
| `-32603` | `sampling/create not available (no SamplingPort wired)` | Port non configuré |
| `-32603` | `Sampling LLM error: ...` | Erreur du provider LLM |

---

## 2. Breaking Change : Capacité `sampling` dans `initialize`

### Ce qui change

La réponse `initialize` inclut désormais `capabilities.sampling: {}` :

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "protocolVersion": "2025-06-18",
    "capabilities": {
      "tools": {"listChanged": false},
      "resources": {"subscribe": false, "listChanged": false},
      "prompts": {"listChanged": false},
      "sampling": {}
    },
    "serverInfo": {"name": "thinktuning-mcp", "version": "2.0.0"}
  }
}
```

### Impact

Les clients qui valident strictement les capacités (whitelist) doivent ajouter
`sampling` à leur liste de capacités acceptées.

### Migration

```python
# Avant (v1.x)
ALLOWED_CAPABILITIES = {"tools", "resources", "prompts"}

# Après (v2.0.0)
ALLOWED_CAPABILITIES = {"tools", "resources", "prompts", "sampling"}
```

---

## 3. Additif : Tool `orchestrate`

Le tool `orchestrate` est ajouté à la surface v2.0.0+. Il est découvrable via
`tools/list` et ne nécessite **aucun changement** dans les clients existants.

---

## 4. Version bump : `0.1.0` → `2.0.0`

La version de la surface MCP passe de `0.1.0` à `2.0.0` (SemVer : breaking
changes → major bump). Les clients qui valident la version doivent accepter
`2.0.0`.

---

## 5. Checklist de migration

- [ ] Mettre à jour la liste des capacités acceptées (ajouter `sampling`)
- [ ] Mettre à jour la liste des versions supportées (ajouter `2.0.0`)
- [ ] Gérer la méthode `sampling/create` (ou l'ignorer proprement)
- [ ] Tester la connexion avec le nouveau serveur v2.0.0
- [ ] Mettre à jour la documentation client

---

## 6. Compatibilité ascendante

Les clients v1.x **continuent de fonctionner** avec le serveur v2.0.0 :

- Les méthodes `tools/list`, `tools/call`, `resources/*`, `prompts/*` restent
  inchangées.
- La capacité `sampling` est ignorée si le client ne la gère pas.
- Le tool `orchestrate` est découvrable via `tools/list` (aucun code à changer).

**Recommandation** : migrez vers v2.0.0 pour bénéficier de la capacité sampling
et de l'orchestration agentique.

---

## 7. Support

- **Documentation** : `docs/mcp/CHANGELOG.md` (section v2.0.0)
- **Issues** : [GitHub Issues](https://github.com/achrafarbounper/ThinkTuning/issues)
- **Gouvernance** : `docs/mcp/MCP_GOVERNANCE.md`
