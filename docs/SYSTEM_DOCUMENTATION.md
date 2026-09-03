# Vue d'ensemble du système

ThinkTuning est un backend ML d'analyse de sentiments FR/EN (FastAPI, DistilBERT/LLM) augmenté d'une couche agentique. Le système suit une architecture hexagonale progressive : un nouveau noyau agentique (« core v2 », activé par le flag `AGENT_NEW_CORE=1`) coexiste avec le code historique (`ia/`, `core/`, `src/`), auquel il accède via des adaptateurs.

```
HTTP/WS/SSE ─▶ api/ (FastAPI — adapters d'entrée, aucune logique métier)
                     │ Depends / use-cases
              app/agent/core.py — AgentCore v2
              Intent → Plan → Policy → Budget → Action → Réponse
                     │ ports (interfaces, app/domain/ports)
        ┌────────────┴────────────────┐
   app/domain/ (entités, erreurs, ports)   app/infrastructure/ (adaptateurs legacy)
                     │
   legacy : ia/ (LLM, outils, sandbox, orchestrateur multi-agents, copilote)
            core/ (stores SQLite, scheduler, runners ML)
            src/  (ML : dataset, model, inference)
```

**Règle d'or des dépendances** : `app/domain/**` ne dépend de rien ; `app/agent/**` ne dépend que du domaine ; `app/infrastructure/**` implémente les ports en déléguant au legacy ; `api/**` assemble et expose.

Les composants agentiques sont pilotés par des feature flags (`AGENT_RELIABILITY`, `AGENT_AUDIT`, `AGENT_CONTEXT`, `AGENT_COPILOT`, `AGENT_MULTI_AGENT`, `AGENT_WEBSOCKET`) et le basculeur de noyau `AGENT_NEW_CORE`.

## Sommaire

- [Core v2](#core-v2)
- [Agents](#agents)
  - [AgentCore v2 (noyau agentique)](#agentcore-v2-noyau-agentique)
  - [AgentCore legacy (boucle historique)](#agentcore-legacy-boucle-historique)
  - [Lead / Superviseur multi-agents](#lead--superviseur-multi-agents)
  - [Worker — Recherche web](#worker--recherche-web)
  - [Worker — Fichiers](#worker--fichiers)
  - [Worker — Machine Learning](#worker--machine-learning)
  - [Worker — Données (SQL)](#worker--données-sql)
  - [Worker — Calcul](#worker--calcul)
  - [Worker — Système & Ops](#worker--système--ops)
  - [Worker — Exécution (Shell)](#worker--exécution-shell)

---

## Core v2

### Architecture

- **Boucle applicative** : `app/agent/core.py` (`AgentCore`), moteur du noyau v2, bâti exclusivement sur les ports du domaine — testable sans réseau ni SQLite via des fakes.
- **Composition root** : `app/agent/factory.py` assemble le noyau avec le client LLM legacy (retry + circuit breaker + streaming) et l'adaptateur du registre d'outils, en lisant `app/config/settings.py` (Pydantic Settings, fail-fast sur les clés API).
- **Ports (6)** : `LLMClientPort`, `ToolRegistryPort`, `SessionStorePort`, `AuditStorePort`, `RunStorePort`, `ApprovalStorePort` — implémentés par le legacy (`ia/agent/llm_client.py`, `ia/tools/tool_registry.py`, `core/session_store.py`, `core/audit_store.py`, `core/run_store.py`, `core/approval_store.py`). Des tests de conformité verrouillent l'alignement Protocols ↔ implémentations.
- **Bascule** : sans `AGENT_NEW_CORE=1`, l'endpoint `/ask/core` renvoie 503 et le comportement historique est strictement préservé (rollout incrémental, flags désactivés par défaut).

### Responsabilités

1. **Génération du system prompt depuis le registre réel des outils** — le LLM connaît les noms/arguments exacts des outils disponibles et sait quand les appeler.
2. **Exécution du flux Intent → Plan → Policy → Budget → Action**, avec traçabilité complète (traces d'actions sérialisables, réponses SSE, journalisation auditable).
3. **Garde-fous** : budget de rounds LLM et d'appels d'outils, sandbox policy (AUTO_APPROVE / APPROVE / REJECT), anti-boucle sur rejet identique.
4. **Auto-correction** : outil inconnu, arguments manquants ou erreur d'exécution sont renvoyés au LLM avec la liste des outils valides, jusqu'à épuisement du budget.

### Flux de données

1. **Intent** (`POST /api/agent/ask/core`) : prompt, session, rôle, budget (`agent_max_llm_rounds`, `agent_max_tool_calls`), validé à l'entrée via les entités du domaine.
2. **Plan** : le planner (LLM) propose un plan JSON ; parsing tolérant (JSON direct, liste, fences markdown, prose autour). Une réponse **sans JSON est une réponse finale légitime** (salutation, explication) renvoyée telle quelle.
3. **Policy** (par action) : `sandbox_policy.py` décide :
   - `AUTO_APPROVE` → exécution immédiate (lecture, réseau lisible) ;
   - `APPROVE` → validation humaine obligatoire (écriture, exécution) : le run se termine en `pending_approval`, l'action est persistée dans `core/approval_store`, le client approuve via `POST /api/agent/approvals/{id}/approve` puis relance avec `resume_request_id`. La reprise n'accorde que l'action dont l'empreinte SHA-256 des arguments correspond exactement à la demande approuvée ;
   - `REJECT` → règle dure, jamais exécutée : SQL mutant (seuls `SELECT/WITH/EXPLAIN/PRAGMA` passent), POST vers hôte privé (anti-SSRF), chemins sensibles (`.git`, `.env`, clés privées). Anti-boucle : une même action rejetée deux fois (empreinte) arrête le run.
4. **Budget** : plafonnement des rounds LLM et appels d'outils ; dépassement → `BUDGET_EXHAUSTED`.
5. **Action** : exécution via `ToolRegistryPort` ; erreur outil → renvoyée au LLM (auto-correction).
6. **Mémoire & audit** : messages et mémoire long terme persistés (`SessionStorePort`), chaque appel audité (`AuditStorePort`), événements d'outils diffusés en temps réel (SSE `tool_start` / `tool_result` / `thinking_delta`).

---

## Agents

### AgentCore v2 (noyau agentique)

- **Rôle / mission** : boucle générique Intent → Plan → Policy → Budget → Action → Réponse. Noyau unique remplaçant progressivement la boucle historique ; sert aussi de base aux workers du mode multi-agents.
- **Inputs** :
  - Requête HTTP `POST /api/agent/ask/core` (prompt utilisateur, session_id, rôle) ;
  - System prompt généré dynamiquement depuis le registre d'outils (`tools_config.json`) ;
  - Historique de session + mémoire inter-sessions (`core/session_store.py`, table `agent_memory`) ;
  - Réglages : provider LLM (`ollama` / `openrouter` / `hf`), modèle, timeout, budgets.
- **Outputs** :
  - `AgentRunResult` : réponse finale, statut terminal, trace de réflexion (« thinking »), traces d'actions (outil, args, décision, statut, résumé de résultat), budget consommé ;
  - Destinations : réponse HTTP, flux SSE (`/ask/core/stream`), stores (runs, sessions, audit), demandes d'approbation (`pending_approval`).
- **APIs / services** :
  - LLM : endpoint Ollama local, OpenRouter ou HF (client legacy avec retry + circuit breaker + streaming) ;
  - Registre d'outils legacy (~50 outils : web, fichiers, SQL, ML, calcul, ops, shell, docker) ;
  - Stores SQLite : sessions, runs, audit, approvals, jobs.
- **Logique de décision** : policy par catégorie d'outil (`classify_tool`) ; rejet bloquant et audité des actions dangereuses ; anti-boucle par empreinte d'action ; auto-correction itérative ; réponse texte directe si aucun outil nécessaire ; reprise d'approbation vérifiée par empreinte SHA-256 des arguments.
- **Cas d'usage typiques** : question-réponse augmentée par outils, exécution supervisée d'actions risquées (écriture fichier, SQL), chat avec mémoire inter-sessions, streaming temps réel dans le dashboard.
- **Limitations** : pas de replanification automatique après `budget_exhausted` ; dépend du parsing tolérant du JSON du LLM (petits modèles locaux) ; GPU/nvidia-smi hors sandbox Windows CI ; budget de contexte borné (~8k tokens des modèles 8B).

### AgentCore legacy (boucle historique)

- **Rôle / mission** : boucle agentique d'origine (`ia/agent/agent_core.py`) : planification JSON → exécution d'outils → réponse finale. Coexiste avec le core v2 pendant la migration.
- **Inputs** : mêmes sources que le core v2 (prompt, historique, registre d'outils, config env).
- **Outputs** : `AgentResult` (answer, thinking, actions, métriques), événements sur l'event bus interne, logs audit.
- **APIs / services** : client LLM, registre d'outils, event bus, middlewares, observabilité, audit — tous optionnels via imports best-effort (l'absence d'un module ne bloque pas le démarrage).
- **Logique de décision** : system prompt généré depuis le registre réel ; réponse texte sans JSON acceptée au premier tour ; auto-correction (outil inconnu / arguments manquants / erreur) renvoyée au LLM avec la liste des outils valides (jusqu'à 6 rounds) ; résultats tronqués à 4000 caractères ; fidélité exigée (la conclusion doit s'appuyer sur les résultats d'outils).
- **Cas d'usage typiques** : tous les endpoints agent historiques (`/api/agent/*`), workers multi-agents (la classe `AgentCore` du legacy sert de worker).
- **Limitations** : logique moins stricte que le core v2 (pas de budget de policy intégré) ; double identité d'import (`ia.tools` / `tools`) via hacks `sys.path` ; progressivement remplacé.

### Lead / Superviseur multi-agents

- **Rôle / mission** : orchestration multi-agents (flag `AGENT_MULTI_AGENT`) : décomposer la demande utilisateur en sous-tâches assignées aux rôles spécialisés, dispatcher, puis agréger les résultats en une réponse finale cohérente. N'exécute **aucun outil**.
- **Inputs** :
  - Prompt utilisateur ;
  - Catalogue des rôles et de leurs capacités (`roles.py`, `ROLE_ORDER`) ;
  - Résultats des workers.
- **Outputs** :
  - Plan JSON validé (liste de `{task_id, role, subtask}`) ;
  - Réponse finale synthétisée, avec signalement explicite de chaque sous-tâche non exécutée (« continueBroken ») ;
  - Événements de cycle de vie : `agent.plan`, `agent.worker.*`, `agent.synthesizing`, `agent.done`, `agent.error`.
- **APIs / services** : LLM (prompts planner/synthèse dédiés), `plan_validator` (validation déterministe du plan), pool de threads (dispatch parallèle optionnel), event bus.

### Worker — Recherche web

- **Rôle / mission** : recherche et lecture d'informations réelles sur Internet (actualité, faits, contenu de pages).
- **Inputs** : sous-tâche du superviseur + contexte global résumé (texte) ; requête de recherche ou URL à lire.
- **Outputs** : résultat d'outil (texte/JSON) renvoyé au LLM du worker, puis agrégé par le Lead ; résumés SSE `tool_start`/`tool_result`.
- **APIs / services** : `web_search`, `web_fetch`, `web_read`, `http_get`, `http_post` (via registre d'outils).
- **Logique de décision** : boucle AgentCore restreinte à ce sous-ensemble d'outils ; politique anti-SSRF appliquée aux POST (hôte privé refusé).
- **Cas d'usage typiques** : veille d'actualité, vérification de faits, extraction de contenu de pages.
- **Limitations** : `http_post` soumis à approbation selon policy ; aucune authentification avancée ; dépend de la disponibilité des cibles externes (le circuit breaker LLM ne couvre pas le web).

### Worker — Fichiers

- **Rôle / mission** : lecture, écriture et manipulation de fichiers dans la sandbox (création, édition, déplacement, suppression, recherche).
- **Inputs** : sous-tâche + chemins/contenus (texte, JSON).
- **Outputs** : fichiers créés/modifiés dans la sandbox ; résultats d'outils (statistiques, checksums, lignes) renvoyés au LLM.
- **APIs / services** : `list_dir`, `read_file`, `find_file`, `make_dir`, `copy_path`, `move_path`, `remove_path`, `write_file`, `file_info`, `file_checksum`, `head_file`, `count_lines`, `touch`, `write_json`, `read_json`, `find_duplicates`, `split_file`, `dedupe_lines`, `search_in_files`, `tail_file`, `append_file`, `now`.
- **Logique de décision** : lectures en `AUTO_APPROVE` ; écritures/suppressions en `APPROVE` ; chemins sensibles (`.git`, `.env`, clés privées) en `REJECT` dur.
- **Cas d'usage typiques** : génération de rapports, nettoyage/consolidation de données, vérification d'intégrité.
- **Limitations** : confiné à la sandbox ; suppression définitive non réversible après approbation humaine ; aucune gestion de permissions POSIX avancée.

### Worker — Machine Learning

- **Rôle / mission** : métier ThinkTuning — gestion des jobs d'entraînement, datasets, versions de modèles et prédictions de sentiment.

### Worker — Données (SQL)

- **Rôle / mission** : interrogation de bases relationnelles (SQLite, PostgreSQL) en lecture sécurisée.
- **Inputs** : sous-tâche + requête SQL.
- **Outputs** : résultats de requêtes (lignes, JSON) renvoyés au LLM.
- **APIs / services** : `sqlite_query`, `postgres_query`.
- **Logique de décision** : règles dures — SQL mutant (`INSERT/UPDATE/DELETE/DROP`…) en `REJECT` ; seuls `SELECT/WITH/EXPLAIN/PRAGMA` passent.
- **Cas d'usage typiques** : explorations analytiques, vérifications de données avant/après entraînement, audits.
- **Limitations** : aucune écriture possible (par conception) ; pas de requêtes longues paginées ; SQL généré par un petit LLM peut échouer (auto-correction limitée par le budget).

### Worker — Calcul

- **Rôle / mission** : calculs exacts et expressions mathématiques via la calculatrice sûre.
- **Inputs** : sous-tâche + expression mathématique.
- **Outputs** : résultat numérique exact.
- **APIs / services** : `calc`, `add` (outils déterministes, sans LLM).
- **Logique de décision** : exécution déterministe en `AUTO_APPROVE` ; le LLM se borne à formuler l'expression.
- **Cas d'usage typiques** : arithmétique exacte (le LLM seul se trompe), conversions, agrégations simples.
- **Limitations** : expressions supportées limitées au parser de la calculatrice ; pas de calcul symbolique avancé.

### Worker — Système & Ops

- **Rôle / mission** : informations système/exploitation, archivage, git, téléchargements.
- **Inputs** : sous-tâche + chemins/URLs.
- **Outputs** : rapports système (env, disque), archives, états git, fichiers téléchargés.
- **APIs / services** : `env_info`, `disk_usage`, `zip_path`, `unzip_file`, `git_status`, `git_log`, `git_diff`, `download_file`.
- **Logique de décision** : lecture système en `AUTO_APPROVE` ; écritures (zip, download, mutations git) en `APPROVE`.
- **Cas d'usage typiques** : diagnostic de capacité disque, snapshots d'artefacts, revue de l'état git.
- **Limitations** : accès git en lecture seule (pas de push) ; téléchargements soumis à validation ; nvidia-smi hors sandbox Windows CI.

### Worker — Exécution (Shell)

- **Rôle / mission** : exécution de commandes shell et de scripts Python.
- **Inputs** : sous-tâche + commande ou script Python.
- **Outputs** : sortie standard/erreur de la commande, résultats de scripts.
- **APIs / services** : `run_command`, `run_python` (via sandbox `ia/tools/sandbox.py`).
- **Logique de décision** : **toute exécution est en `APPROVE`** — validation humaine obligatoire ; rejet dur si empreinte déjà refusée (anti-boucle).
- **Cas d'usage typiques** : tâches ad-hoc non couvertes par les outils dédiés, vérifications rapides.
- **Limitations** : friction d'approbation systématique ; risque le plus élevé du système ; limité par le timeout de la sandbox.

### Worker — Docker & GPU

- **Rôle / mission** : gestion de conteneurs Docker et information GPU.

### Copilote — Suggestions d'outils

- **Rôle / mission** : assistance « GitHub Copilot » (Phase D, flag `AGENT_COPILOT`) : suggérer les outils les plus pertinents pour la conversation en cours et pré-remplir leurs arguments.
- **Inputs** :
  - Historique de messages `[{role, content}]` (dernier message utilisateur = requête par défaut) ;
  - Brouillon en cours de saisie (affine la requête, pré-remplit les arguments) ;
  - Registre d'outils + scores lexicaux (`tool_discovery.suggest_tools`) ;
  - Boosts d'apprentissage du store de feedback.
- **Outputs** : liste de suggestions triées `{tool, score, base_score, reasons, required_args, args}` (squelette d'arguments pré-rempli, champs obligatoires à compléter) ; mapping langage naturel → outil unique (`nl_to_tool`) ; complétion de texte.
- **APIs / services** : `tool_discovery` (scoring lexical), `copilot.feedback` (boosts), client LLM (uniquement pour `complete_text` — injection par l'appelant, testable offline).
- **Logique de décision** : score lexical de base + boost d'apprentissage (borné [0,1]) ; squelette d'args : si un seul argument obligatoire et une valeur entre guillemets dans le brouillon → pré-remplissage ; échec doux (chaîne vide) sur toute erreur LLM.
- **Cas d'usage typiques** : auto-complétion dans le dashboard, découverte d'outils par langage naturel, accélération de la saisie de requêtes agent.
- **Limitations** : scoring purement lexical (pas sémantique) ; boosts déterministes simples ; complétion texte dépendante de la qualité du LLM local.

### Copilote — Boucle de feedback

- **Rôle / mission** : apprendre des interactions utilisateur : enregistrer acceptation/refus des suggestions et en dériver un boost de pertinence pour le re-classement.
- **Inputs** : issues de suggestions (tool, accepted, session_id, suggestion JSON) via l'API.
- **Outputs** : boost par outil (+0.1 par acceptation, plafonné à +0.3 ; −0.15 si refus dominants ; neutre si non évalué) ; statistiques (accepts / rejects / accept_rate).
- **APIs / services** : base SQLite dédiée `experiments/agent_copilot.db` (surchargeable via `AGENT_COPILOT_PATH`), store thread-safe, singleton paresseux.
- **Logique de décision** : ajustement déterministe ; un outil souvent accepté monte, souvent refusé descend.
- **Cas d'usage typiques** : personnalisation progressive des suggestions par usage, mesure de la qualité du copilote.
- **Limitations** : pas de décroissance temporelle des boosts ; granularité par outil uniquement (pas par contexte) ; apprentissage simple, non validé statistiquement.

### Gestionnaire de contexte conversationnel

- **Rôle / mission** : gestion avancée du contexte (Phase C, flag `AGENT_CONTEXT`) : borner le contexte rejoué au LLM et maintenir une mémoire inter-sessions.
- **Inputs** : historique de messages, budget en jetons, fonction de résumé LLM optionnelle.
- **Outputs** : historique optimisé (fenêtre glissante : tours récents conservés, anciens résumés en un message ou écartés avec note de troncature) ; note de mémoire inter-sessions (plafonnée à 2000 caractères) persistée dans `core/session_store.py` (table `agent_memory`).
- **APIs / services** : heuristique d'estimation de tokens (~4 caractères/jeton, budget défaut 1200 tokens) ; résumé via LLM injecté par l'appelant (aucune I/O réseau dans le module) ; store de sessions SQLite.
- **Logique de décision** : parcours du plus récent au plus ancien tant que le budget n'est pas dépassé ; les tours débordants sont résumés (un seul appel LLM) si une fonction de résumé est fournie, sinon écartés avec note explicite.
- **Cas d'usage typiques** : conversations longues avec modèles à fenêtre réduite (8B, ~8k tokens), reprise de contexte entre sessions.
- **Limitations** : estimation de tokens approximative ; mémoire inter-sessions limitée à un résumé textuel court ; pas de récupération sémantique (pas de vector store).

---

## Roadmap et améliorations possibles

Issues du backlog de migration (`ARCHITECTURE.md` §6) et des limitations identifiées :

- **Migration des stores legacy** vers `app/infrastructure/persistence/` (SQLAlchemy + Alembic pour le schéma SQLite).
- **Suppression des hacks `sys.path`** (double identité `ia.tools` / `tools`) — un paquet installable unique.
- **Étendre ruff/mypy** à `api/`, `core/`, `ia/`, `tests/`.
- **Streaming SSE complet de `/ask/core`** : événements `tool_start`/`tool_result` réutilisant l'event bus legacy.
- **Baseline GPU** : intégrer `gpu_info` et `nvidia-smi` dans la sandbox Windows / CI.
- **Replanification dynamique multi-agents** : aujourd'hui abort sur plan invalide et « continueBroken » sans replan ; introduire une replanification contrôlée en V2.
- **Mémoire sémantique** : remplacer le résumé textuel inter-sessions par une récupération vectorielle (embeddings + vector store).
- **Scoring sémantique du copilote** : au-delà du lexical + boosts, intégrer des embeddings et une décroissance temporelle du feedback.
- **Observabilité unifiée** : corréler runs / audits / métriques outils / circuit breaker dans un dashboard unique.
- **Approbations par lots** et politiques de délégation (approuver N actions similaires d'un coup).

- **Inputs** : sous-tâche + identifiants de conteneurs / commandes.
- **Outputs** : états de conteneurs, logs, statistiques, infos GPU.
- **APIs / services** : `docker_ps`, `docker_logs`, `docker_exec`, `docker_stats`, `gpu_info`.
- **Logique de décision** : inspection (`ps`, `logs`, `stats`, `gpu_info`) en `AUTO_APPROVE` ; `docker_exec` (exécution dans le conteneur) en `APPROVE`.
- **Cas d'usage typiques** : diagnostic d'environnement d'entraînement, supervision de conteneurs, vérification de la disponibilité GPU.
- **Limitations** : nécessite Docker local ; `docker_exec` hérite des risques du shell ; supervision limitée aux conteneurs locaux.

- **Inputs** : sous-tâche + paramètres (nom de job, dataset, texte à classer).
- **Outputs** : statuts de jobs, statistiques de dataset, prédictions de sentiment, historique de versions de modèles.
- **APIs / services** : `job_list`, `job_get`, `predict_sentiment`, `dataset_stats`, `model_versions`, `start_training`, `train_model`, `cancel_training`, `stop_training` — pont vers `core/` (job_store, trainer_runner, scheduler) et `src/` (entraînement DistilBERT, inférence).
- **Logique de décision** : lecture libre ; lancement/arrêt d'entraînement soumis à la policy (mutations → `APPROVE`) ; erreurs job renvoyées au LLM pour auto-correction.
- **Cas d'usage typiques** : « entraîne le modèle sur le dataset enrichi », « quelle est la précision du dernier run ? », « classe ce texte ».
- **Limitations** : entraînement long non supervisable round-by-round (jobs asynchrones) ; GPU partagé ; budget de rounds peut couper une orchestration complexe.

- **Logique de décision** :
  1. **Plan** : le LLM produit un plan ; validation déterministe — plan invalide ⇒ abort global (pas de dispersion) ;
  2. **Dispatch** : un `AgentCore` worker est créé **par appel** (aucun état partagé, sûr en threads) ; isolation stricte du contexte : chaque worker ne reçoit que le contexte global résumé + sa sous-tâche ;
  3. **Approbation** : si au moins un worker est `awaiting_approval`, interruption **avant** synthèse (une synthèse sans résultat bloqué serait trompeuse) — le front affiche la carte Approuver/Refuser puis relance la sous-tâche ;
  4. **Synthèse** : tentée si au moins un worker est `ok` ; échec de synthèse → réponse partielle honnête (jamais de replanification en V1).
- **Cas d'usage typiques** : tâches composées (ex. « cherche X sur le web, enregistre dans un fichier, lance un calcul »), délégation parallèle par domaine, reprise après validation humaine.
- **Limitations** : pas de replanification dynamique (V1) ; granularité d'erreurs limitée à trois buckets (ok / failed / abort) ; pas d'état partagé entre workers (impossible de chaîner les sorties d'un worker vers un autre sans passer par le superviseur).


**Statuts terminaux d'un run** : `completed`, `pending_approval`, `rejected_loop`, `budget_exhausted`, `failed`.

**Fiabilité transverse** (`ia/agent/reliability.py`, flag `AGENT_RELIABILITY`) : classification des erreurs LLM (réessayable vs définitive), retry à backoff exponentiel + jitter, circuit breaker thread-safe (fermé / ouvert / demi-ouvert, cooldown, `reset()`).

  - [Worker — Docker & GPU](#worker--docker--gpu)
  - [Copilote — Suggestions d'outils](#copilote--suggestions-doutils)
  - [Copilote — Boucle de feedback](#copilote--boucle-de-feedback)
  - [Gestionnaire de contexte conversationnel](#gestionnaire-de-contexte-conversationnel)
- [Roadmap et améliorations possibles](#roadmap-et-améliorations-possibles)
