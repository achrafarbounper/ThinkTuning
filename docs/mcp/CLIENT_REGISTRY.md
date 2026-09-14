# Registre opérationnel des clients MCP

Ce document définit les informations minimales à conserver pour chaque client
MCP autorisé. Les secrets ne doivent jamais être versionnés dans ce registre.

## Fiche client

| Champ | Description |
|---|---|
| `client_id` | Identifiant stable et non secret |
| `owner` | Équipe ou responsable |
| `environment` | `dev`, `staging` ou `production` |
| `scope` | `read_only`, `contributor`, `operator` ou `admin` |
| `allowed_tools` | Restriction additive/soustractive appliquée au client |
| `allowed_resources` | Resources accessibles |
| `event_granularity` | `minimal`, `summary` ou `verbose` |
| `status` | `active`, `suspended` ou `revoked` |
| `created_at` / `rotated_at` | Dates de cycle de vie de la clé |

## Règles de sécurité

- Les clés sont stockées dans le secret manager du déploiement, jamais dans
  Git, ce fichier ou les logs.
- Un client commence en `read_only` et reçoit uniquement les tools nécessaires.
- `admin` est réservé aux opérations explicitement approuvées.
- Toute rotation invalide l'ancienne clé après une période de grâce définie par
  l'exploitation.
- Toute révocation doit être auditée avec le `client_id`, sans journaliser la
  valeur de la clé.

## Provisionnement

Le provisionnement doit créer ou mettre à jour l'entrée client dans le store
MCP, vérifier les scopes effectifs, puis exécuter `tools/list` avec la clé du
client. Le catalogue retourné est la preuve de la surface réellement visible.

La validation de production doit également vérifier `initialize`, un appel
read-only, l'accès refusé à un tool hors scope et la reconnexion SSE avec
`after_sequence`.
