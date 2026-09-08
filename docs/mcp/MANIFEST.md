# Manifeste MCP ThinkTuning — v0.1.0

> ⚠️ **FICHIER GÉNÉRÉ** — ne pas éditer à la main.
> Source : `ia/tools/tools_config.json` (standard `thinktuning.tool/v1`) ;
> régénération : `python -m app.infrastructure.mcp.manifest_generator` (depuis `backend/`).

| Attribut | Valeur |
|---|---|
| Serveur | `thinktuning-mcp` |
| Version surface | 0.1.0 |
| Protocole MCP | 2025-06-18 |
| Tools | **57** (read-only : **33** · mutation : **24**) |
| Généré le | 2026-09-08T17:11:14.953Z |
| Avertissements | 7 |

## Catalogue

Posture (annotations MCP `tools/list`) : `readOnly` = readOnlyHint,
`destructive` = destructiveHint, `idempotent` = idempotentHint.
`Scope requis` = rôle minimal pour VOIR le tool (`MCPScopeRole`,
docs/mcp/MCP_SECURITY.md) — hint design-time ; la policy runtime
(tâche 5) reste le garde-fou effectif à chaque appel.

| Tool | Scope requis | readOnly | destructive | idempotent | Catégorie | Description |
|---|---|:-:|:-:|:-:|---|---|
| `add` | read_only | ✅ | — | ✅ | builtin | — |
| `append_file` | contributor | — | ✅ | — | builtin | Ajoute `content` Ã la fin d'un fichier DANS la sandbox (crÃ©e le fichier et ses parents si nÃ©cessaire â€” co… |
| `calc` | read_only | ✅ | — | ✅ | builtin | Ã‰value une expression arithmÃ©tique pure et renvoie {expression, result} |
| `call_api` | contributor | — | ✅ | — | builtin | Appel HTTP générique GET/POST vers une API externe (schéma http/https, sortie tronquée). GET sans corps ; POS… |
| `cancel_training` | admin | — | ✅ | — | builtin | Demande l'arrÃªt d'un entraÃ®nement en cours (pending/running) via son job_id ; le thread s'arrÃªte au procha… |
| `copy_path` | contributor | — | ✅ | — | builtin | Copie fichier ou arborescence dans la sandbox |
| `count_lines` | read_only | ✅ | — | ✅ | builtin | DÃ©compte lignes, mots, caractÃ¨res et octets (Ã©quivalent `wc`) |
| `dataset_stats` | read_only | ✅ | — | ✅ | builtin | Profil rapide d'un dataset CSV/TSV/JSONL sous la sandbox : lignes, colonnes, valeurs manquantes, distribution… |
| `dedupe_lines` | contributor | — | ✅ | — | builtin | Supprime les lignes dupliquÃ©es d'un fichier (dans la sandbox) |
| `disk_usage` | read_only | ✅ | — | ✅ | builtin | Espace disque libre + taille des enfants directs d'un dossier sandbox (les entraÃ®nements meurent silencieuse… |
| `docker_exec` | operator | — | ✅ | — | builtin | ExÃ©cute `command` (chaÃ®ne, via `sh -c`) dans le conteneur |
| `docker_logs` | read_only | ✅ | — | ✅ | builtin | DerniÃ¨res `tail` lignes de logs d'un conteneur (stdout + stderr) |
| `docker_ps` | read_only | ✅ | — | ✅ | builtin | Liste les conteneurs (un objet JSON par conteneur, format `docker ps`) |
| `docker_stats` | read_only | ✅ | — | ✅ | builtin | Consommation CPU/RAM par conteneur (`docker stats --no-stream`, un objet JSON par conteneur â€” mÃªme convent… |
| `download_file` | contributor | — | ✅ | — | builtin | TÃ©lÃ©charge un fichier http(s) DANS la sandbox, en streaming |
| `env_info` | read_only | ✅ | — | ✅ | builtin | Diagnostic lecture seule : Python, OS, CPU et versions des packages clÃ©s |
| `file_checksum` | read_only | ✅ | — | ✅ | builtin | Empreinte (hash) d'un fichier : md5, sha1, sha256 (dÃ©faut) ou sha512 |
| `file_info` | read_only | ✅ | — | ✅ | builtin | MÃ©tadonnÃ©es d'un fichier ou dossier (type, taille, dates, encodage, lignes) |
| `find_duplicates` | admin | — | ✅ | — | builtin | DÃ©tecte les fichiers au contenu identique (par empreinte) sous `path` |
| `find_file` | read_only | ✅ | — | ✅ | builtin | Cherche rÃ©cursivement les fichiers/dossiers dont le chemin relatif ou le nom correspond Ã `pattern` (regex P… |
| `git_diff` | read_only | ✅ | — | ✅ | builtin | `git diff` (index <-> travail), optionnellement `--cached` et restreint Ã un chemin de la sandbox |
| `git_log` | read_only | ✅ | — | ✅ | builtin | `git log --oneline` des N derniers commits (limit plafonnÃ© Ã 100) |
| `git_status` | read_only | ✅ | — | ✅ | builtin | `git status --short --branch` sur le dÃ©pÃ´t de la racine sandbox |
| `gpu_info` | read_only | ✅ | — | ✅ | builtin | Ã‰tat GPU complet : disponibilitÃ© CUDA, VRAM, utilisation |
| `head_file` | read_only | ✅ | — | ✅ | builtin | PremiÃ¨res lignes d'un fichier texte (pendant symÃ©trique de tail_file) |
| `http_get` | read_only | ✅ | — | ✅ | builtin | GET HTTP : renvoie {status, reason, url, content_type, body tronquÃ©} |
| `http_post` | contributor | — | ✅ | — | builtin | POST HTTP : corps brut (`data`) ou JSON (`json_payload`), mutuellement exclusifs |
| `job_get` | read_only | ✅ | — | ✅ | builtin | Charge le payload COMPLET d'un job (hyperparamÃ¨tres, erreur, chemin modÃ¨le) |
| `job_list` | read_only | ✅ | — | ✅ | builtin | Liste les jobs d'entraÃ®nement les plus rÃ©cents (lecture seule) |
| `list_dir` | read_only | ✅ | — | ✅ | builtin | Liste un rÃ©pertoire (dossiers d'abord, puis fichiers, ordre alphabÃ©tique) |
| `make_dir` | contributor | — | ✅ | — | builtin | CrÃ©e un rÃ©pertoire (parents inclus, sans erreur s'il existe dÃ©jÃ ) |
| `model_versions` | read_only | ✅ | — | ✅ | builtin | Liste les versions de modÃ¨les entraÃ®nÃ©s visibles dans la sandbox (mÃªmes conventions que core/model_versio… |
| `move_path` | contributor | — | ✅ | — | builtin | DÃ©place/renomme fichier ou rÃ©pertoire dans la sandbox |
| `now` | read_only | ✅ | — | ✅ | builtin | Horodatage courant ISO lisible ('2026-08-25 14:03:27+00:00') |
| `postgres_query` | read_only | ✅ | — | ✅ | builtin | ExÃ©cute une requÃªte SQL sur PostgreSQL |
| `predict_sentiment` | read_only | ✅ | — | ✅ | builtin | PrÃ©dit le sentiment (positive/neutral/negative) d'une liste de textes FR/EN avec le modÃ¨le courant |
| `read_file` | read_only | ✅ | — | ✅ | builtin | Lit un fichier texte (UTF-8) ; tronque au-delÃ de max_bytes |
| `read_json` | read_only | ✅ | — | ✅ | builtin | Lit et parse un fichier JSON ; message d'erreur prÃ©cis si invalide |
| `remove_path` | contributor | — | ✅ | — | builtin | Supprime fichier ou rÃ©pertoire |
| `run_command` | operator | — | ✅ | — | builtin | ExÃ©cute une commande en liste d'arguments, ex : ["git", "--version"] |
| `run_python` | operator | — | ✅ | — | builtin | ExÃ©cute un extrait Python dans un sous-processus fraÃ®chement crÃ©Ã© |
| `run_shell` | admin | — | ✅ | — | builtin | Exécute une commande SÛRE en liste d'arguments (allowlist AGENT_ALLOWED_BINARIES, jamais de shell, timeout pl… |
| `search_in_files` | read_only | ✅ | — | ✅ | builtin | Cherche `pattern` (regex Python, insensible Ã la casse) dans le contenu des fichiers sous `path` |
| `split_file` | contributor | — | ✅ | — | builtin | DÃ©coupe un gros fichier en morceaux numÃ©rotÃ©s (max_lines lignes chacun) |
| `sqlite_query` | read_only | ✅ | — | ✅ | builtin | ExÃ©cute une requÃªte SQL sur une base SQLite situÃ©e dans la sandbox |
| `start_training` | admin | — | ✅ | — | builtin | Lance un entraÃ®nement en arriÃ¨re-plan (mÃªme mÃ©canique que POST /train) et retourne immÃ©diatement le job_… |
| `stop_training` | admin | — | ✅ | — | builtin | Alias de cancel_training : demande l'arrÃªt propre d'un entraÃ®nement en cours (pending/running) via son job_… |
| `tail_file` | read_only | ✅ | — | ✅ | builtin | DerniÃ¨res `lines` lignes d'un fichier texte (lecture arriÃ¨re bornÃ©e Ã 256 Ko : adaptÃ© aux logs qui grossi… |
| `touch` | contributor | — | ✅ | — | builtin | CrÃ©e un fichier vide ou rafraÃ®chit sa date de modification (sans Ã©craser) |
| `train_model` | admin | — | ✅ | — | builtin | Lance un entraÃ®nement et ATTEND sa fin (bloquant, timeout en secondes) ; retourne le statut final, le chemin… |
| `unzip_file` | contributor | — | ✅ | — | builtin | Extrait une archive .zip de la sandbox vers un dossier de la sandbox |
| `web_fetch` | read_only | ✅ | — | ✅ | builtin | RÃ©cupÃ¨re une page distante : {status, reason, url, content_type, title, body} |
| `web_read` | read_only | ✅ | — | ✅ | builtin | Lit une page web et en extrait le TEXTE lisible (sans HTML) |
| `web_search` | read_only | ✅ | — | ✅ | builtin | Recherche web : SearXNG auto-hébergée en primaire, repli DuckDuckGo Lite ; {query, engine, result_count, resu… |
| `write_file` | contributor | — | ✅ | — | builtin | Ã‰crit `content` DANS la sandbox (rÃ©tro-compatible : chemins relatifs rÃ©solus depuis la racine autorisÃ©e,… |
| `write_json` | contributor | — | ✅ | — | builtin | SÃ©rialise `data` (dict ou list) en JSON UTF-8 indentÃ© DANS la sandbox |
| `zip_path` | contributor | — | ✅ | — | builtin | Compresse un fichier ou un dossier de la sandbox vers une archive .zip |

## Schémas d'entrée (`inputSchema`)

### `add`

```json
{
  "type": "object",
  "properties": {
    "a": {
      "type": "number",
      "description": ""
    },
    "b": {
      "type": "number",
      "description": ""
    }
  },
  "required": [
    "a",
    "b"
  ]
}
```

### `append_file`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "content": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "path",
    "content"
  ]
}
```

### `calc`

```json
{
  "type": "object",
  "properties": {
    "expression": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "expression"
  ]
}
```

### `call_api`

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": ""
    },
    "method": {
      "type": "string",
      "description": ""
    },
    "headers": {
      "type": "object",
      "description": ""
    },
    "body": {
      "type": "string",
      "description": ""
    },
    "json_payload": {
      "type": "object",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    },
    "max_chars": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "url"
  ]
}
```

### `cancel_training`

```json
{
  "type": "object",
  "properties": {
    "job_id": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "job_id"
  ]
}
```

### `copy_path`

```json
{
  "type": "object",
  "properties": {
    "src": {
      "type": "string",
      "description": ""
    },
    "dst": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "src",
    "dst"
  ]
}
```

### `count_lines`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `dataset_stats`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "sample_rows": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `dedupe_lines`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "keep": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `disk_usage`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    }
  },
  "required": []
}
```

### `docker_exec`

```json
{
  "type": "object",
  "properties": {
    "container": {
      "type": "string",
      "description": ""
    },
    "command": {
      "type": "string",
      "description": ""
    },
    "workdir": {
      "type": "string",
      "description": ""
    },
    "user": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "container",
    "command"
  ]
}
```

### `docker_logs`

```json
{
  "type": "object",
  "properties": {
    "container": {
      "type": "string",
      "description": ""
    },
    "tail": {
      "type": "integer",
      "description": ""
    },
    "timestamps": {
      "type": "boolean",
      "description": ""
    }
  },
  "required": [
    "container"
  ]
}
```

### `docker_ps`

```json
{
  "type": "object",
  "properties": {
    "all_containers": {
      "type": "boolean",
      "description": ""
    }
  },
  "required": []
}
```

### `docker_stats`

```json
{
  "type": "object",
  "properties": {
    "all_containers": {
      "type": "boolean",
      "description": ""
    }
  },
  "required": []
}
```

### `download_file`

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": ""
    },
    "filename": {
      "type": "string",
      "description": ""
    },
    "max_mb": {
      "type": "integer",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    }
  },
  "required": [
    "url",
    "filename"
  ]
}
```

### `env_info`

```json
{
  "type": "object",
  "properties": {},
  "required": []
}
```

### `file_checksum`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "algo": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `file_info`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `find_duplicates`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    }
  },
  "required": []
}
```

### `find_file`

```json
{
  "type": "object",
  "properties": {
    "pattern": {
      "type": "string",
      "description": ""
    },
    "path": {
      "type": "string",
      "description": ""
    },
    "max_results": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "pattern"
  ]
}
```

### `git_diff`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "staged": {
      "type": "boolean",
      "description": ""
    }
  },
  "required": []
}
```

### `git_log`

```json
{
  "type": "object",
  "properties": {
    "limit": {
      "type": "integer",
      "description": ""
    }
  },
  "required": []
}
```

### `git_status`

```json
{
  "type": "object",
  "properties": {},
  "required": []
}
```

### `gpu_info`

```json
{
  "type": "object",
  "properties": {},
  "required": []
}
```

### `head_file`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "max_lines": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `http_get`

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": ""
    },
    "headers": {
      "type": "string",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    },
    "max_chars": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "url"
  ]
}
```

### `http_post`

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": ""
    },
    "data": {
      "type": "string",
      "description": ""
    },
    "json_payload": {
      "type": "string",
      "description": ""
    },
    "headers": {
      "type": "string",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    },
    "max_chars": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "url"
  ]
}
```

### `job_get`

```json
{
  "type": "object",
  "properties": {
    "job_id": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "job_id"
  ]
}
```

### `job_list`

```json
{
  "type": "object",
  "properties": {
    "status": {
      "type": "string",
      "description": ""
    },
    "limit": {
      "type": "integer",
      "description": ""
    }
  },
  "required": []
}
```

### `list_dir`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "recursive": {
      "type": "boolean",
      "description": ""
    }
  },
  "required": []
}
```

### `make_dir`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `model_versions`

```json
{
  "type": "object",
  "properties": {
    "model_root": {
      "type": "string",
      "description": ""
    }
  },
  "required": []
}
```

### `move_path`

```json
{
  "type": "object",
  "properties": {
    "src": {
      "type": "string",
      "description": ""
    },
    "dst": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "src",
    "dst"
  ]
}
```

### `now`

```json
{
  "type": "object",
  "properties": {
    "utc": {
      "type": "boolean",
      "description": ""
    }
  },
  "required": []
}
```

### `postgres_query`

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": ""
    },
    "dsn": {
      "type": "string",
      "description": ""
    },
    "readonly": {
      "type": "boolean",
      "description": ""
    },
    "max_rows": {
      "type": "integer",
      "description": ""
    },
    "timeout_s": {
      "type": "number",
      "description": ""
    }
  },
  "required": [
    "query"
  ]
}
```

### `predict_sentiment`

```json
{
  "type": "object",
  "properties": {
    "texts": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "texts"
  ]
}
```

### `read_file`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "max_bytes": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `read_json`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `remove_path`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "recursive": {
      "type": "boolean",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `run_command`

```json
{
  "type": "object",
  "properties": {
    "command": {
      "type": "array",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    },
    "cwd": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "command"
  ]
}
```

### `run_python`

```json
{
  "type": "object",
  "properties": {
    "code": {
      "type": "string",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    }
  },
  "required": [
    "code"
  ]
}
```

### `run_shell`

```json
{
  "type": "object",
  "properties": {
    "command": {
      "type": "array",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    },
    "cwd": {
      "type": "string",
      "description": ""
    },
    "dry_run": {
      "type": "boolean",
      "description": ""
    }
  },
  "required": [
    "command"
  ]
}
```

### `search_in_files`

```json
{
  "type": "object",
  "properties": {
    "pattern": {
      "type": "string",
      "description": ""
    },
    "path": {
      "type": "string",
      "description": ""
    },
    "glob": {
      "type": "string",
      "description": ""
    },
    "max_results": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "pattern"
  ]
}
```

### `split_file`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "max_lines": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `sqlite_query`

```json
{
  "type": "object",
  "properties": {
    "db_path": {
      "type": "string",
      "description": ""
    },
    "query": {
      "type": "string",
      "description": ""
    },
    "readonly": {
      "type": "boolean",
      "description": ""
    },
    "max_rows": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "db_path",
    "query"
  ]
}
```

### `start_training`

```json
{
  "type": "object",
  "properties": {
    "max_per_lang": {
      "type": "integer",
      "description": ""
    },
    "local_corrections_path": {
      "type": "string",
      "description": ""
    },
    "augment_fraction": {
      "type": "number",
      "description": ""
    },
    "variants_per_example": {
      "type": "integer",
      "description": ""
    },
    "class_augment_weights": {
      "type": "object",
      "description": ""
    },
    "epochs": {
      "type": "integer",
      "description": ""
    },
    "batch_size": {
      "type": "integer",
      "description": ""
    },
    "device": {
      "type": "string",
      "description": ""
    }
  },
  "required": []
}
```

### `stop_training`

```json
{
  "type": "object",
  "properties": {
    "job_id": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "job_id"
  ]
}
```

### `tail_file`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "lines": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `touch`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "path"
  ]
}
```

### `train_model`

```json
{
  "type": "object",
  "properties": {
    "max_per_lang": {
      "type": "integer",
      "description": ""
    },
    "local_corrections_path": {
      "type": "string",
      "description": ""
    },
    "augment_fraction": {
      "type": "number",
      "description": ""
    },
    "variants_per_example": {
      "type": "integer",
      "description": ""
    },
    "class_augment_weights": {
      "type": "object",
      "description": ""
    },
    "epochs": {
      "type": "integer",
      "description": ""
    },
    "batch_size": {
      "type": "integer",
      "description": ""
    },
    "device": {
      "type": "string",
      "description": ""
    },
    "wait_timeout": {
      "type": "number",
      "description": ""
    }
  },
  "required": []
}
```

### `unzip_file`

```json
{
  "type": "object",
  "properties": {
    "src": {
      "type": "string",
      "description": ""
    },
    "dst": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "src",
    "dst"
  ]
}
```

### `web_fetch`

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": ""
    },
    "headers": {
      "type": "string",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    },
    "max_chars": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "url"
  ]
}
```

### `web_read`

```json
{
  "type": "object",
  "properties": {
    "url": {
      "type": "string",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    },
    "max_chars": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "url"
  ]
}
```

### `web_search`

```json
{
  "type": "object",
  "properties": {
    "query": {
      "type": "string",
      "description": ""
    },
    "max_results": {
      "type": "integer",
      "description": ""
    },
    "timeout": {
      "type": "number",
      "description": ""
    }
  },
  "required": [
    "query"
  ]
}
```

### `write_file`

```json
{
  "type": "object",
  "properties": {
    "filename": {
      "type": "string",
      "description": ""
    },
    "content": {
      "type": "string",
      "description": ""
    },
    "max_bytes": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "filename",
    "content"
  ]
}
```

### `write_json`

```json
{
  "type": "object",
  "properties": {
    "path": {
      "type": "string",
      "description": ""
    },
    "data": {
      "type": "string",
      "description": ""
    },
    "indent": {
      "type": "integer",
      "description": ""
    }
  },
  "required": [
    "path",
    "data"
  ]
}
```

### `zip_path`

```json
{
  "type": "object",
  "properties": {
    "src": {
      "type": "string",
      "description": ""
    },
    "dst": {
      "type": "string",
      "description": ""
    }
  },
  "required": [
    "src",
    "dst"
  ]
}
```

## Avertissements de compilation

- add: « description » manquante ou vide
- cancel_training: tool non classé par la policy legacy — posture fail-closed (mutation + admin) ; déclarer « safety » (standard thinktuning.tool/v1) dans tools_config.json pour lever l'ambiguïté
- find_duplicates: tool non classé par la policy legacy — posture fail-closed (mutation + admin) ; déclarer « safety » (standard thinktuning.tool/v1) dans tools_config.json pour lever l'ambiguïté
- run_shell: tool non classé par la policy legacy — posture fail-closed (mutation + admin) ; déclarer « safety » (standard thinktuning.tool/v1) dans tools_config.json pour lever l'ambiguïté
- start_training: tool non classé par la policy legacy — posture fail-closed (mutation + admin) ; déclarer « safety » (standard thinktuning.tool/v1) dans tools_config.json pour lever l'ambiguïté
- stop_training: tool non classé par la policy legacy — posture fail-closed (mutation + admin) ; déclarer « safety » (standard thinktuning.tool/v1) dans tools_config.json pour lever l'ambiguïté
- train_model: tool non classé par la policy legacy — posture fail-closed (mutation + admin) ; déclarer « safety » (standard thinktuning.tool/v1) dans tools_config.json pour lever l'ambiguïté
