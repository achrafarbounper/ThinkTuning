# Runbook — Backup chiffré & exercice de restauration (P2 lot 16)

## Ce qui est couvert

| Base | Contenu | TTL/purge |
|---|---|---|
| `experiments/jobs.db` | jobs d'entraînement (statuts, erreurs) | backup chiffré |
| `experiments/train_metrics.db` | métriques par epoch (loss/F1/accuracy) | backup chiffré |
| `experiments/agent_sessions.db` | conversations (données personnelles) | TTL 90 j (`scripts/retention.py`) + chiffrement au repos |
| `experiments/agent_audit.db` | journal d'audit | TTL 365 j + chiffrement au repos |

Le chiffrement au repos (sessions + audit) est **fail-closed en production** :
`ensure_store_crypto_configured()` (appelé dans le lifespan de l'API) refuse le
démarrage si `STORE_ENCRYPTION_KEY` est absente quand `ENV=prod`.

## Backup (quotidien, cron)

```bash
cd backend
python scripts/backup_encrypted.py
# -> experiments/backups/thinktuning-<TS>.backup.enc (+ manifeste .json)
```

- Archive **Fernet** (AES-128-CBC + HMAC) ; aucune base en clair sur disque
  pendant l'opération (tar en mémoire, chiffré en flux) ;
- clé : `BACKUP_ENCRYPTION_KEY` sinon `STORE_ENCRYPTION_KEY` ;
- le manifeste `.json` porte les tailles + le résultat de
  `PRAGMA integrity_check` AU MOMENT du backup.

Copier ensuite l'archive `.enc` + le manifeste hors de la machine (stockage
objet, NAS) — le backup local seul ne protège pas d'un ransomware.

## Exercice de restauration (mensuel)

**Objectif : prouver que le backup est restaurable — un backup jamais testé
n'est pas un backup.**

```bash
# 1. Restaurer dans un répertoire isolé (jamais destructif) :
python scripts/backup_encrypted.py --restore experiments/backups/thinktuning-XXXX.backup.enc

# 2. Vérifier le rapport : integrity_ok == true pour chaque base restaurée
#    (PRAGMA integrity_check exécuté sur les fichiers déchiffrés).

# 3. Écraser les bases live UNIQUEMENT après vérification :
python scripts/backup_encrypted.py --restore experiments/backups/thinktuning-XXXX.backup.enc --apply

# 4. Redémarrer l'API puis vérifier :
#    - GET /api/v1/health répond 200 ;
#    - GET /api/v1/train/jobs renvoie l'historique attendu (comptage vs manifeste).
```

## Rotation de la clé

Si `STORE_ENCRYPTION_KEY` est compromise :
1. générer une nouvelle clé ;
2. ré-écrire les stores chiffrés (les lignes `enc:` à l'ancienne clé ne sont
   plus lisibles — exporter avant rotation) ;
3. refaire un backup chiffré avec la nouvelle clé ;
4. documenter la rotation dans le journal d'audit (action `config_change`).

## Rétention

`scripts/retention.py` (cron hebdomadaire) purge sessions (90 j) et audit
(365 j) :

```bash
python scripts/retention.py --dry-run   # simulation
python scripts/retention.py             # application réelle
```