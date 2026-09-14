# Runbook — Migration des durable-runs SQLite vers MongoDB

Ce runbook concerne uniquement les anciennes données MCP stockées dans
`mcp_durable_runs` et `mcp_durable_events`. La migration ne modifie jamais la
base SQLite source.

## Prérequis

- Exécuter la commande depuis `backend/`.
- Configurer `MONGODB_URI` et `MONGODB_DATABASE`.
- Vérifier que l'instance MongoDB cible utilise les mêmes règles d'accès que le
  service MCP.
- Arrêter les producteurs SQLite ou accepter que les nouveaux runs créés après
  le début de l'export restent à migrer lors d'une seconde passe.

## Prévisualisation

```powershell
python scripts/migrate_mcp_sqlite_to_mongo.py `
  --sqlite-path experiments/mcp_runs.db `
  --dry-run
```

La commande affiche un rapport JSON avec `runs_seen`, `runs_migrated`,
`runs_skipped` et `events_migrated`. En mode `dry-run`, MongoDB n'est pas
modifié.

## Migration

```powershell
python scripts/migrate_mcp_sqlite_to_mongo.py `
  --sqlite-path experiments/mcp_runs.db
```

Les identifiants de runs, les fingerprints, les états et les séquences
d'événements sont conservés. Les runs déjà présents dans MongoDB sont ignorés,
ce qui permet de relancer la commande après une interruption.

## Vérification

1. Comparer le rapport de migration avec le nombre de lignes SQLite.
2. Vérifier quelques runs avec le tool MCP `orchestrate_get_run`.
3. Vérifier le replay avec `orchestrate_events` et `after_sequence`.
4. Contrôler que l'index TTL `mcp_durable_events_created_at_ttl` est présent si
   `MCP_EVENT_RETENTION_DAYS` est supérieur à zéro.
5. Relancer la commande en `--dry-run` : les runs déjà migrés doivent apparaître
   dans `runs_skipped`, sans nouvelle écriture.

## Retour arrière (rollback)

La migration n'est pas destructive. Pour revenir à SQLite, rétablir le backend
SQLite dans la configuration de développement et conserver MongoDB intact.
Les événements créés uniquement dans MongoDB après la migration ne sont pas
recopiés automatiquement vers SQLite.

## Rétention et replay

MongoDB conserve les événements pendant 30 jours par défaut via
`MCP_EVENT_RETENTION_DAYS`. Une valeur `0` désactive le TTL. Un replay avec un
curseur plus ancien que la fenêtre de rétention peut donc retourner une
séquence incomplète.

## Smoke test MCP production

Avant et après la migration, exécuter depuis `backend/` :

```powershell
python scripts/smoke_mcp_production.py `
  --url https://<service>/mcp/sse `
  --api-key <clé>
```

Le script est strictement non destructif : il teste uniquement `initialize` et
`tools/list`, puis affiche le nombre de tools et la présence de `sampling`.
