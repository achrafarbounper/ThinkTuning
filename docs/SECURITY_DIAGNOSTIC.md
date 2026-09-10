# Diagnostic Sécurité — ThinkTuning — Rapport + Plan d'Action

> **Mode : PLAN** — diagnostic statique sur code lu en lecture seule, sans modification ni test intrusif. Pas de scan dynamique / pentest / `pip audit` / `npm audit` exécuté.
> **Périmètre :** `backend/api/*`, `backend/app/agent/policies/*`, `backend/app/infrastructure/security/api_key.py`, `backend/app/config/settings.py`, `backend/ia/tools/*`, `backend/Dockerfile`, `docker-compose.yml`, `searxng/settings.yml`, `frontend/templates/default.conf.template`, `frontend/src/api/*`.
> **Branche analysée :** `SCRUM-136` (`3b677d4`).

---

## 1. Synthèse exécutive

**Niveau global : MOYEN-FAIBLE — durcissable rapidement.** Bonnes fondations : sandbox chemins (`safe_resolve`), allowlist binaires sans shell, comparaison clé à temps constant, `PRAGMA query_only`, policy `REJECT/APPROVE/AUTO_APPROVE`, budget LLM/outils, user non-root Docker, healthcheck.

**Mais 4 faiblesses systémiques dominent :**

| # | Risque systémique | Criticité | Effet |
|---|---|---|---|
| R1 | **Secret unique + fallback dev public** `dev-local-api-key` si `API_KEY` absente | **Critique** | Prise de contrôle totale API + agent + MCP si exposé sans `.env` |
| R2 | **SSRF / exfiltration / RCE indirect via agent** : `AGENT_BLOCK_PRIVATE_HOSTS` OFF par défaut, `requests` sans garde domaines, `docker_exec(sh -c)`, `run_python` hérite `os.environ` | **Haute** | Scan réseau interne, exfil secrets, rebond |
| R3 | **Auth / exposition inégale** : lectures sessions publiques, token WS en query `?token=`, rate-limit limité à `/predict`, `X-Forwarded-For` trusted par défaut | **Haute** | Fuite conversations, contournement throttle, DoS LLM/train |
| R4 | **Hygiène secrets / supply-chain** : `searxng secret_key` codée en dur, pas de rotation/scopes/RBAC, pas de CSP/HSTS nginx, pas de scan deps/SBOM | **Moyenne-Haute** | Persistances, XSS/clickjacking, CVE silencieuses |

**Score indicatif (interne) : 5.2/10 — objectif 8.5/10 après P0+P1.**

---

## 2. Constats détaillés

### 2.1 Authentification / Autorisation — OWASP API1, API5

**F1 — Fallback dev public [CRITIQUE]**
`backend/app/infrastructure/security/api_key.py:26-31` : `DEV_FALLBACK_KEY="dev-local-api-key"` si `API_KEY` vide. Seul un `logger.warning` au démarrage (`backend/api/dependencies/auth.py:28-34`). `docker-compose.yml:74` : `API_KEY=${API_KEY}` → vide si non défini = fallback actif en prod.
*Risque :* clé publique connue → bypass 401 global. *Correctif P0 :* fail-closed si `ENV=prod` et clé absente ; clé 256-bit générée au setup.

**F2 — Secret unique sans scopes/RBAC [HAUTE]**
`require_api_key()` binaire `bool` partout. Même clé pour `predict`, `train`, `maintenance/enable`, `approvals`, `MCP`. Pas de rotation, pas de hachage, pas de révocation par client (aveu `mcp_server_sse.py:248` : « secret client store S4+ »).
*Risque :* compromission = contrôle total + DoS via `POST /maintenance/enable`.

**F3 — Token WS en query param [MOYENNE]**
Convention `?token=` pour `/train/stream/{job_id}` et `/api/agent/ws` (README). Logs serveur nginx/proxy, historique navigateur, `Referer` → fuite.
*Correctif :* jeton éphémère `DASHBOARD_WS_TOKEN` à TTL court + `Sec-WebSocket-Protocol`, révocation à la fermeture.

**F4 — Lectures publiques [MOYENNE]**
`backend/api/routes/sessions.py:31,58` : `GET /api/sessions`, `GET /{id}/messages` sans `require_api_key` (commentaire assumé « lecture publique »). `GET /health` expose `model_dir` absolu + `active_jobs`. `GET /metrics` exclu maintenance sans auth.
*Risque :* énumération conversations (PII), fingerprinting infra.

### 2.2 Surface HTTP / CORS / Throttle — OWASP API4, API7

**F5 — CORS permissif + credentials [HAUTE]**
`backend/api/main.py:167-174` : `allow_methods=["*"]`, `allow_headers=["*"]`, `allow_credentials=True` + `allow_origin_regex` optionnelle (ex. `^https://think-tuning-ai-.*\.vercel\.app$` très large). Combinaison `credentials+*` dangereuse si regex mal posée.
*Correctif :* whitelist méthodes/headers explicites, `allow_credentials=True` uniquement avec origines explicites, jamais avec regex wildcard.

**F6 — Rate-limit partiel + spoofable [HAUTE]**
`backend/api/middlewares/rate_limit.py:60-89` : seuls `POST /predict|/predict/batch|/compare (+v1)` throttlés. `ask/agent/train/explain/mcp/orchestrate` = LLM/GPU coûteux **non limités**. `RATE_LIMIT_TRUST_PROXY=1` par défaut → `X-Forwarded-For` forgeable → bypass. Bucket in-memory par process → bypass multi-workers gunicorn.
*Correctif :* bucket Redis partagé, throttle par coût (LLM/train strict), `TRUST_PROXY=0` si exposition directe.

**F7 — Proxy nginx longue durée [MOYENNE]**
`frontend/templates/default.conf.template:33-35,51-52` : `proxy_read/send_timeout 3600s`, `buffering off` sur `/api/` et `/mcp/`. Aucun `limit_req`, aucun header sécu (`CSP, HSTS, X-Frame-Options, X-Content-Type-Options`).
*Risque :* Slowloris / occupation workers, clickjacking, MIME-sniffing.

### 2.3 Agent / Tools — OWASP LLM01-LLM08 + RCE/SSRF

Points positifs reconnus : `sandbox.safe_resolve().resolve()` + refus `..`, `check_command_allowed` sans shells, `run_subprocess(shell=False)`, `truncate_output`, policy pure `sandbox_policy.decide()` avec `DENIED_PATH_PARTS={.git,.env,venv,id_rsa...}`, budget `6 LLM / 20 tools`.

**F8 — SSRF OFF par défaut [HAUTE]**
`backend/ia/tools/sandbox.py:187` : `AGENT_BLOCK_PRIVATE_HOSTS` vide = pas de blocage. `network_tools.http_get/post`, `web_tools.web_fetch/read/search` appellent `enforce_host_policy` qui devient no-op. `requests.get/post` suivent les redirects par défaut → bypass allowlist via `302` vers `169.254.169.254`. Pas de borne taille download avant `resp.text` → OOM.
*Correctif P0 :* `=1` par défaut en prod, `allowlist={searxng}` déjà prévue, `allow_redirects=False` + validation rebond, `stream=True` + `Content-Length` max.

**F9 — `docker_exec(sh -c)` = injection déléguée [HAUTE]**
`backend/ia/tools/docker_tools.py:63-77` : `["docker","exec",container,"sh","-c",command]` où `command:str` libre. L'allowlist hôte est contournée dès qu'on entre dans le conteneur. Si socket Docker monté un jour → évasion.
*Correctif :* passer en `argv:list` sans `sh -c`, ou classer `EXEC` → `APPROVE` humain obligatoire + liste conteneurs autorisés.

**F10 — `run_python` exfiltre l'env [HAUTE]**
`backend/ia/tools/shell_tools.py:72-80` : `env=dict(os.environ)` → snippet hérite `API_KEY, OPENROUTER_API_KEY, HF_TOKEN, AGENT_PG_DSN`. Pas de limites CPU/RAM, timeout jusqu'à 300s, répertoire `.agent_tmp` sous sandbox (pollution).
*Correctif :* env minimal blanchi, `RLIMIT_CPU/AS`, seccomp/gVisor à terme, interdire `import os/socket/subprocess` ou exécuter en microVM.

**F11 — Écritures sensibles non bloquées au niveau exécution [MOYENNE]**
`file_tools._ensure_writable:53-59` ne bloque que `root` et `.git`. Protection `.env/.pem/.key/id_rsa` uniquement dans `sandbox_policy.classify_path_risk` (décisionnelle). Si appel direct tool sans passer `PolicyGate` → écrasement `.env`, clés, `jobs.db`. `AGENT_ALLOWED_BINARIES` inclut `curl,wget,python,pip,docker,node,npm` → exfil + install arbitraire.
*Correctif :* remonter `DENIED` dans `safe_resolve/_ensure_writable` (défense en profondeur), `curl/wget/docker` hors défaut.

**F12 — SQL : garde perfectible [MOYENNE]**
`database_tools.py:23-37,63-66` : filtre mots-clés + `PRAGMA query_only` (bon). Mais `readonly=false` autorise création/écriture partout sandbox → remplissage disque. `postgres_query` : `cur.execute(query)` brute (par design outil), `dsn` passable en arg → loggé dans `runs/audit` → fuite credentials. `_DB_ALLOWED_PREFIXES` autorise `pragma` → `PRAGMA` abusables.
*Correctif :* masquer DSN dans logs, `readonly=false` → `APPROVE` + quota taille, retirer `pragma` des préfixes.

**F13 — Prompt-injection → actions [MOYENNE]**
Aucune séparation système/outils observée, pas de validation sortie LLM avant `run_tool`. `custom_tools.run_shell(shlex.split(string))` accepte chaîne LLM.
*Correctif :* schéma strict + confirmation humaine pour `WRITE/EXEC/NETWORK` (déjà `decide()` → vérifier câblage `PolicyGateToolProvider` sur **tous** les transports y compris MCP `orchestrate`).

### 2.4 Secrets / Config — OWASP A07

**F14 — Secrets en clair [HAUTE]**
`searxng/settings.yml:10` : `secret_key:"thinktuning-searxng-change-me"`. `.env` parsé maison (`settings.py:33-70`) sans `python-dotenv`, chargé depuis `backend/.env` + racine (risque commit accidentel ; `.gitignore` couvre `.env` OK mais pas `.env.*` totalement). Clés LLM via env + `agent_settings` API — vérifier masquage réponse (`agent.py:1292 pop` → à auditer : ne jamais renvoyer `*_key`).
*Correctif :* vault/manager secrets, rotation 90j, `secret_key` générée au déploiement, audit `GET /agent/settings` sans valeurs.

### 2.5 Frontend / Data / Supply-chain / Résilience

**F15 — Stockage clé côté front [MOYENNE]** `AppProvider` + `useLocalStorage` pour `config` → clé API en `localStorage` = volable en XSS. Pas de CSP. *Correctif :* cookie `HttpOnly;Secure;SameSite=Strict` via proxy same-origin, CSP stricte, `XSS` tests.

**F16 — Upload ML [MOYENNE-FAIBLE]** Bornes `PREDICT_*` bonnes (`predict.py:26-30`), chunk 128 anti-OOM, `pyarrow` géré. Reste : CSV `pandas` → formula injection à l'export (`=cmd|...`), modèle `transformers` = désérialisation pickle possible si modèle non fiable. *Correctif :* préfixe `'`, `trust_remote_code=False`, vérif hash modèle.

**F17 — Deps sans scan [MOYENNE]** `transformers==5.15.0` (version future/suspecte à épingler/vérifier), `torch>=2.0`, `starlette==1.0.1`, `onnxruntime`, `psycopg2`, `apscheduler`. Pas de `pip-audit/npm-audit/Dependabot/SBOM`. Dockerfile bon (multi-stage offline, `USER 1000`, `max-requests`) mais base `python:3.13-slim` non pinnée par digest.
*Correctif :* pin + digest, `pip-audit` + `npm audit` en CI, SBOM CycloneDX.

**F18 — Observabilité [FAIBLE]** `metrics.py` log `client_ip+path` en INFO (volume/PII), pas de corrélation `request-id`, audit `agent_audit` existant mais anonymisation à vérifier (RGPD : sessions/messages persistés en SQLite sans TTL/chiffrement).

---

## 3. Matrice des risques (Probabilité × Impact)

| ID | Vulnérabilité | P | I | Criticité | OWASP |
|---|---|---|---|---|---|
| F1 | Fallback `dev-local-api-key` | H | C | **Critique** | API2 |
| F8 | SSRF (défaut OFF) | H | H | **Haute** | API7/LLM06 |
| F9 | `docker_exec sh -c` | M | C | **Haute** | A03 |
| F10 | `run_python` hérite secrets | M | C | **Haute** | LLM07 |
| F2 | Clé unique sans RBAC | M | H | Haute | API1 |
| F5 | CORS `*` + credentials | M | H | Haute | API8 |
| F6 | Throttle partiel/spoofable | H | M | Haute | API4 |
| F14 | `searxng secret_key` dur | H | M | Haute | A07 |
| F3,F4,F11,F12,F15 | Token query, lectures publiques, écritures, DSN loggé, clé localStorage | M | M | Moyenne | — |
| F7,F16,F17,F18 | nginx, CSV/pickle, deps, logs | M | M/F | Moyenne/Faible | — |

---

## 4. Plan d'action priorisé

### P0 — 0-15 jours (stop-bleeding, effort faible)

1. **Fail-closed API_KEY en prod** : refuser démarrage si `ENV=prod` et `API_KEY` absente/longueur<32 ; générer `secrets.token_urlsafe(32)` au setup ; révoquer fallback. Fichiers : `security/api_key.py`, `api/main.py`, `compose`.
2. **SSRF ON par défaut** : `AGENT_BLOCK_PRIVATE_HOSTS=1`, `ALLOWLIST=searxng`, `allow_redirects=False` + re-validation hôte après redirect, `stream+max_bytes` dans `network/web_tools`.
3. **Durcir CORS** : méthodes `GET,POST,PUT,PATCH,DELETE,OPTIONS` explicites, headers `Authorization,X-API-Key,Content-Type` explicites, `allow_credentials` uniquement origines explicites, resserrer `CORS_ALLOW_ORIGIN_REGEX`.
4. **Corriger `searxng secret_key`** : génération obligatoire via env `SEARXNG_SECRET` au déploiement, supprimer valeur codée en dur.
5. **Protéger lectures** : `require_api_key` sur `GET /sessions`, `/messages`, masquer `model_dir` absolu, auth sur `/metrics` ou port interne seul.
6. **`TRUST_PROXY=0` par défaut** sauf derrière nginx vérifié ; documenter.

### P1 — 15-45 jours (confinement agent + auth)

7. **Défense en profondeur fichiers** : `DENIED_PATH_PARTS/EXT` dans `safe_resolve/_ensure_writable`, pas seulement policy.
8. **Neutraliser `docker_exec`** : `argv:list` sans `sh -c`, allowlist conteneurs, `APPROVE` humain obligatoire, jamais de socket Docker en prod sans proxy filtrant.
9. **Blanchir env `run_python`** : env minimal `{PATH,PYTHON*}`, `resource.setrlimit`, timeout 30s défaut, blocage imports réseau/`os.exec`.
10. **RBAC minimal + rotation** : 2 clés (`read:predict` vs `admin:train/agent/maintenance`), rotation via `API_KEY_OLD` grace 24h, masquage `*_key,dsn` dans logs/runs/audit.
11. **Throttle global** : TokenBucket Redis partagé, quotas par route coûteuse (`ask=20/min, train=5/h, orchestrate=30/min`), `Retry-After`, alertes 429.
12. **Frontend** : CSP `default-src'self'`, `frame-ancestors'deny'`, `HSTS`, `X-Content-Type-Options:nosniff`, clé hors `localStorage` (cookie HttpOnly via same-origin).

### P2 — 1-3 mois (durable)

13. **Auth moderne** : JWT courte durée + service-account, vault (Doppler/Infisical/Azure KV), audit accès secrets.
14. **Isolation exécution** : workers outils en conteneur éphémère sans réseau/secrets, gVisor/Firecracker, quotas disque.
15. **Supply-chain** : pin digest Docker, `pip-audit`+`npm audit`+`trivy` en CI bloquant Haute/Critique, SBOM + signature modèles (`sha256` vérifié au load, `trust_remote_code=False`).
16. **Privacy/Résilience** : TTL/chiffrement `sessions/audit`, `request-id` tracing, backup chiffré `jobs.db/train_metrics`, exercice restore, limites `train` concurrent + file d'attente.
17. **LLM-sécurité** : détecteur prompt-injection, validation JSON plan (`plan_validator` déjà existant à généraliser), red-team SSRF/exfil quarterly.

### Alternatives

* **Simple (PME, 2j)** : P0 seul → divise risque ~70% pour <1j/homme.
* **Intermédiaire (recommandée)** : P0+P1 → posture audit-passable, surcharge perf <5%.
* **Avancée (régulé/OIV)** : +P2, WAF ModSecurity, mTLS inter-services, SOC/SIEM, pentest annuel.

---

## 5. Prochaines étapes proposées

1. Validez le périmètre (prod visée : Render ? VPS ?) et l'appétence `fail-closed` vs compat dev.
2. **Basculez en `Act mode`** (« toggle to Act mode ») pour que j'implémente P0 fichier par fichier avec tests (`pytest` + vérif non-régression) :
   - `api_key.py` + `auth.py` + `compose` ;
   - `sandbox.py` + `network/web_tools` + `CORS/main.py` + `rate_limit.py` ;
   - `searxng/settings.yml` + `sessions.py` + `default.conf.template` headers.
3. Je livrerai ensuite `SECURITY.md` + checklist OWASP + GitHub Actions `pip-audit/trivy`.

*Hypothèses/limites : sans exécution dynamique, criticités = estimation statique ; à confirmer par `trivy`, `pip-audit`, `npm audit`, test d'intrusion SSRF/RCE en staging isolé.*



