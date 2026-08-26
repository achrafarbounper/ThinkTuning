# Système d'analyse de sentiments multilingue avec recomposition (EDA)

Pipeline complet : recomposition de données (augmentation) + fine-tuning d'un
modèle multilingue pour la classification de sentiments (positif / neutre / négatif),
en français et anglais.

## Structure

```
sentiment_project/
├── augmentation.py   # Système de recomposition (EDA) : SR, RI, RS, RD
├── data_loader.py     # Chargement du dataset multilingue + application de l'augmentation
├── train.py            # Fine-tuning de XLM-RoBERTa sur le dataset augmenté
├── predict.py         # Inférence sur de nouveaux textes
└── requirements.txt
```

## Installation (Windows, version CPU)

Ouvrez **PowerShell** (ou l'invite de commandes) dans le dossier du projet :

```powershell
py -3.13 -m venv venv
venv\Scripts\activate

pip install --upgrade pip
pip install torch --index-url https://download.pytorch.org/whl/cpu
pip install -r requirements.txt
python -c "import nltk; nltk.download('wordnet'); nltk.download('omw-1.4'); nltk.download('punkt')"
```

> Notes Windows :
> - Si `py -3.13` ne fonctionne pas, essayez `python -m venv venv` (à condition que `python --version` renvoie bien 3.13.13).
> - En **PowerShell**, si `venv\Scripts\activate` est bloqué par la politique d'exécution, lancez d'abord :
>   `Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass`
> - En **invite de commandes (cmd.exe)**, utilisez `venv\Scripts\activate.bat` à la place.
> - Une fois activé, votre prompt doit afficher `(venv)` au début de la ligne.

## Le système de recomposition (`augmentation.py`)

4 opérations combinées, appliquées aléatoirement pour générer des variantes
d'un texte qui conservent son sentiment d'origine :

| Opération | Description |
|---|---|
| **Synonym Replacement** | Remplace des mots par des synonymes (via WordNet FR/EN) |
| **Random Insertion** | Insère des synonymes à des positions aléatoires |
| **Random Swap** | Permute deux mots dans la phrase |
| **Random Deletion** | Supprime aléatoirement quelques mots |

```python
from augmentation import recompose

variants = recompose(
    "Ce film était vraiment excellent, j'ai adoré chaque instant.",
    lang="fr",
    num_variants=4
)
```

## Entraînement

```bash
python train.py --max_per_lang 500 --epochs 2 --batch_size 8
```

Options principales :
- `--max_per_lang` : nombre d'exemples chargés par langue (fr/en) — 500 par défaut, adapté au CPU
- `--local_corrections_path` : chemin d'un fichier local de corrections manuelles (CSV ou JSONL) à concaténer au dataset HF avant l'entraînement (voir [Boucle d'active learning complète](#boucle-dactive-learning-complète))
- `--dataset_file` : chemin vers un CSV/JSONL local (`text`, `label`, `lang_code`) — ex. la sortie enrichie de `merge_reviewed_data.py`. Par défaut : dataset Hugging Face.
- `--augment_fraction` : proportion du dataset recomposée (0.4 = 40%)
- `--variants_per_example` : nombre de variantes générées par texte augmenté
- `--epochs`, `--batch_size` : hyperparamètres classiques (2 epochs / batch 8 par défaut pour rester raisonnable en CPU)
- `--num_workers` : nombre de workers de tokenisation / DataLoader. Par défaut calculé automatiquement pour un entraînement CPU stable.

Le modèle utilisé est **DistilBERT multilingue** (`distilbert-base-multilingual-cased`,
~135M paramètres), un bon compromis vitesse/qualité pour un entraînement CPU,
qui gère nativement le français et l'anglais.

**Conseil CPU** : laissez `--num_workers` non renseigné pour que le script choisisse
une valeur sûre automatiquement, ou réduisez-le à `0`/`1` si votre machine est
très limitée.

**Temps indicatif en CPU** : avec les valeurs par défaut (500 exemples/langue,
2 epochs), comptez de l'ordre de 20 à 60 minutes selon votre machine. Pour aller
plus vite pendant les tests, réduisez encore `--max_per_lang` (ex: 200).

## Boucle d'active learning complète

Le pipeline ferme la boucle « entraîner → détecter les incertitudes → corriger
manuellement → ré-entraîner » :

```
active_learning.py → review manuelle → merge_reviewed_data.py → train.py --local_corrections_path ...
                                                    (ou POST /train avec local_corrections_path)
```

### 1. Sélectionner les exemples incertains

```bash
python active_learning.py --input data/train.jsonl --top_n 50 --output data/manual_review_template.csv
```

Exporte les exemples dont la confidence prédite est la plus proche de 1/3
(incertitude maximale sur 3 classes) dans un CSV de review.

### 2. Review manuelle

Ouvrez le CSV (`text`, `predicted_label`, `manual_label`, `status`) et remplissez
`manual_label` (`negative` / `neutral` / `positive`) pour chaque ligne corrigée.

### 3. Fusionner les corrections validées

```bash
python merge_reviewed_data.py --review data/sample_review.csv \
    --source data/train.jsonl --output data/corrections.csv
```

> Script fourni par SCRUM-56. Il produit un fichier **CSV ou JSONL** normalisé
> avec exactement trois colonnes : `text`, `label`, `lang_code`.

### 4. Ré-entraîner avec les corrections

Via la CLI :

```bash
python train.py --local_corrections_path data/corrections.csv --max_per_lang 500 --epochs 2
```

Ou via l'API :

```bash
curl -X POST http://localhost:8000/train \
  -H "Content-Type: application/json" \
  -H "X-API-Key: $API_KEY" \
  -d '{"max_per_lang": 500, "epochs": 2, "local_corrections_path": "data/corrections.csv"}'
```

Les corrections sont **concaténées au dataset Hugging Face** avant toute étape
suivante : le `train_test_split` puis l'augmentation EDA s'appliquent donc bien
**après** la fusion (principe « augmentation après split », pour éviter la fuite
de données). Le split répartit aussi les corrections entre train et validation.

### Format attendu du fichier de corrections

| Colonne    | Valeurs acceptées |
|------------|-------------------|
| `text`     | texte non vide |
| `label`    | `0` / `1` / `2` ou `negative` / `neutral` / `positive` (insensible à la casse) |
| `lang_code`| `fr` / `en` |

Exemple CSV :

```csv
text,label,lang_code
Service client au top !,positive,fr
Mediocre quality overall.,negative,en
```

Exemple JSONL équivalent :

```jsonl
{"text": "Service client au top !", "label": "positive", "lang_code": "fr"}
{"text": "Mediocre quality overall.", "label": 0, "lang_code": "en"}
```

Le fichier est validé au chargement : chemin existant, extension `.csv` /
`.jsonl` / `.ndjson`, colonnes présentes, labels et langues supportés. Toute
anomalie lève une erreur explicite (message précisant la ligne et la cause), et
le job d'entraînement passe en `failed` avec ce message via `GET /train/status`.
Sans `--local_corrections_path` / `local_corrections_path`, le comportement est
strictement inchangé (dataset HF seul).

## Boucle d'active learning (review manuelle)

Le pipeline permet de cibler les faiblesses du modèle puis de réinjecter les
corrections humaines avant un nouveau fine-tuning :

```bash
# 1. Sélection des exemples les plus incertains (confidence proche de 1/3)
python active_learning.py --input data/train.jsonl --output data/manual_review_template.csv

# 2. Review manuelle : compléter la colonne manual_label du CSV
#    (negative / neutral / positive — les lignes vides ou non tranchées
#    seront ignorées à l'étape suivante)

# 3. Réinjection des corrections dans le jeu d'entraînement.
#    Par défaut, fusion avec le dataset Hugging Face utilisé par train.py ;
#    --source accepte aussi un fichier local (CSV/JSON/JSONL).
python merge_reviewed_data.py --review data/manual_review_template.csv --source data/train.jsonl --output data/train_enriched.jsonl

# 4. Réentraînement sur le dataset enrichi
python train.py --dataset_file data/train_enriched.jsonl --epochs 2 --batch_size 8
```

Comportement de `merge_reviewed_data.py` :

- seules les lignes avec un `manual_label` valide (`negative` / `neutral` /
  `positive`, alias français acceptés) sont conservées ;
- le label est converti en entier selon `LABEL_NAMES` de `src/dataset/loader.py`
  ({0: negative, 1: neutral, 2: positive}) ;
- la déduplication se fait sur le texte normalisé (trim + minuscules) : un
  texte déjà présent dans la source voit son label **mis à jour** avec la
  correction manuelle (pas de doublon), un texte nouveau est ajouté avec
  `lang_code` (`--lang_code`, `fr` par défaut) ;
- export en JSONL (défaut) ou CSV (`--format csv`) avec les colonnes
  `text`, `label`, `lang_code`, directement consommable par
  `train.py --dataset_file`.

## Prédiction

```bash
python predict.py "Ce produit est fantastique, je recommande !"
```

Comment l'utiliser

Place api.py à la racine du projet (au même niveau que train.py, configs/, src/), puis :

bash
pip install fastapi "uvicorn[standard]"
# Linux/macOS
export API_KEY="change-me-super-secret"
export RATE_LIMIT_PER_MINUTE="60"
# Windows PowerShell
# $env:API_KEY="change-me-super-secret"
# $env:RATE_LIMIT_PER_MINUTE="60"
uvicorn api:app --reload --host 0.0.0.0 --port 8000

La doc interactive s'ouvre sur http://localhost:8000/docs.

API_KEY est obligatoire même en local. Les routes sensibles exigent l'en-tête X-API-Key. Exemple :
bash
curl -H "X-API-Key: change-me-super-secret" http://localhost:8000/models

Protection contre les abus
- Les endpoints POST /predict et /predict/batch sont protégés par un rate limit configurable via la variable d'environnement RATE_LIMIT_PER_MINUTE (par défaut 60 requêtes/minute par client IP).
- Si la limite est dépassée, l'API répond avec un 429 Too Many Requests et envoie l'en-tête Retry-After (en secondes).
- La logique est implémentée en Python avec un token bucket simple, sans dépendance externe.

Lancer un entraînement
bash
curl -X POST http://localhost:8000/train \
  -H "Content-Type: application/json" \
  -d '{"max_per_lang": 500, "epochs": 2, "batch_size": 8}'

→ répond immédiatement avec un job_id.

Suivre la progression
bash
curl http://localhost:8000/train/status/<job_id>

→ step te dit où en est l'entraînement (loading_dataset, training, saving_model, etc.) et status passe à completed ou failed.

Prédire
bash
curl -X POST http://localhost:8000/predict \
  -H "Content-Type: application/json" \
  -d '{"texts": ["Ce produit est fantastique !", "This is terrible."]}'
Détails d'implémentation à connaître
/train ne bloque jamais : l'entraînement tourne dans un thread séparé, l'API répond en millisecondes.
Le Predictor est mis en cache en mémoire — il n'est chargé qu'une fois, au premier appel à /predict, et automatiquement invalidé/rechargé quand un entraînement se termine (ou manuellement via /predict/reload).
Le registre des jobs est en mémoire (dict Python) — parfait pour du dev/local, mais si tu déploies en multi-worker (plusieurs process uvicorn) ou que tu redémarres le service, il faudrait passer à un store partagé (Redis, base de données). Dis-moi si c'est ton cas, je peux adapter.
Un GET /health te donne un statut rapide (modèle dispo ou non, jobs actifs).

## Calibration (`calibrate.py`)

Vérifie que la **confidence** du modèle reflète sa **précision réelle** : le
script génère une calibration curve (*reliability diagram*) construite avec
`sklearn.calibration.calibration_curve`, calcule l'**ECE** (Expected
Calibration Error) et enregistre un **avertissement dans les logs dès que
l'ECE dépasse 0.1** (mauvais calibrage).

```bash
# Courbe de calibration + ECE sur le jeu d'évaluation
# (par défaut : la dernière version valide de experiments/models)
python calibrate.py --max_per_lang 500

# Calibrer une version précise du modèle
python calibrate.py --model_name 20260825T153054Z

# Si l'ECE > 0.1 : ajuster les probabilités par temperature scaling post-hoc
python calibrate.py --temperature_scaling     # T appris (minimisation NLL)
python calibrate.py --temperature 1.8         # ou T fixé à la main
```

Sorties générées dans `outputs/` :

- `calibration_curve.png` : diagramme de fiabilité — précision réelle en
  fonction de la confiance prédite, la diagonale représentant un modèle
  parfaitement calibré ;
- `calibration_curve_after.png` : idem après temperature scaling (si activé) ;
- `calibration_report.json` : ECE avant/après, détail des bins, température.

Options principales :

- `--model_name <version>` : calibre une version précise de `experiments/models`
  (ex. `20260825T153054Z`) ; par défaut, la **dernière version valide**
  (horodatage le plus récent) y est utilisée automatiquement ;
- `--max_per_lang`, `--batch_size`, `--max_length` : identiques à `evaluate.py`
  (`max_length` réutilise `configs/default.yaml` par défaut) ;
- `--n_bins` (10 par défaut) et `--strategy uniform|quantile` : binning de
  l'ECE et de la courbe ;
- `--output_dir` : dossier des artefacts (défaut : `outputs/`).

> Notes :
> - Le temperature scaling ne change **jamais** la classe prédite (l'argmax
>   est invariant), uniquement la confiance associée.
> - Pour un rapport non biaisé, ajustez idéalement T sur un jeu de validation
>   distinct du jeu d'évaluation final.

## Agent IA (`ia/`)

L'agent LLM (Ollama + outils sandboxés) est intégré à l'API principale sous le
préfixe `/api/agent` (l'ancien serveur autonome `ia/api_server.py` a été retiré ;
`core/agent_cache.py` est le point d'entrée unique vers l'agent) :

```bash
uvicorn api.main:app --reload --host 0.0.0.0 --port 8000
```

Variables d'environnement dédiées (relues à chaque appel) :

- `AGENT_OLLAMA_URL` (défaut `http://192.168.1.184:11434/api/chat`)
- `AGENT_MODEL_NAME` (défaut `llama3.1:8b`)
- `AGENT_TIMEOUT_SECONDS` (défaut `600`) — timeout des appels vers Ollama
- `AGENT_SANDBOX_ROOT` : racine autorisée pour tous les outils fichiers (défaut : répertoire de lancement)
- `AGENT_ALLOWED_BINARIES` : allowlist CSV des exécutables autorisés par `run_command`
- `AGENT_BLOCK_PRIVATE_HOSTS=1` : interdit aux outils HTTP (`http_get`, `http_post`,
  `download_file`, `web_*`) les hôtes privés/loopback (anti-SSRF)
- `AGENT_PG_DSN` : DSN PostgreSQL de `postgres_query` (`postgresql://user:pwd@host:5432/base`)
- `AGENT_LOG_LEVEL` : niveau de logs de l'agent (`DEBUG`, `INFO` par défaut, `WARNING`, ...)

### Logs de l'agent (`thinktuning.agent`)

L'agent journalise chaque étape sur le logger standard **`thinktuning.agent`**
(même convention que `thinktuning.api` pour le reste de l'API) :

| Événement | Niveau | Contenu |
|---|---|---|
| `run_start` / `run_done` | INFO | début/fin d'une requête agent (nb de rounds, taille de réponse, raison de sortie) |
| `round start=i/n` | INFO | chaque tour de planification LLM |
| `llm_request` / `llm_response` | INFO | appel Ollama : url, modèle, durée, taille du contenu |
| `tool_call` / `tool_result` | INFO | outil exécuté avec ses arguments / type de résultat |
| `tool_error` | ERROR | échec d'un outil, traceback complet inclus |
| `auto_correction` | WARNING | erreur renvoyée au LLM pour qu'il se corrige |
| `tool_intent_detected` | WARNING | outil annoncé dans la réflexion sans aucun appel JSON : relance automatique exigeant le JSON strict |
| `llm_raw_response`, `llm_response_content`, `tool_result_content` | DEBUG | payloads bruts (activer via `AGENT_LOG_LEVEL=DEBUG`) |

Routes (auth principale `X-API-Key`, sauf `/status` qui est public) :

- `GET /api/agent/status` — statut, modèle visé, outils disponibles ;
- `GET /api/agent/tools` — outils et arguments requis ;
- `POST /api/agent/tools/run` — exécution directe d'un outil : `{"tool": "gpu_info", "args": {}}` ;
- `POST /api/agent/ask` — prompt libre : `{"prompt": "..."}` — l'agent planifie lui-même
  les appels d'outils puis renvoie la réponse finale ; en cas d'appel invalide
  (tool inconnu, arguments manquants, erreur d'exécution), l'erreur est renvoyée
  au LLM qui se corrige automatiquement (plafonné à `MAX_LLM_ROUNDS` tours).

### Mode « Réflexion » (thinking)

L'agent peut raisonner explicitement avant de répondre : la trace de raisonnement
est séparée de la réponse finale et affichée dans une bulle repliable du chat.

- **Activation** : toggle « Réflexion » de l'en-tête du chat (choix persisté en
  localStorage), ou champ `"enable_thinking": true` dans `POST /api/ai`
  (désactivé par défaut).
- **Deux mécanismes complémentaires** :
  - *induit par le prompt* : la section `THINKING_PROMPT_SECTION`
    (`ia/agent/system_prompt.py`) demande au modèle d'encadrer son raisonnement
    par des balises `<think>…</think>`, extraites par `ia/agent/thinking.py`
    avant le parsing JSON des outils — fonctionne avec n'importe quel modèle,
    y compris `llama3.1:8b` ;
  - *natif Ollama* : avec un modèle de raisonnement (`deepseek-r1`, `qwen3`,
    `gpt-oss`…), `LLMClient(think=True)` envoie `"think": true` et lit le champ
    `message.thinking` (repli automatique sur les balises inline pour les
    serveurs Ollama anciens).
- **Streaming réelle** : `LLMClient` appelle Ollama avec `stream: true` et
  consomme le flux NDJSON (`message.thinking` et `message.content`). En mode
  « Réflexion », la trace est diffusée EN TEMPS RÉEL via les événements SSE
  `{"thinking_delta": "…"}` pendant la génération (avant la réponse finale),
  elle s'affiche donc progressivement dans la bulle repliable du chat. Sans
  réflexion, aucun événement `thinking_delta` : contrat inchangé.
- **API Python** : `AgentCore.run_detailed(prompt, on_thinking=None) ->
  AgentResult(answer, thinking)` et `core.agent_cache.ask_agent_detailed(prompt,
  model, enable_thinking) -> {"answer", "thinking"}` exposent la trace ;
  `run()` / `ask_agent()` conservent leur comportement historique (réponse
  seule). `ask_agent_detailed_streaming(...)` propage en plus la réflexion
  au fil de l'eau via un callback.
- **Garde-fou « outil annoncé mais jamais appelé »** : quand le modèle raisonne
  (« je vais appeler web_search », « let me use the web_search tool… ») puis
  conclut EN TEXTE sans avoir jamais émis le JSON d'appel, l'agent refuse cette
  réponse sortie de mémoire (bug « qui a gagné la coupe du monde 2026 »),
  journalise `tool_intent_detected` (WARNING) et relance UNE fois le modèle en
  exigeant le JSON strict ; l'outil est alors réellement exécuté avant toute
  conclusion. Le parsing a aussi été re-rendu tolérant : un JSON d'appel
  entouré de prose ou de fences markdown est de nouveau détecté et exécuté
  (`AgentCore.extract_json_blocks` délègue à `ia/agent/json_parser.py`).
- **Tests** : `pytest tests/test_agent_thinking.py -v` (tests offline).

### Provider LLM : Ollama ou OpenRouter

Le même agent peut consommer soit un serveur Ollama auto-hébergé (historique),
soit l'API hébergée [OpenRouter](https://openrouter.ai/) (compatible OpenAI,
accès à des centaines de modèles cloud dont les modèles de raisonnement).
La bascule est **globale côté serveur**, par variable d'environnement :

| Variable | Rôle |
|---|---|
| `AGENT_PROVIDER` | `ollama` (défaut) ou `openrouter` |
| `AGENT_MODEL_NAME` | nom du modèle (`llama3.1:8b`, ou ID OpenRouter `vendor/model`) |
| `AGENT_OLLAMA_URL` | endpoint chat Ollama (`/api/chat`) |
| `AGENT_OPENROUTER_URL` | endpoint OpenRouter (`…/api/v1/chat/completions`) |
| `OPENROUTER_API_KEY` | clé API OpenRouter — **requise** si provider = openrouter |

Exemple pour passer à OpenRouter (`.env`) :

```env
AGENT_PROVIDER=openrouter
AGENT_MODEL_NAME=deepseek/deepseek-r1:free
OPENROUTER_API_KEY=sk-or-v1-…
```

Comportements notables :

- **Mode « Réflexion »** : fonctionne dans les deux cas. Côté Ollama le champ
  natif `message.thinking` est lu ; côté OpenRouter le flux SSE porte la trace
  via `choices[0].delta.reasoning` (repli `reasoning_content`), diffusée en
  temps réel via les mêmes événements `thinking_delta`. Les balises `<think>`
  inline restent extraites en repli.
- **Sélecteur de modèle du chat** : `/api/models` liste les modèles installés
  sur Ollama (`GET /api/tags`) OU disponibles sur OpenRouter (`GET /api/v1/models`)
  selon le provider actif ; le contrat JSON est identique, aucun changement
  dashboard requis.
- **Clé manquante** : démarrage avec `AGENT_PROVIDER=openrouter` sans
  `OPENROUTER_API_KEY` → réponse HTTP 500 explicite au premier appel.
- **Tests** : `pytest tests/test_agent_openrouter.py -v` (tests offline).

### Outils disponibles

| Catégorie | Outil | Signature | Description |
|---|---|---|---|
| Math | `add` | `(a, b)` | Addition |
| Fichiers | `write_file` | `(filename, content)` | Écrit un fichier (parents créés), sandboxé |
| Fichiers | `list_dir` | `(path=".", recursive=false)` | Liste un répertoire (type, taille, date) |
| Fichiers | `read_file` | `(path, max_bytes=65536)` | Lit un fichier texte, sortie tronquée |
| Fichiers | `find_file` | `(pattern, path=".", max_results=100)` | Recherche récursive par regex (nom ou chemin), retourne les chemins relatifs |
| Fichiers | `make_dir` | `(path)` | Crée un répertoire (parents inclus) |
| Fichiers | `copy_path` | `(src, dst)` | Copie fichier ou arborescence |
| Fichiers | `move_path` | `(src, dst)` | Déplace / renomme |
| Fichiers | `remove_path` | `(path, recursive=false)` | Supprime (garde-fous : racine et `.git` interdits) |
| Exécution | `run_command` | `(command: liste, timeout=60)` | Commande en liste d'arguments, allowlist de binaires, sans shell |
| Exécution | `run_python` | `(code, timeout=30)` | Code Python dans un sous-processus isolé puis nettoyé |
| Réseau | `http_get` | `(url, headers?, timeout=30)` | GET http(s), corps tronqué (~8 Ko) |
| Réseau | `http_post` | `(url, data?/json_payload?, ...)` | POST brut ou JSON |
| Internet | `web_search` | `(query, max_results=5)` | Recherche web DuckDuckGo Lite : titres, URLs, extraits (publicités filtrées) |
| Internet | `web_fetch` | `(url, headers?, timeout=20)` | Page distante brute : statut, titre HTML, corps tronqué |
| Internet | `web_read` | `(url, timeout=20)` | Texte lisible extrait d'une page HTML (sans scripts/styles/balises) |
| Docker | `docker_ps` | `(all_containers=false)` | Conteneurs au format JSON |
| Docker | `docker_logs` | `(container, tail=100)` | Dernières lignes de logs |
| Docker | `docker_exec` | `(container, command)` | Commande via `sh -c` dans le conteneur |
| GPU | `gpu_info` | `()` | CUDA, VRAM totale/allouée/réservée, utilisation % (torch + nvidia-smi) |
| Bases | `sqlite_query` | `(db_path, query, readonly=true)` | SQLite confinée à la sandbox, lecture seule par défaut |
| Bases | `postgres_query` | `(query, readonly=true, timeout_s=30)` | PostgreSQL via psycopg2, statement_timeout appliqué |

### Sécurité (`ia/tools/sandbox.py`)

- **Confinement des chemins** : tout chemin est résolu sous `AGENT_SANDBOX_ROOT` ;
  `../`, chemins absolus externes et évasions sont refusés.
- **Pas d'injection shell** : `run_command` exige une LISTE d'arguments exécutée
  avec `shell=False` ; cmd/powershell/bash/sh sont hors allowlist par défaut.
- **Timeouts partout** : sous-processus (1–600 s), HTTP, SQL (`statement_timeout`).
- **Sorties plafonnées** (~8 Ko par outil, ~4 Ko injectés au LLM) pour protéger le contexte.
- **SQL lecture seule par défaut** : SQLite via `PRAGMA query_only`, PostgreSQL via
  filtre de mots-clés (INSERT/UPDATE/DROP/… refusés tant que `readonly=true`).
- **Destructif encadré** : `remove_path` refuse la racine et tout ce qui est sous
  `.git` ; un dossier non vide impose `recursive=true`.

### Exemples

```bash
# État GPU (VRAM + utilisation)
curl -H "X-API-Key: change-me-api-key" -H "Content-Type: application/json" \
  -X POST http://localhost:8000/api/agent/tools/run -d '{"tool": "gpu_info", "args": {}}'

# Conteneurs Docker actifs
curl -H "X-API-Key: change-me-api-key" -H "Content-Type: application/json" \
  -X POST http://localhost:8000/api/agent/tools/run \
  -d '{"tool": "docker_ps", "args": {"all_containers": true}}'

# Requête SQLite en lecture seule
curl -H "X-API-Key: change-me-api-key" -H "Content-Type: application/json" \
  -X POST http://localhost:8000/api/agent/tools/run \
  -d '{"tool": "sqlite_query", "args": {"db_path": "experiments/jobs.db", "query": "SELECT * FROM jobs LIMIT 5"}}'

# Recherche Internet (outils web_search / web_fetch / web_read)
curl -H "X-API-Key: change-me-api-key" -H "Content-Type: application/json" \
  -X POST http://localhost:8000/api/agent/tools/run \
  -d '{"tool": "web_search", "args": {"query": "transformers fine-tuning"}}'

# Prompt libre : l'agent choisit lui-même les outils
curl -H "X-API-Key: change-me-api-key" -H "Content-Type: application/json" \
  -X POST http://localhost:8000/api/agent/ask \
  -d '{"prompt": "Liste le dossier experiments puis dis-moi combien de modèles il contient."}'
```

Doc interactive : http://localhost:8000/docs. Notes :

- `psycopg2-binary` (déjà dans requirements.txt) n'est requis que pour `postgres_query`.
- Tests offline de tous ces outils : `pytest tests/test_agent_tools.py tests/test_agent_api.py tests/test_agent_logging.py -v`.

## Docker
docker compose build

# Entraînement (service par défaut)
docker compose up train

# Prédiction (profil séparé, modèle déjà entraîné requis)
docker compose --profile predict run --rm predict

# Évaluation
docker compose --profile evaluate run --rm evaluate

# Override rapide des hyperparamètres sans toucher le compose file
docker compose run --rm train python train.py --max_per_lang 200 --epochs 1

## Notes

- Le dataset source (`cardiffnlp/tweet_sentiment_multilingual`) est téléchargé
  automatiquement depuis le Hugging Face Hub au premier lancement, via ses
  fichiers Parquet (~1800 exemples/langue en train — `max_per_lang` au-delà
  de ce nombre sera automatiquement plafonné).
- Pour de meilleurs résultats, un GPU est fortement recommandé pour le
  fine-tuning (Google Colab gratuit fonctionne bien pour des tests).
- Vous pouvez ajuster `augment_fraction` : plus il est élevé, plus le
  dataset final grossit, mais avec un risque de bruit si trop de mots
  sont remplacés (ajustez `alpha` dans `augmentation.py` si besoin).
