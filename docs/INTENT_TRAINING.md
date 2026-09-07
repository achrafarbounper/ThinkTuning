# Entraînement du classifieur d’intention — Architecture technique

> Documentation technique de la chaîne d'entraînement du **classifieur
> intention chat/action** (encodeur `AutoModelForSequenceClassification`,
> entraînement HF `Trainer`).

Complète `ARCHITECTURE.md` et `docs/CLASSIFIERS.md` sur le même format. La voie
intention est distincte du **fine-tuning LLM causal** (`docs/FINETUNING.md`) :
un **encodeur** à **tête de classification** sur un dataset JSONL local
`{"text", "label"}` (labels `chat`/`action`), **sans** EDA, **sans** dataset HF
et **sans** quantification 4-bit.

---

## 1. Périmètre et mise en situation

Le classifieur d'intention distingue, pour chaque message de l'agent :

- **`action`** — un message qui déclenche une action tool ;
- **`chat`** — une interaction conversationnelle (remarque, remerciement, …).

Le modèle (MiniLM multilingue par défaut) est entraîné via **`scripts/train_intent.py`**
historique, puis refactorisé en module importable (`core/intent_trainer.py`,
SCRUM-95) et **exposé par l'API** (`POST /train/intent`). Contrairement au
fine-tuning LLM, **aucune sous-position GPU n'est exigée** pour l'inférence :
le classifieur possède un **fallback règles** (`ia/agent/classifiers/fallback.py`)
qui prend le relais quand aucun modèle entraîné n'existe.

### Position dans les deux chaînes d'entraînement

| Entraînement | Encodeur (classification) | LLM causal (génération texte) |
|---|---|---|
| Module | `core/intent_trainer.py` / `scripts/train_intent.py` | `finetune_llm.py` |
| Modèle | `AutoModelForSequenceClassification` | `AutoModelForCausalLM` |
| Dataset | JSONL local `{text,label}` | JSONL Alpaca `instruction/input/output` (issu du labeling) |
| Sortie | Versions `experiments/intent_models/<ts>` | Adapter `experiments/pipeline/<job>/lora_model` |
| Quantification | INT8 dynamique (optionnel) | 4-bit NF4 QLoRA (GPU) |
| Activation | `active.json` (pointeur) + `POST /classifiers/intent/reload` | manuelle (`--model_path`) |
| Tickets | SCRUM-95 | SCRUM-12 / SCRUM-39 / SCRUM-52 |

---

## 2. Vue d'ensemble

```
                ┌────────────────────────────────────────────────────────┐
                │    Entraînement du classifieur d'intention              │
                │  (chat/action — encodeur classification, pas de génération)│
                └───────────────┬───────────────────────────────────────┘
        CLI            │          │   API (X-API-Key)
  scripts/train_intent.py │          │   POST /train/intent  (+ /api/v1)
                           │          │   GET  /train/intent/status/{job_id}
                           ▼          │   POST /train/intent/cancel/{job_id}
            core/intent_trainer.py    │   GET  /train/intent/jobs   (kind="intent")
        (Thread daemon par job)      │   GET  /train/intent/versions
          · loading_dataset            │   POST /train/intent/activate
          · splitting_dataset          │
          · loading_model              │
          · training  (Trainer HF)     │
          · saving_model               │
                           │          │
                           ▼          │
        experiments/intent_models/<horodatage>/   (version, config + poids)
          · active.json  ← pointeur version active
          · POST /classifiers/intent/reload côté runtime
                           │
                           ▼
            IntentClassifier(engine=auto) → inference chat/action
                 (fallback règles si aucune version valide)
```

---

## 3. Entrées et flux de données

### 3.1 Jeu de données — JSONL `{text, label}`

| Source | Chemin par défaut | Production |
|---|---|---|
| `IntentTrainRequest.dataset_path` | `data/intent_dataset.jsonl` | `scripts/build_intent_dataset.py` |

Format (une ligne JSON par exemple) :

```json
{"text": "Peux-tu lancer l'entraînement du modèle ?", "label": "action"}
{"text": "Merci pour ton aide", "label": "chat"}
```

`core/intent_store.default_intent_labels()` renvoie le **jeu de labels fixe**
et immuable `(chat, action)` (ordre = indices de la tête de classification) —
toute autre valeur est rejetée par validation (422/ValueError).

### 3.2 Split train/val — déterministe, stratifié, avant tokenisation

`core/intent_trainer._split_records(records, test_size=0.1, seed=42)` :

- **split stratifié** via `sklearn.model_selection.train_test_split(
  stratify=[r["label"] for r in records], random_state=42)` — la proportion
  de chaque classe en val reflète le dataset (le shuffle pouvait, par
  malchance, vider val d'une classe et fausser le classification_report §13) ;
- `test_size` ∈ ]0, 1[ validé par pydantic (`IntentTrainRequest`);
- split **sur les exemples bruts avant tokenisation** (équivalent au
  `Dataset.train_test_split(0.1, seed=42)` historique puisque la tokenisation
  est déterministe) — cela rend l'étape `splitting_dataset` observable avant le
  chargement du modèle ;
- cas limites (jamais d'échec) : un seul exemple → val vide → évaluation
  désactivée ; classe réduite à 1 occurrence (ou jeu trop petit pour
  `test_size`) → **repli shuffle** historique avec log d'avertissement.

> La version API expose `eval_strategy="epoch"` (les checkpoints ne sont pas
> conservés, cf. 5.6), donc la validation n'est évaluée qu'entre deux epochs complètes.
### 3.3 Labels et validation précoce

`core/intent_trainer._run_intent_pipeline` valide très tôt :

- **dataset vide** → `ValueError("Dataset vide.")` → job `FAILED` ;
- **labels inconnus** → `ValueError(f"Labels inconnus dans le dataset : {unknown}")`
  (ex. un label `positive` — hors `{chat, action}`) ;
- les **comptes par classe** sont loggués (détection de déséquilibre).

Ces erreurs remontent par `run_intent_training` → `FAILED` (et non par validation
pydantic à l'import, car l'API ne peut pas ouvrir le dataset) :

```python
counts = {label: sum(1 for r in records if r["label"] == label) for label in labels}
logger.info("Dataset chargé : %d lignes (%s)", len(records), counts)
```

### 3.4 Rien d'autre à préparer

Contrairement au pipeline sentiment (`label_dataset.py`), le dataset d'intention
est **déjà annoté** (labels `chat`/`action`) — il n'y a ni confidence filtering,
ni génération de labels par un modèle. L'entrée est directement exploitable.

---

## 4. Orchestration — `core/intent_trainer.py`

Refactor du CLI `scripts/train_intent.py` en module importable, exécuté dans un
**thread daemon** par la route `POST /train/intent` (même pattern que
`trainer_runner` sentiment), **sans subprocess** (le modèle est un encodeur
léger — MiniLM — que l'API peut charger dans le thread).

### 4.1 Machine à états du job

```
queued → loading_dataset → splitting_dataset → loading_model → training → saving_model → done
```

Correspond exactement à `INTENT_TRAIN_JOB_STEPS` (`core/models.py`) et à
`INTENT_TRAIN_STEPS` (`frontend/src/api/jobSteps.ts` — à garder alignés).

`run_intent_training(job_id, req)` :

1. `store[job_id] = job` ; `JobStatus.RUNNING`, `started_at`, étape `queued` ;
2. branchement du `JobLogHandler` (`job_logs.attach_job_logging(job_id)`) → logs
   du thread capturés pour le WebSocket `/train/stream/{job_id}` ;
3. délègue à `_run_intent_pipeline(job, store, job_id, req, cancel_event)` ;
4. succès → `COMPLETED`, étape `done` ; `IntentTrainingCancelled` → `CANCELLED`
   (catché **avant** `Exception`, l. 332) ; autre échec → `FAILED` +
   `_mark_step_error`.

> **Différence forte vs sentiment** (`trainer_runner`) : le runner sentiment
> attrape `Exception` **après** `cancel_training`, ce qui réécrit `CANCELLED`
> en `FAILED`. Le runner intent **ne** subit **pas** ce bug :
> `IntentTrainingCancelled` est une classe dédiée, interceptée en premier
> (l. 332), le statut `CANCELLED` est donc **conservé** — le contrat
> dashboard/422 est respecté.

### 4.2 Annulation coopérative

- `_intent_cancel_events` : registre par job, **Event pré-créé** à la création
  (`get_cancel_event(job_id)`) → source unique route/runner (pas de dict dupliqué) ;
- `cancel_intent_training(job_id)` (`POST /train/intent/cancel/{job_id}`) :
  pose l'event, persiste `CANCELLED` / étape `cancelled` /
  `error="Intent training cancelled by user"` ;
- le callback HF `_IntentJobCallback.on_step_end` lève `IntentTrainingCancelled`
  à chaque batch si l'event est levé → le `Trainer` s'arrête proprement :

```python
class _IntentJobCallback(TrainerCallback):
    def __init__(self, event: threading.Event): self.event = event
    def on_step_end(self, args, state, control, **kwargs):
        if self.event.is_set(): raise IntentTrainingCancelled()
        _update_train_progress(store, job_id, state)
```

### 4.3 Machine d'avancement `job.progress`

Structure partagée avec le sentiment (`_set_step_progress` / `_update_train_progress`)
: répartition **loading→splitting = 20 %, training = 20→90 %, saving_model = 100 %**.
Le `global_pct` progresse entre 20 et 90 selon `epoch / num_train_epochs`
### 4.4 Capture de logs et diffusion temps réel

`core/job_logs.py` : un `JobLogHandler` global capte les records du thread via
un mapping `thread ident → job_id`, puis le WebSocket `/train/stream/{job_id}`
les rejoue au dashboard :

```jsonc
{"type": "log", "seq": 42, "ts": 1700.., "level": "INFO", "step": "training", "message": "Epoch 1 évaluée | accuracy=0.941 | confiance moyenne=0.82"}
```

---

## 5. Cœur de l'entraînement

### 5.1 `IntentTrainRequest` — paramètres (validations pydantic → 422)

| Champ | Défaut | Validation |
|---|---|---|
| `dataset_path` | `data/intent_dataset.jsonl` | — |
| `base_model` | `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` | — |
| `base_model_version` | `None` | résolu vs `experiments/intent_models` (422 si invalide) → continual training |
| `epochs` | `3` | entier > 0 |
| `batch_size` | `32` | entier > 0 |
| `learning_rate` | `2e-5` | > 0 |
| `max_length` | `64` | entier > 0 |
| `test_size` | `0.1` | ∈ ]0, 1[ |
| `quantize_int8` | `False` | — |
| `activate` | `False` | active la version post-entraînement |

> Même jeu de validation que le CLI historique (`scripts/train_intent.py`),
> exposé en 422 côté API : `POST /train/intent` vérifie `dataset_path` et
> `base_model_version` avant la création du job.

### 5.2 Tokenisation & dataset

```python
padding_cfg = _padding_bucket_config(req.max_length)  # §13 checklist #3
train_ds = Dataset.from_list(
    [{"text": r["text"], "labels": labels.index(r["label"])} for r in train_records]
).map(lambda b: tokenizer(b["text"], **padding_cfg["tokenizer"]), batched=True)
```

- tokenisation **sans padding fixe** (`padding=False`, troncature à
  `max_length`) ; le padding est appliqué **par batch** par
  `DataCollatorWithPadding(padding=True)` → chaque batch est padé à sa
  longueur réelle maximale (fini le gaspillage sur les courtes phrases
  `chat`/`action`, cf. §13 §1c) ;
- **bucketisation** : `TrainingArguments(train_sampling_strategy=
  "group_by_length")` (v5 — remplace `group_by_length=True` retiré) regroupe
  les échantillons par longueur (LengthGroupedSampler HF) → moins de padding,
  RAM/CPU libérés pour monter `batch_size` ;
- `labels` est un **entier par exemple** (indice de classe), pas de masquage
### 5.3 Modèle — encodeur à tête de classification

`AutoModelForSequenceClassification.from_pretrained(base, num_labels=2)` →
sortie `[batch, 2]` → indices `chat`/`action`. Continental training (§13 §4b) :
recharge poids + tokenizer depuis `experiments/intent_models/<base_model_version>`.

Pas de `use_cache=False` nécessaire (pas de génération) — le runner n'y gaspille
rien.

### 5.4 Métriques — `eval_strategy="epoch"`

`_compute_metrics` (par epoch, si split val existant) :

```python
exp = np.exp(logits - logits.max(axis=-1, keepdims=True))
probs = exp / exp.sum(axis=-1, keepdims=True)
preds = probs.argmax(axis=-1)
top = probs[np.arange(n), preds]          # confiance du top choix
diag = _intent_classification_report(preds, labels_true, labels)  # cf. §13
logger.info("Classification report (chat↔action):\n%s",
            _format_intent_report(diag, labels))
return {
    "eval_accuracy": float((preds == labels_true).mean()),
    "eval_avg_confidence": float(top.mean()),       # ~0.5 = modèle qui hésite
    "eval_below_60pct": float((top < 0.6).mean()),   # part < 60 % confiance
    "eval_f1_macro": diag["f1_macro"],               # F1 macro (Nouveau §13 #1)
    "eval_f1_chat": diag["f1_per_class"]["chat"],    # F1 par classe
    "eval_f1_action": diag["f1_per_class"]["action"],
    "eval_confusion_matrix": diag["confusion_matrix"],
}
```

Le callback `_IntentJobCallback` :
- `on_step_end` → test annulation + `_update_train_progress` ;
- `on_evaluate` → `_persist_epoch_metrics(...)` → table `train_metrics`
  (upsert `ON CONFLICT(job_id, epoch)`, **partagée** avec le sentiment, diffusée
  par `/train/stream` sans changement de format). Le champ `f1_macro` est
  désormais renseigné avec `eval_f1_macro` (auparavant systématiquement `NULL`),
  et la ligne de log d'epoch inclut `f1_macro=...`.

### 5.5 `TrainingArguments` — choix structurants

| Option | Valeur | Justification |
|---|---|---|
| `num_train_epochs` | `req.epochs` (3) | paramètre sweep |
| `per_device_train_batch_size` | `req.batch_size` (32) | élevé — tenu de charge ; RAM à surveiller |
| `learning_rate` | `req.learning_rate` (2e-5) | typique encodeur |
| `eval_strategy` | `"epoch"` si val sinon `"no"` | évite l'overhead sur petits datasets |
| `logging_steps` | `20` | logs serveur réguliers |
| `save_strategy` | `"no"` | **une seule version finale** (pas de checkpoints) |
| `seed` | `42` | reproductibilité |
| `report_to` | `[]` | pas d'expérimentation externe |
| `disable_tqdm` | `True` | logs serveur propres |

> `save_strategy="no"` (contrairement au sentiment `save_steps=200`) : le modèle
> est écrit **une seule fois**, à l'étape `saving_model`. Aucun checkpoint
> intermédiaire → on ne retient **pas** le « meilleur » (epoch) en mémoire, mais
> la métrologie par epoch est quand-même **persistée** (`train_metrics`) et
> affichée.
  token-level — **aucun bug de collator** ici (contrairement au LLM causal,
### 5.6 Entraînement → quantification → sauvegarde

```python
trainer.train()
if eval_ds is not None:
    m = trainer.evaluate()
    logger.info("Évaluation finale : accuracy=%.3f, confiance moyenne=%.3f (%.1f%% < 60%%)",
                m.get("eval_accuracy",0.0), m.get("eval_avg_confidence",0.0),
                m.get("eval_below_60pct",0.0)*100.0)
if req.quantize_int8:
    model = torch.quantization.quantize_dynamic(model, dtype=torch.qint8)   # +1.5–2x léger
_save_model(model, tokenizer, output_dir)   # config.json + model.safetensors + tokenizer
```

- la quantification est **post-entraînement, pré-sauvegarde** — elle n'affecte
  ni la métrologie, ni la structure `config.json`/`active.json` ;
- `_save_model` → `model.save_pretrained` + `tokenizer.save_pretrained` ;
- version = horodatage `%Y%m%dT%H%M%SZ` (collision → suffixe `-job_id[:8]`) sous
  `experiments/intent_models/`.

---

## 6. Inférence, versions et activation

### 6.1 Store de versions — `core/intent_store.py`

- `INTENT_MODEL_ROOT = experiments/intent_models` (séparé de `experiments/models`
  sentiment) ;
- version valide = `config.json` parsable **et** poids non vide
  (`model.safetensors` / `pytorch_model.bin` / `model.pt`) ;
- `resolve_intent_model_dir(name=None)` : → version explicite → `active.json` →
  dernière valide → `RuntimeError` (déclenche le **fallback règles**) ;
- `list_intent_model_versions()` → noms triés DESC.

### 6.2 Activation — pointeur `active.json`

```json
{"active": "20260905T120000Z", "updated_at": "2026-09-05T12:00:00+00:00"}
```

- POST `/train/intent/activate` → `set_active_intent_version(version)` ;
- **store vs runtime sont séparés** : poser `active.json` ne recharge pas le
  classifieur en mémoire — le dashboard chaîne `POST /classifiers/intent/reload`
  (cf. `CLASSIFIERS.md` §6 et `IntentClassifier` côté `ia/agent/classifiers/`) ;
- même convention que le sentiment (`core/model_activation.py` pour `active.json`).

### 6.3 Inférence — `IntentClassifier` (résumé, cf. `CLASSIFIERS.md` §2-3)

`engine="auto"` : modèle actif (MiniLM) → sinon repli **règles lexicales**
déterministes (`fallback_intent` via `IntentClassifier` + `fallback.py` :
seuil safety `action`; sous le seuil → `chat`).
Surcharge CPU minimale (~2–8 ms), fallback garantit la disponibilité.

---

## 7. Exposition API et contrat

### 7.1 Surface (parité legacy ↔ versionnée)

| Méthode | Route | Auth | Description |
|---|---|---|---|
| `POST` | `/train/intent` | X-API-Key | 202 + `TrainJob` `kind="intent"`, validation dataset/version |
| `GET` | `/train/intent/status/{job_id}` | X-API-Key | statut / étape / erreur |
| `POST` | `/train/intent/cancel/{job_id}` | X-API-Key | annulation coopérative |
| `GET` | `/train/intent/jobs` | X-API-Key | paginé, **filtre `kind="intent"`** (+ status, limit/offset) |
| `GET` | `/train/intent/versions` | X-API-Key | versions valides + `active` |
| `POST` | `/train/intent/activate` | X-API-Key | pointe `active.json` (422 si inconnue) |

- **Legacy** : `api/routes/intent_train.py` (logique directe) ;
- **v1** : `api/routes/v1/intent_training.py`, monté sous `/api/v1`,
  **strangler** — délègue aux use-cases (`app.application.intent_training_usecase`)
  via des **ports** (`IntentTrainingRunnerPort`, `IntentVersioningPort`,
  `TrainingJobsPort`) injectés par `api/dependencies/composition.py`.

> Parité stricte : validations 422, messages d'erreur legacy, store partagé
> (`kind` distingue l'historique du dashboard).

### 7.2 TrainJob pour l'intention

- `kind="intent"` (filtre côté `/train/intent/jobs` et UI) ;
- `model_path` = chemin de la version `experiments/intent_models/<ts>` après save ;
- `progress` : structure identique au sentiment (steps / global_pct).
- `progress` : structure identique au sentiment (steps / global_pct).

`kind="intent"` filtre l'historique du store partagé (`experiments/jobs.db`,
`JOB_STORE_PATH`) : pas de collision sentiment/pipeline.

---

## 8. Persistance et artefacts

### 8.1 Store SQLite — `core/job_store.py`

- `PersistentJobStore` (dict + SQLite), **partagé** sentiment/intention/pipeline ;
- table `jobs` avec colonne `kind` (`intent`/`sentiment`/`pipeline`) ;
- table `train_metrics` (upsert `ON CONFLICT(job_id, epoch)`) **partagée** →
  un format WebSocket `/train/stream` unique ;
- nettoyage vieillissant : `cleanup_old_jobs.py` (`JOB_STORE_PATH` /
  `--db-path`).

### 8.2 Artefacts — `experiments/intent_models/<version>/`

```
experiments/intent_models/
├── 20260905T120000Z/            # version horodatée (collision → -job_id[:8])
│   ├── config.json              # num_labels=2
│   ├── model.safetensors        # poids ; int8 si quantifié
│   ├── tokenizer.json           # tokenizer lent (use_fast=False)
│   └── special_tokens_map.json
├── 20260901T084533Z-abc12345/   # suffixe collision
└── active.json                  # pointeur → version active
```

**Aucun checkpoint intermédiaire** (`save_strategy="no"`) : la version est
remplacée ou non.

---

## 9. Cycle de vie et reprise

```
POST /train/intent ──▶ pending ──▶ RUNNING ──▶ COMPLETED | FAILED | CANCELLED
```

- **Annulation** : Event pré-créé + callback `on_step_end` (batch) →
  `IntentTrainingCancelled` interceptée *avant* `Exception` (l. 332) → statut
  `CANCELLED` conservé (contre-exemple du bug sentiment §4.1) ;
- **Reprise** : `save_strategy="no"` ⇒ relancer net ; **continental training**
  (`base_model_version`) = seul mécanisme d'affinage incrémental.
   (`base_model_version`) = seul mécanisme d'affinage incrémental.

---

## 10. Contraintes d'exécution, matériel et tests

- **Machine** : encodeur léger (MiniLM, 12 M params) → entraînement **CPU
  possible** (lent) ou GPU (`torch` pické automatiquement par HF) ;
- **RAM** : `batch_size=32` × `max_length=64` ~ 64 Mo + poids → OK CPU, le
  frein devient les gros encodeurs ;
- **Import lazy** : `torch`/`transformers`/`datasets` importés **dans le thread
  job** (l. 391-399) → l'API reste importable sans ces dépendances ;
- **CUDA pas obligatoire** — contraste avec le LLM causal (`docs/FINETUNING.md`
  §10 : QLoRA 4-bit *GPU only*).

### Tests couvrants (aucun torch / infra dans les tests API)

| Fichier | Couverture |
|---|---|
| `tests/test_intent_classifier.py` | fallback règles (`auto`/`rules`), seuil sécurité, active vs dernière version |
| `tests/test_multi_intent.py` | batch multi-exemples |
| `tests/test_api_v1_intent_training.py` | fakes ports : **422** (dataset introuvable, version source, activation) + parité legacy |
| `tests/test_intent_trainer.py` | helpers `classification_report` / `_format_intent_report` : matrice 2x2, F1 macro/par-classe, `zero_division` |
| `tests/test_api_v1_training.py` | `save_epoch_metrics`, streaming WebSocket, job terminal |
| `tests/test_job_cleanup.py` | rotation vieillissant `train_metrics` |

> Tests API v1 injectent dépendances via `app.dependency_overrides` :
> **aucun entraînement réel ni `torch`** — `start()` est factice (`PENDING`).

> Tests API v1 injectent dépendances via `app.dependency_overrides` :
> **aucun entraînement réel ni `torch`** — `start()` est factice (`PENDING`).

---

## 11. Exemples d'exécution

```bash
# CLI (hors API)
python scripts/train_intent.py --dataset data/intent_dataset.jsonl \
    --base sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 \
    --epochs 5 --batch-size 32 --lr 3e-5 --quantize-int8 --activate

# API — lancement job
curl -X POST "$API/train/intent" -H "X-API-Key:$KEY" -H "Content-Type:app/json" -d '{
  "dataset_path":"data/intent_dataset.jsonl","epochs":5,"batch_size":32,"learning_rate":3e-5}' | jq .
# suivi : GET /train/intent/status/{job_id} | WebSocket /train/stream/{job_id}

# versions / activation (store ≠ runtime : 2 appels)
curl -H "X-API-Key:$KEY" "$API/train/intent/versions"
curl -X POST "$API/train/intent/activate" -H "X-API-Key:$KEY" -d '{"version":"20260905T120000Z"}'
curl -X POST "$API/classifiers/intent/reload" -H "X-API-Key:$KEY"
```

---

## 12. Limites connues

| Point | Détail |
|---|---|
| `max_length=64` ✅ (#2) | troncature à 64 tokens (intent ~30-40) — coût ~2× réduit |
| Padding dynamique ✅ (#3) | `padding=False` + `DataCollatorWithPadding` (padding par batch) + `train_sampling_strategy="group_by_length"` (v5) |
| LR constant `2e-5` | aucun scheduler (warmup/cosinus) dans `TrainingArguments` |
| `save_strategy="no"` | aucun « meilleur checkpoint » — une seule version finale |
| Métriques | accuracy + confiance ; **f1/classification report présents depuis §13 #1** |
| Split stratifié ✅ (#2) | `train_test_split(stratify=labels)` — répartition préservée ; repli shuffle si classe à 1 occurrence |
| `fp16` absent | inutile en CPU ; manquant si GPU présent |
| Quantif. | INT8 dynamique seulement (pas de GPTQ/NF4) |
| Continental | reprise via `base_model_version` (pas de checkpoint intermédiaire) |
| Store vs runtime | `active.json` ≠ rechargement → 2 appels (activate + reload) |
| Store vs runtime | `active.json` ≠ rechargement → 2 appels (activate + reload) |

---

## 13. Plan d'optimisation — leviers à fort impact

> Construit à partir du code (§5) + contexte ML. Priorité **données >
> tokenisation > scheduler > modèle** ; nuance GPU/CPU.

### Diagnostic (1) : classification report — ✅ fait (§13 checklist #1)

`_compute_metrics` expose désormais le **`classification_report` sklearn** dans
`on_evaluate` (helper pur `_intent_classification_report` + formateur
`_format_intent_report` dans `core/intent_trainer.py`, parité CLI) : rapport
précision/rappel/F1 par classe + macro, **matrice de confusion** logguée à
chaque évaluation → les confusions `chat ↔ action` sont visibles, y compris
dans le flux `/train/stream` (capture `core/job_logs`). Le F1 macro est aussi
persisté dans la table `train_metrics` (champ `f1_macro`, auparavant `NULL`).

### 📊 1. Dataset (levier le plus puissant — **compatible CPU**)

- **1a. Hard-exemples frontières** : phrases où `chat`/`action` se chevauchent
  (*"Tu peux me dire si c'est faisable ?"*) — les cas où `eval_below_60pct`
  met en garde ; le classification report (§13 Diagnostic) pointe les phrases
  à ajouter (priorité absolue si confusion `chat↔action`).
- **1b. Stratified split** — ✅ fait (§13 checklist #2) : `_split_records`
  utilise `sklearn.model_selection.train_test_split(stratify=labels)` →
  répartition des classes préservée en val ; repli shuffle documenté si une
  classe n'a qu'un seul exemple (ou jeu trop petit).
- **1c. Tokenisation dynamique + bucketisation** — ✅ fait (§13 checklist #3)
  : `_padding_bucket_config` (source unique CLI/API) — `padding=False`
  (troncature seule) → `DataCollatorWithPadding(padding=True)` +
  `TrainingArguments(train_sampling_strategy="group_by_length")` (v5,
  LengthGroupedSampler HF) : chaque batch padé à sa longueur réelle,
  échantillons regroupés par longueur → moins de padding → RAM/CPU libérés
  pour monter `batch_size` (compatible CPU).

### ⚙️ 2. Tokenisation & batch (compatible CPU)

- **2a. `max_length=64`** (intent ~30-40 tokens) — ✅ fait (§13 checklist #2) : coupe le coût ~2× → libère RAM.
- **2b. `batch_size=64`** si RAM le permet → époque +1.5× plus rapide
  sans perte de convergence (encodeur, LR fixe).

### 🔁 3. Scheduler & checkpoint (compatible CPU / GPU)

| Changement | Pourquoi |
|---|---|
| `lr_scheduler_type="cosine"` + `warmup_ratio=0.1` | LR 2e-5 constant est défensif ; cosinus+warmup converge mieux >3 epochs |
| `metric_for_best_model="accuracy"` + `load_best_model_at_end=True` + `save_strategy="epoch"` + `save_total_limit=2` + `EarlyStoppingCallback(patience=2)` | **contrebalancer** §12 (`save_strategy="no"`) — nécessite de choisir ce mode |

> ⚠️ Trade-off §5.5 : choisir **version horodatée** (actuel,
> `save_strategy="no"`) **ou** meilleur-checkpoint
> (`save_strategy="epoch"`) — ne pas faire les deux sans `load_best_model_at_end`.

### 📈 4. Modèle + fort (GPU)

- **4a. Passer à `intfloat/multilingual-e5-small`** (35 M, fine-tunable) ou
  `paraphrase-MiniLM-L3-v2` → gain OOD notable sur FR nègre/sarcasme.
- **4b. Continental training** (`base_model_version`) : ré-affiner la version
  active sur les nouveaux cas utilisateurs ⇒ *continual learning* (supporté).

### 🔍 5. Inférence (déploiement)

- **5a. Recalibrer le seuil `action`** du fallback (`fallback_intent` dans
  `ia/agent/classifiers/fallback.py`) sur la val — baisser si trop conservateur.
- **5b. `fp16` en inférence GPU** (`torch_dtype=float16`) → latence ↓.

### Matrice décision — GPU disponible ?

| Levier | CPU only | GPU |
|---|---|---|
| Hard-exemples / stratifié ✅ / padding-dynamique ✅ | ✅ prioritaire | ✅ prioritaire |
| `max_length=64` | ✅ | — |
| `batch_size=64` | ⚠️ (RAM) | ✅ |
| Scheduler cosine + warmup | ✅ | ✅ |
| `load_best_model_at_end` + `save_strategy="epoch"` | ✅ (disque) | ✅ |
| Modèle + fort (E5-small) | ⚠️ lent | ✅ |
| `fp16` inférence | ✗ | ✅ |

### Checklist priorisée (impact ★★★)

- [x] **#1** — `classification_report` (confusions `chat↔action`) ;
- [x] **#1** — stratified split dans `_split_records` ;
- [x] **#1** — tokenisation `padding=True` + bucketisation par longueur ;
- [x] **#2** — `max_length=64` ;
- [ ] **#3** — scheduler `cosine` + `warmup_ratio=0.1` ;
- [ ] **#3** — `load_best_model_at_end` + `save_strategy="epoch"` + early stopping
  (au lieu du mode horodatage) ;
- [ ] **#4** — monter vers `intfloat/multilingual-e5-small` (continental training) ;
- [ ] **#5** — recalibrer seuil `action` + `fp16` inférence GPU.

> **Premier levier sans GPU (restant)** : scheduler `cosine` +
> `warmup_ratio=0.1` (§3) — stabilise la fin de convergence, sans surcoût.
> Classification report ✅ (#1), stratified split ✅ (#2), padding dynamique +
> bucketisation ✅ (#3), `max_length=64` ✅ (#2) : le report (#1) montre *quoi* améliorer dans le dataset.
