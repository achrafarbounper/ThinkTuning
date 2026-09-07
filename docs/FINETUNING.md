# Fine-tuning LLM — Architecture technique

> Documentation technique de la partie **fine-tuning d'un LLM causal** (LoRA / QLoRA)
> pour l'analyse de sentiments FR/EN — depuis le dataset labelé jusqu'à l'inférence
> sur le modèle fine-tuné.

Cette documentation décrit l'architecture, le flux de données, le contrat API, la
persistance, le cycle de vie des jobs et les contraintes d'exécution de la chaîne
de fine-tuning. Elle complète `ARCHITECTURE.md` et `docs/CLASSIFIERS.md`, sur le
même format et les mêmes conventions.

---

## 1. Périmètre et mise en situation

Le projet exposait historiquement un classifieur de sentiment **DistilBERT**
(encodeur `AutoModelForSequenceClassification`). La partie fine-tuning ajoute une
deuxième voie : un **LLM causal génératif** (par défaut
`TinyLlama/TinyLlama-1.1B-Chat-v1.0`) adapté par **PEFT LoRA / QLoRA** et utilisé
par génération de texte (« produce `positive|neutral|negative` ») plutôt que par
tête de classification.

La chaîne complète, dite **pipeline end-to-end**, enchaîne trois étapes :

```
texte brut ──▶ 1. Labeling DistilBERT   ──▶ 2. Filtrage confidence  ──▶ 3. Fine-tuning LLM
                (label_dataset.py)          (min_confidence)          (finetune_llm.py)
```

Références historiques :

| Ticket | Contenu |
|---|---|
| SCRUM-12 | Création de `finetune_llm.py` (LoRA/QLoRA, CLI autonome). |
| SCRUM-39 | Pipeline end-to-end `label_dataset → finetune_llm` en une commande (`pipeline.py`) + jobs asynchrones persistés (`core/pipeline_runner.py`, routes `/pipeline`). |
| SCRUM-52 | Benchmark `DistilBERT vs LLM fine-tuné` (`benchmark.py`, `predict_llm.py`). |
| SCRUM-106 | Branche courante (déploiement/calibrage) — le fine-tuning reste inchangé. |

---

## 2. Vue d'ensemble

```
                    ┌─────────────────────────────────────────────────────────────┐
   entrée            │    Pipeline end-to-end (labeling → filtrage → fine-tuning)  │
   CSV/JSON/JSONL    │                                                             │
   TXT ─────────────▶│  pipeline.py (CLI)        api/routes/pipeline.py (API)      │
                     │        │                    api/routes/v1/pipeline.py        │
                     │        └──────────┬───────────────────────────┘             │
                     │                   ▼                                          │
                     │   core/pipeline_runner.py  (Thread daemon par job)          │
                     │   · run_labeling     → label_dataset.py (DistilBERT)       │
                     │   · filtrage         → min_confidence + garde-fou vide     │
                     │   · run_finetune     → SUBPROCESS finetune_llm.py          │
                     │   · job persisté     → core/job_store (SQLite jobs.db)     │
                     └───────────────┬─────────────────────────────────────────────┘
                                     ▼
                 ┌─────────────────────────────────────────────────┐
                 │  finetune_llm.py — fine-tuning LoRA / QLoRA      │
                 │  · Dataset HF  │ Tokenizer  │ 4-bit (QLoRA)      │
                 │  · LoraConfig  │ Trainer HF │ adapter PEFT + tok │
                 └───────┬─────────────────────────────────────────┘
                         ▼
         predict_llm.py · benchmark.py        (inférence / comparaison)
         experiments/pipeline/<job_id>/lora_model   (artefact de sortie)
```

**Règles d'architecture du fine-tuning :**

1. **Isolation process** : le fine-tuning est lancé en **subprocess**
   (`python finetune_llm.py ...`) pour isoler torch / les modèles chargés et
   **réutiliser la CLI existante sans refactor** (cf. docstring de
   `core/pipeline_runner.py`). Les imports lourds (torch, transformers, peft,
   datasets) ne sont donc jamais importés par la couche API/runner au premier plan.
2. **Réutilisation du contrat de jobs** : mêmes `TrainJob` / `JobStatus` que
   l'entraînement sentiment, avec `kind="pipeline"` pour distinguer les jobs.
3. **Zéro dérive de DTO** : les routes versionnées `/api/v1/pipeline` délèguent
   aux handlers legacy et réutilisent `core.models.PipelineRequest`.
4. **Persistance** : store SQLite partagé (`experiments/jobs.db`,
   surchargeable par `JOB_STORE_PATH`).

---

## 3. Entrée et flux de données

### 3.1 Formats d'entrée acceptés

L'étape de labeling (`label_dataset.py → load_texts_from_file`) accepte :

| Format | Lecture | Notes |
|---|---|---|
| `.csv` | `csv.DictReader` (`utf-8-sig`) | colonne `text` (ou `--text_column`) ; BOM ignoré |
| `.json` | liste, ou objet `{records: [...]}` / `{data: [...]}` | |
| `.jsonl` | une ligne = un objet | |
| `.txt` / `.md` | une ligne = un texte | |

Les textes sont **lecture seule** à ce stade : le pipeline n'écrit que le JSONL
Alpaca intermédiaire et l'adapter de sortie.

### 3.2 Données intermédiaires : format Alpaca JSONL

Après labeling + filtrage, chaque record conservé est exporté en JSONL Alpaca
(`label_dataset.py → build_alpaca_record`) :

```json
{"instruction": "Classify the sentiment of the following text as negative, neutral, or positive.",
 "input": "Ce produit est absolument fantastique !",
 "output": "positive",
 "confidence": 0.97}
```

- `instruction` : fixe (`DEFAULT_INSTRUCTION`) ;
- `input` : le texte à classifier ;
- `output` : la classe prédite par DistilBERT (text cible pour le LLM) ;
- `confidence` : confiance du prédicteur — **non utilisée par le fine-tuning**
  (elle a déjà servi au filtrage).

Ces records sont normalisés côté fine-tuning (`normalize_record`) dans le template
`DEFAULT_TEMPLATE` :

```
### Instruction:
{instruction}

### Input:
{input_text}

### Response:
```

### 3.3 Résumé du flux de données end-to-end

```
CSV/JSON/JSONL/TXT ─▶ label_dataset.py ─▶ [filtrage min_confidence] ─▶ labeled.jsonl
                                                                       (Alpaca JSONL)
        ──▶ finetune_llm.py (train/val split 90/10) ──▶ experiments/pipeline/<job>/lora_model
                                                                       (adapter PEFT + tokenizer)
        ──▶ predict_llm.py (génération greedy) ──▶ {sentiment, confidence}
```

---

## 4. Orchestration asynchrone — `core/pipeline_runner.py`

Module central partagé par le CLI (`pipeline.py`) et l'API (`/pipeline`),
conformément à `api/routes/pipeline.py` (« même pattern de jobs que /train »).

### 4.1 Machine à états du job

Étapes canoniques (miroir de `frontend/src/api/jobSteps.ts`) :

```
queued → labeling → filtering → finetuning → done
```

Statuts possibles (`JobStatus`) : `pending → running → completed | failed | cancelled`.

### 4.2 `run_pipeline(job_id, req)` — thread daemon

1. Marque le job `RUNNING`, enregistre `started_at` ;
2. Résout les chemins de sortie : `req.labeled_output` / `req.output_dir` si
   fournis, sinon `experiments/pipeline/<job_id>/{labeled.jsonl, lora_model}`
   (`default_paths`) ;
3. **Étape labeling** : `run_labeling()` → `label_dataset.label_dataset()` qui
   applique déjà le filtrage par confidence et écrit le JSONL Alpaca ;
4. **Étape filtering** : positionne `job.model_path = labeled_output`, puis
   **garde-fou** : si `0 record` au-dessus du seuil, le pipeline passe `FAILED`
   avec un message explicite — le fine-tuning **n'est jamais lancé** sur un
   dataset vide ;
5. **Étape finetuning** : `build_finetune_cmd()` puis `run_finetune()` en
   subprocess annulable ;
6. Succès → `job.model_path = output_dir`, statut `COMPLETED`, étape `done` ;
   exception → `FAILED` (ou `CANCELLED` si l'event d'annulation est levé).

### 4.3 `build_finetune_cmd(params, train_file, output_dir)`

Construit la liste d'arguments du subprocess. **Règle de contrat** :
un paramètre `None` n'émet PAS l'argument → `finetune_llm.py` applique sa valeur
par défaut. `use_qlora` est toujours explicité (`--use_qlora` / `--no_qlora`).

### 4.4 `run_finetune(cmd, cancel_event)` — subprocess annulable

```python
process = subprocess.Popen(cmd, cwd=PROJECT_ROOT, stdout=PIPE, stderr=STDOUT,
                           text=True, encoding="utf-8", errors="replace")
```

- Boucle de supervision : `process.poll()` ; à chaque itération, l'`Event`
  d'annulation est testé avec un timeout de 2 s (`cancel_event.wait(2.0)`) ;
- **Annulation** : `process.terminate()` puis `process.wait(10)` puis
  `process.kill()` en dernier recours → `RuntimeError("Pipeline cancelled by user")` ;
- **Échec** (code ≠ 0) : les 20 dernières lignes de sortie sont logguées en erreur
  et remontées dans l'exception → job `FAILED` ;
- `cwd = PROJECT_ROOT` : `finetune_llm.py` est exécuté depuis `backend/`.

### 4.5 Annulation (`POST /pipeline/cancel/{job_id}`)

- L'`Event` d'annulation est **pré-créé** à la création du job
  (`get_cancel_event(job_id)`) : source unique partagée entre la route et le
  runner (pas de dict dupliqué) ;
- `cancel_pipeline` pose l'event, puis marque le job `CANCELLED`, étape
  `cancelled` et `error="Pipeline cancelled by user"` ;
- Points de contrôle : avant labeling, entre labeling et finetuning, et au sein
  du subprocess (poll 2 s).

---

## 5. Cœur du fine-tuning — `finetune_llm.py`

Script CLI autonome, exécuté seul ou en subprocess par le pipeline. C'est LA
source de vérité des hyper-paramètres (les `None` du `PipelineRequest` retombent
sur ces valeurs).

### 5.1 Interface CLI

```
python finetune_llm.py --train_file <jsonl> --output_dir <dir> [options]
```

| Argument | Défaut | Rôle |
|---|---|---|
| `--train_file` | **requis** | JSONL Alpaca d'entraînement |
| `--output_dir` | **requis** | Dossier de sortie de l'adapter |
| `--base_model` | `TinyLlama/TinyLlama-1.1B-Chat-v1.0` | Modèle HF de base |
| `--validation_file` | `None` | JSONL de validation (sinon split 90/10) |
| `--max_seq_length` | `512` | Longueur de contexte max |
| `--batch_size` | `2` | Batch par device |
| `--gradient_accumulation_steps` | `8` | Accumulation de gradient (batch effectif = 16) |
| `--learning_rate` | `2e-4` | Learning rate (typique LoRA) |
| `--epochs` | `3` | Nombre d'epochs |
| `--lr_scheduler_type` | `cosine` | Scheduler |
| `--warmup_ratio` | `0.05` | Ratio de warmup (converti en steps, cf. 5.5) |
| `--weight_decay` | `0.01` | Weight decay |
| `--logging_steps` | `10` | Fréquence des logs |
| `--save_steps` | `200` | Fréquence de sauvegarde des checkpoints |
| `--eval_steps` | `200` | Fréquence d'évaluation |
| `--lora_r` | `16` | Rang LoRA |
| `--lora_alpha` | `32` | Échelle LoRA |
| `--lora_dropout` | `0.05` | Dropout LoRA |
| `--target_modules` | `q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj` | Modules cibles |
| `--use_qlora` / `--no_qlora` | `use_qlora=True` | QLoRA 4-bits si GPU dispo |
| `--seed` | `42` | Reproductibilité |
| `--push_to_hub` / `--hub_model_id` | off / `None` | Publication Hub HF optionnelle |

### 5.2 Séquence d'exécution

```
set_seed
  → chargement JSONL (records dicts) → Dataset HF (train ; val = fichier ou split 10 %)
  → tokenizer (pad=eos, padding_side="right", pas de remote code)
  → quantification 4-bit ? (QLoRA + GPU)  →  BitsAndBytesConfig (nf4, double quant)
  → chargement base model (device_map="auto", dtype bf16/fp16 GPU / fp32 CPU, use_cache=False)
  → get_peft_model(LoraConfig)           →  print_trainable_parameters()
  → tokenisation supervisée (masque du prompt, labels = réponse)
  → TrainingArguments + Trainer + DataCollatorForLanguageModeling(mlm=False)
  → trainer.train() → trainer.save_model(output_dir) + tokenizer.save_pretrained()
  → [push_to_hub si demandé]
```

### 5.3 Quantification QLoRA — `build_quantization_config`

```python
if not use_qlora or not gpu_available() or BitsAndBytesConfig is None:
    return None
```

| Paramètre | Valeur |
|---|---|
| `load_in_4bit` | `True` |
| `bnb_4bit_quant_type` | `nf4` |
| `bnb_4bit_use_double_quant` | `True` |
| `bnb_4bit_compute_dtype` | `bfloat16` si supporté, sinon `float16` |

Conditions cumulatives : `--use_qlora` **ET** GPU CUDA **ET** `bitsandbytes`
disponible. Sinon chargement plein précision (cf. 5.6).

### 5.4 Chargement du modèle

| Cas | Configuration |
|---|---|
| QLoRA actif (GPU + quant) | `quantization_config` + `device_map="auto"` |
| GPU actif (sans quant) | `device_map="auto"`, `torch_dtype=bf16` (si supporté) sinon `fp16` |
| CPU | `torch_dtype=float32` (pas d'`device_map`) |

`model.config.use_cache = False` est **toujours** forcé (requis en fine-tuning).

### 5.5 Configuration LoRA

```python
LoraConfig(
    task_type=TaskType.CAUSAL_LM,
    r=16, lora_alpha=32, lora_dropout=0.05,
    target_modules=[q_proj, k_proj, v_proj, o_proj, gate_proj, up_proj, down_proj],
    bias="none",
)
```

Les **7 projections linéaires** des blocs Transformer sont adaptées (attention +
MLP) — le choix standard pour un LLM causal. `print_trainable_parameters()` loggue
le ratio de poids entraînés (≈ quelques % du modèle).

**Note `transformers 5.x`** : `warmup_ratio` a été retiré de `TrainingArguments` ;
le script convertit le ratio en `warmup_steps` :

```python
steps_per_epoch = len(train) // (batch_size * grad_accum)
warmup_steps    = int(steps_per_epoch * epochs * warmup_ratio)
```

---
### 5.6 Tokenisation supervisée — `build_tokenized_dataset`

> ⚠️ **À jour avec le plan §14.3a** : le masquage par `-100` décrit ci-dessous est
> **neutralisé à l'exécution** par `DataCollatorForLanguageModeling(mlm=False)`,
> qui réécrit `labels = input_ids.clone()` (v5.15.0). Le correctif (collator
> custom) est décrit au §14.3a. Après ce correctif, la description ci-dessous
> correspond au comportement réel.

Contrairement à un simple `DataCollatorForLanguageModeling`, seule la **réponse**
contribue (à l'intention) à la loss (le prompt est masqué à `-100`) :

```python
prefix_tokens    = tokenizer(prompt,           add_special_tokens=False, truncation=True)
response_tokens  = tokenizer(response + eos,   add_special_tokens=False, truncation=True)

input_ids = prefix_tokens["input_ids"] + response_tokens["input_ids"]
labels    = [-100] * len(prefix_tokens["input_ids"]) + response_tokens["input_ids"]
```

- le prompt est extrait via `text.rsplit("\n", 1)[0]` sur le texte complet ;
- le token `eos` du tokenizer est **appendé** à la réponse (condition d'arrêt) ;
- truncation globale à `max_seq_length` (512) ; `attention_mask` rempli à 1 ;
- `dataset.map(tokenize_example, remove_columns=...)` sur le Dataset HF.

### 5.7 `TrainingArguments` — choix structurants

| Option | Valeur | Justification |
|---|---|---|
| `eval_strategy="steps"` | `eval_steps=200` | éval régulière ; `"no"` si pas de validation |
| `save_strategy="steps"` | `save_steps=200` | checkpoints périodiques |
| `load_best_model_at_end=True` | si validation dispo | restaure le meilleur modèle (critère `loss`, `greater_is_better=False`) |
| `per_device_train_batch_size=2` | + 8 steps d'accumulation | batch effectif = 16 |
| `lr_scheduler_type="cosine"` | warmup_steps dérivé | plan classique LoRA |
| `fp16` / `bf16` | auto selon la carte | jamais posé en QLoRA-quant (géré par bitsandbytes) |
| `remove_unused_columns=False` | — | conserve les `labels` construits |
| `report_to=[]` | — | pas d'intégration tracking externe |

Le `Trainer` est instancié avec
`DataCollatorForLanguageModeling(tokenizer=..., mlm=False)` (padding dynamique).

### 5.8 Persistance de l'adapter

```python
trainer.save_model(str(output_dir))   # adapter_model.safetensors + adapter_config.json
tokenizer.save_pretrained(str(output_dir))
```

La sortie est un **adapter PEFT** (LoRA/QLoRA), **pas** un modèle fusionné :
l'inférence devra recharger le modèle de base pour appliquer l'adapter
(voir section 6).

---

## 6. Inférence sur le modèle fine-tuné — `predict_llm.py`

### 6.1 Détection adapter vs modèle fusionné — `load_model`

L'exploration du dossier `model_path` identifie un adapter PEFT par la présence
de marqueurs :

```python
adapter_markers = ["adapter_config.json", "adapter_model.safetensors", "adapter_model.bin"]
```

| Cas | Chargement |
|---|---|
| Adapter + `auto-peft` dispo | `AutoPeftModelForCausalLM.from_pretrained(path, device_map="auto")` |
| Adapter + `--base_model` | `PeftModel.from_pretrained(AutoModelForCausalLM(base), path)` |
| Modèle fusionné | `AutoModelForCausalLM.from_pretrained(path, device_map="auto")` |
| Incompatible | `ValueError` / `RuntimeError` explicites |

Le tokenizer est chargé depuis `model_path` si `tokenizer_config.json` y est
présent, sinon depuis `base_model`.

### 6.2 Génération et parsing

1. **Prompt identique à l'entraînement** (`build_prompt`) : même template Alpaca +
   la même instruction figée → alignement train/inférence ;
2. **Génération greedy déterministe** :
   `do_sample=False, temperature=0.0, max_new_tokens=32, pad_token_id=eos` —
   pas d'aléa, arrêt sur `eos` ;
3. **Parsing tolérant** (`parse_generation`) :
   - recherche de la classe via **alias FR/EN** (`positive|positif|bon|excellent…`,
     `négatif|negatif|mauvais…`, `neutral|neutre|mitigé…`) en regex mots entiers ;
   - extraction de la confiance auto-déclarée : `confidence:` / `confiance:` suivi
     d'un nombre, borné à `[0, 1]` ;
   - échec → `{sentiment: "unknown", confidence: 0.0}` + `raw_output` conservée
     (débogage).

```bash
python predict_llm.py --model_path experiments/pipeline/<job>/lora_model \
    --base_model TinyLlama/TinyLlama-1.1B-Chat-v1.0 --text "Superbe service !"
# → {"text": "...", "sentiment": "positive", "confidence": 0.94, ...}
```

### 6.3 Benchmark — `benchmark.py`

Comparaison quantitative `DistilBERT (Predictor) vs LLM fine-tuné` sur un même jeu
de test : `--llm_path` pointe l'adapter (ou le merged) et `--base_model` permet le
rechargement de l'adapter. Sortie : tableau `rich` + métriques par modèle
(précision, rappel, F1, etc. via `compute_metrics`).

---

## 7. Exposition API et contrat

### 7.1 Routes

| Méthode | Route | Auth | Description |
|---|---|---|---|
| `POST` | `/pipeline` (+ `/api/v1/pipeline`) | X-API-Key | Crée le job (`202` + `TrainJob` `kind="pipeline"`), exécution en thread daemon |
| `GET` | `/pipeline/status/{job_id}` | X-API-Key | Statut / étape / erreur du job |
| `POST` | `/pipeline/cancel/{job_id}` | X-API-Key | Annulation coopérative |
| `GET` | `/pipeline/jobs` | X-API-Key | Historique paginé (`limit` ≤ 1000, `offset`, filtre `status`), tri `started_at DESC` |

- Route legacy : `api/routes/pipeline.py` ;
- **Strangler** : `api/routes/v1/pipeline.py` délègue aux handlers legacy avec
  conversion d'erreurs HTTP (`convert_legacy_http_error`) et réutilise
  `core.models` (zéro dérive de contrat).

### 7.2 `PipelineRequest` — paramètres

| Bloc | Champs |
|---|---|
| **labeling / filtrage** | `input_path` (requis), `labeled_output`, `model_path`, `text_column="text"`, `min_confidence=0.7`, `label_batch_size=32` |
| **fine-tuning** | `output_dir`, `base_model`, `validation_file`, `epochs`, `finetune_batch_size`, `gradient_accumulation_steps`, `learning_rate`, `max_seq_length`, `lora_r`, `lora_alpha`, `lora_dropout`, `target_modules`, `use_qlora=True`, `seed=42` |

Sémantique : `None` → défaut du script cible ; `use_qlora`/`seed` ont des défauts
non-None (toujours transmis). Pour le CLI, la résolution est
**CLI > YAML > défauts** (`pipeline.py → merge_params`, sections `labeling:` /
`finetune:` du YAML, ex. `pipeline.example.yaml`).

### 7.3 `TrainJob` persisté

```python
class TrainJob(BaseModel):
    job_id: str
    status: JobStatus          # pending | running | completed | failed | cancelled
    step: str = "queued"       # queued | labeling | filtering | finetuning | done
    kind: str = "train"        # "pipeline" pour les jobs pipeline
    started_at / finished_at / error / model_path / regression / progress
```

`progress` (dict sérialisé en JSON) porte l'avancement temps réel consommé par le
dashboard (`PipelineJobTracker`, poll 4 s, étapes `PIPELINE_STEPS` de
`frontend/src/api/jobSteps.ts`).

---

## 8. Persistance et artefacts

### 8.1 Store SQLite — `experiments/jobs.db`

- `core/job_store.py` → `PersistentJobStore` (dict + SQLite), chemin via
  `JOB_STORE_PATH` (défaut `experiments/jobs.db` ; en Docker
  `/app/experiments/jobs.db`) ;
- table `jobs` persistante : survit aux redémarrages ; `kind` distingue les jobs
  pipeline des jobs sentiment/intent ;
- nettoyage : `cleanup_old_jobs.py` (purge des jobs terminés au-delà d'un âge).

### 8.2 Artefacts de sortie — `experiments/pipeline/<job_id>/`

```
experiments/pipeline/<job_id>/
├── labeled.jsonl     # Alpaca JSONL intermédiaire (étapes 1-2)
└── lora_model/       # adapter PEFT + tokenizer (étape 3)
    ├── adapter_config.json
    ├── adapter_model.safetensors
    ├── tokenizer_config.json  +  tokenizer.json + special_tokens_map.json
    └── training_args.bin  (récapitulatif TrainingArguments)
```

L'adapter n'est **pas** activé automatiquement comme version active (contrairement
au sentiment `experiments/models` / `active.json`) : il est consommé par
`predict_llm.py` / `benchmark.py` via son chemin. Les checkpoints intermédiaires
du `Trainer` (`save_steps`) restent dans `output_dir/checkpoint-*` (jetables).

---

## 9. Cycle de vie, annulation et reprise

```
                    ┌── labeling ──▶ filtering ──▶ finetuning ──▶ done
POST /pipeline ──▶ pending ──▶ RUNNING ──┤
 (202 + job)                              └── (exception) ──▶ FAILED / CANCELLED
```

- **Annulation coopérative** (pas de kill brutal du serveur) : un
  `threading.Event` par job, pré-créé à la création. Vérifié entre les étapes et
  pendant le subprocess (poll 2 s). Le `Trainer` HF est tué proprement par
  terminate/kill du subprocess — l'état du job reste cohérent en SQLite.
- **Pas de reprise après crash** : aucun checkpoint n'est rechargé
  automatiquement (les `checkpoint-*` du `Trainer` ne sont pas repris) ; un job
  `FAILED` doit être relancé. C'est un choix assumé (vs continual training
  sentiment `base_model_version`).
- **Garde-fous anti-gaspillage** : dataset vide après filtrage → `FAILED` avant
  tout lancement ; validation précoce `input_path` requis (422).

---

## 10. Contraintes d'exécution et matériel

| Environnement | Comportement |
|---|---|
| **GPU CUDA** | QLoRA activable (`--use_qlora` par défaut) : quant NF4 4-bits + double quant, compute bf16/fp16 ; sinon fp16/bf16 natif |
| **CPU** | QLoRA **désactivé d'office** (`gpu_available()` = False) → LoRA standard en `float32` (lent, mais fonctionnel) |
| **Mémoire** | Docker calibré pour 512 Mo Render : le **subprocess** du fine-tuning libère le processu API parent (DistilBERT reste en mémoire pour le labeling) ; les modèles LLM ne sont pas préchargés au boot |

Rappel dépendances (`backend/requirements.txt`, miroir `pyproject.toml`) :
`torch>=2.0`, `transformers==5.15.0`, `datasets>=2.19`, `peft>=0.11`,
`accelerate>=0.30`, `sentencepiece>=0.1.99`. L'installation CPU de torch doit
passer par l'index dédié (`--index-url .../whl/cpu`) avant le reste.

---

## 11. Tests et validation

| Fichier | Couverture |
|---|---|
| `tests/test_pipeline.py` | orchestration : `build_finetune_cmd` (None → défauts, flags LoRA/QLoRA), garde-fou dataset vide, annulation, échec subprocess, enchaînement CLI |
| `tests/test_api_v1_pipeline.py` | routes versionnées `/api/v1/pipeline` (délégation legacy + conversion d'erreurs) |
| `tests/test_label_dataset.py` | labeling + filtrage confidence + export Alpaca |
| `tests/test_predict_llm.py` | `parse_generation` : alias FR/EN, extraction de confidence, inconnu |
| `tests/test_benchmark.py` | comparaison DistilBERT vs LLM |

```bash
# Depuis backend/
python -m pytest tests/test_pipeline.py tests/test_predict_llm.py tests/test_label_dataset.py -q
```

Note : `finetune_llm.py` lui-même (la boucle d'entraînement réelle) n'a pas de
test unitaire — il s'exécute en subprocess ; les contrats d'arguments sont testés
via `build_finetune_cmd`.

---

## 12. Exemples d'exécution

### 12.1 Fine-tuning direct (CLI)

```bash
python finetune_llm.py \
    --train_file data/train_alpaca.jsonl \
    --output_dir runs/lora_model \
    --base_model TinyLlama/TinyLlama-1.1B-Chat-v1.0 \
    --epochs 3 --learning_rate 2e-4 --max_seq_length 512
```

### 12.2 Pipeline complet (CLI + YAML)

```bash
python pipeline.py --input data/unlabeled.csv --output_dir runs/lora_model \
    --config pipeline.example.yaml
```

### 12.3 Pipeline via API

```bash
curl -X POST http://localhost:8000/pipeline \
  -H "X-API-Key: $API_KEY" -H "Content-Type: application/json" \
  -d '{"input_path": "data/unlabeled.csv", "min_confidence": 0.8, "epochs": 2}'
# → 202 {"job_id": "...", "status": "pending", "kind": "pipeline", ...}

curl "http://localhost:8000/pipeline/status/<job_id>" -H "X-API-Key: $API_KEY"
curl -X POST "http://localhost:8000/pipeline/cancel/<job_id>" -H "X-API-Key: $API_KEY"
```

### 12.4 Prédiction + benchmark

```bash
python predict_llm.py --model_path experiments/pipeline/<job>/lora_model \
    --base_model TinyLlama/TinyLlama-1.1B-Chat-v1.0 --text "Très belle expérience !"

python benchmark.py --llm_path experiments/pipeline/<job>/lora_model \
    --base_model TinyLlama/TinyLlama-1.1B-Chat-v1.0
```

---

## 13. Limites connues et évolutions possibles

- **Pas de fusion des poids** : la sortie est un adapter PEFT, jamais un modèle
  merged — l'inférence exige le `base_model` (coût/RAM au chargement).
  Évolution : step de fusion `model = model.merge_and_unload()` optionnel ;
- **Pas d'activation de version LLM** : aucun mécanisme `active.json` / endpoint
  `/activate` pour les modèles LLM (contrairement au sentiment). Évolution :
  versionner `experiments/pipeline/*` dans un store de versions ;
- **Pas d'export ONNX** LLM (l'ONNX ne concerne que DistilBERT) ;
- **Métriques d'entraînement** : seules les losses `train/eval` sortent dans les
  logs du subprocess (`report_to=[]`) ; elles ne sont pas persistées dans
  `train_metrics` (contrairement au sentiment, `EpochMetric`) ;
- **`warmup_ratio → warmup_steps`** : conversion manuelle pour
  `transformers==5.15.0` (le champ a été retiré) — à surveiller en cas de
  mise à jour ;
- **Reproductibilité** : `set_seed(42)` fixe torch (CUDA inclus) ; partiel sur
  certains opérateurs non déterministes GPU ;
- **Résilience** : pas de reprise automatique sur échec réseau (download HF) ou
  OOM GPU (un `FAILED` doit être relancé).

---
---

## 14. Plan d'amélioration des performances

> Plan d'action hiérarchisé, construit à partir de la documentation ci-dessus,
> des sources installées (`transformers 5.15.0` vérifié dans le venv) et du
> contexte matériel du projet (worker GPU optionnel, Render 512 Mo en CPU).
> Chaque action est reliée à l'endroit exact du code à modifier.

### 14.1 Principe directeur — la hiérarchie réelle de l'impact

Ce pipeline souffre d'un syndrome « garbage-in / garbage-out » amplifié : le LLM
apprend depuis des **labels produits par DistilBERT**, lui-même entraîné sur un
jeu source fixe. L'effet de levier décroissant est le suivant :

| # | Levier | Impact attendu | Effort | Contrainte |
|---|--------|----------------|--------|-------------|
| 1 | Qualité du dataset supervisé (bruit du labeleur) | **Très élevé** | Moyen | Aucune (humaine) |
| 2 | Correction des écarts structurels (masquage prompt, alignement train/inf) | **Élevé** | Faible | Aucune |
| 3 | Hyper-paramètres LoRA/QLoRA (sweep guidé) | Moyen | Faible | GPU pour batch effectif |
| 4 | Inférence/parsing (stop, format strict, confiance logits) | Moyen | Faible | Aucune |
| 5 | Modèle de base plus fort + fusion | Fort *si GPU* | Élevé | GPU ≥ 4 Go VRAM |

Règle d'or : **ne pas toucher aux hyper-paramètres tant que les leviers 1 et 2
ne sont pas actés** — ils multiplient l'efficacité de tout le reste.

### 14.2 Levier 1 — Améliorer le dataset supervisé

#### a) Relever le seuil de confidence du labeleur
`min_confidence` (défaut 0.7, `PipelineRequest` / `pipeline.example.yaml`) :
monter à **0.85–0.9** pour couper le bruit de DistilBERT en amont. Puis auditer
le volume restant (`labeled.jsonl`) : si < ~500 records, le fine-tuning d'un LLM
est sur-appris — descendre à 0.80 plutôt que de garder un dataset pollué.

#### b) Réutiliser l'outillage de correction manuelle existant
Le projet a déjà le workflow de relecture du sentiment
(`sample_for_review.py`, `merge_reviewed_data.py`, active learning SCRUM-56).
L'étendre au pipeline LLM : extraire 200-300 échantillons incertains du JSONL
Alpaca, les corriger, puis les **concaténer au fichier d'entraînement** avant
`finetune_llm.py` (ou les passer via `--validation_file` pour au moins mesurer
le vrai taux d'erreur du labeleur).

#### c) Auditer la distribution de classes
Le labeleur tend à sous-produire `neutral` (biais de classe du jeu HF source).
Vérifier `labeled.jsonl` : si `neutral < 15 %`, injecter des hard negatives
explicites (ambiguïté, ton mitigé, sarcasme, double négation) labelées
`neutral` — c'est aussi le meilleur antidote contre les sorties hors format
(cf. §14.6b).

#### d) Diversifier la source textuelle
Le jeu source HF sentiment FR/EN est borné : mélanger tweets, reviews,
e-mails, chats, phrases longues augmente la robustesse OOD — l'argument
**principal** de la voie LLM face à DistilBERT (cf. §14.5). Les cas difficiles
(sarcasme, négation) sont à ajouter **au dataset d'entraînement du labeleur**
pour casser la corrélation d'erreurs : le labeleur et le LLM partagent la même
source, donc les mêmes angles morts.

#### e) Vérifier la calibration du labeleur
Rien ne garantit que la confiance de DistilBERT soit calibrée. Si le seuil n'est
pas discriminant (peu de records entre 0.7 et 0.9), calibrer le labeleur
(Platt/isotonic sur un échantillon réservé) avant d'utiliser la coupure.
### 14.3 Levier 2 — Corriger les écarts structurels (dérisquage immédiat)

#### a) Le masquage du prompt est neutralisé par le data collator — VÉRIFIÉ
`finetune_llm.py` construit des `labels` avec `-100` sur le prompt
(`build_tokenized_dataset`, cf. §5.6 « seule la réponse contribue à la loss »).
**Or** `DataCollatorForLanguageModeling(mlm=False)` **écrase cette colonne**
pendant l'entraînement : `labels = batch["input_ids"].clone()` avec seul le
padding masqué (source installée `transformers/.../data/data_collator.py:789-793`,
v5.15.0 — vérifié dans `backend/venv`).

**Conséquence** : le modèle prédit *toute* la séquence — l'instruction et l'input
en plus de la réponse. La loss est diluée sur du texte prévisible (l'instruction
est identique pour tous les exemples), le signal de la tâche est affaibli, et le
modèle apprend moins bien à *terminer* après `### Response:`.

**Correctif** — collator custom qui préserve les labels déjà masqués :

```python
from dataclasses import dataclass
import torch

@dataclass
class CompletionDataCollator:
    tokenizer: AutoTokenizer

    def __call__(self, features):
        batch = self.tokenizer.pad(features, return_tensors="pt")
        max_len = batch["input_ids"].shape[1]
        labels = torch.full((len(features), max_len), -100, dtype=torch.long)
        for i, f in enumerate(features):
            lbl = torch.tensor(f["labels"], dtype=torch.long)
            labels[i, : lbl.numel()] = lbl
        labels[labels == self.tokenizer.pad_token_id] = -100
        batch["labels"] = labels
        return batch
```

À brancher dans le `Trainer` de `finetune_llm.py` en remplacement du
`DataCollatorForLanguageModeling(mlm=False)`. Après ce fix, le §5.6 décrit le
comportement *réel* du pipeline.

#### b) Mismatch train / inférence sur le saut de ligne final
En entraînement, le prompt est découpé par `text.rsplit("\n", 1)[0]` →
`...### Response:` **sans** saut de ligne final ; en inférence, `build_prompt()`
renvoie `...### Response:\n` **avec** saut final (`predict_llm.py`). L'écart d'un
token `\n` décale la distribution générée. **Action** : aligner les deux
(retirer le `\n` final des deux côtés, ou le conserver uniformément).

#### c) Consolider le template en un point de vérité unique
Trois constantes dupliquées : `DEFAULT_TEMPLATE` (`finetune_llm.py`),
`INSTRUCTION` (`predict_llm.py`), `DEFAULT_INSTRUCTION` (`label_dataset.py`).
Extraire un module partagé (ex. `core/prompt_template.py`) qui expose
instruction + template + aliasing et le faire référencer par les trois modules.
C'est le pré-requis du §14.6b : une divergence train/inf ici annule tous les
gains de format strict.
### 14.4 Levier 3 — Réglages LoRA/QLoRA (sweep guidé)

**Ordre d'importance** : `learning_rate` > `lora_r/alpha` > `warmup` > `epochs`.
L'early stopping est **déjà actif** (`load_best_model_at_end=True`, critère
loss eval, `save_strategy`/`eval_strategy="steps"`, §5.7) : on peut donc allonger
les epochs sans risque d'overfit durable — le meilleur checkpoint est restauré.

Grille de départ (**un seul facteur à la fois**, `--seed 42` figé, même split) :

| Paramètre | Baseline | Grille | Note |
|---|---|---|---|
| `--learning_rate` | `2e-4` | `{1e-4, 2e-4, 5e-4}` | levier n°1 ; `2e-4` est un bon défaut LoRA |
| `--lora_r` / `--lora_alpha` | `16` / `32` | `{16/32, 32/64, 64/128}` | garder α/r ≈ 2 ; OK en QLoRA jusqu'à 64 |
| `--warmup_ratio` | `0.05` | `{0.05, 0.10, 0.15}` | petits datasets → warmup plus long stabilise |
| `--epochs` | `3` | `{3, 5, 8}` | dataset < 50k lignes ; early stopping retient le meilleur |
| `--batch_size` × `--gradient_accumulation_steps` | `2` × `8` = **16** | GPU : `4` × `16` = **64** | batch effectif doublé → multiplier LR par ≈1,5–2 |

Caveats concrets :
- **CPU / Render 512 Mo** : garder `batch_size=2` (contrainte RAM, §10). Une
  augmentation de `gradient_accumulation_steps` n'ajoute pas de RAM mais ralentit
  chaque step — à utiliser uniquement pour stabiliser.
- `target_modules` : conserver les 7 projections attention+MLP (bon compromis).
  Ajouter `embed_tokens`/`lm_head` est un gain possible sur l'adaptation du
  vocabulaire FR de TinyLlama, mais ajoute ~130 M params entraînés et de la RAM —
  **réservé au GPU**, et à mesurer avant de garder (pas de défaut 512 Mo).
- Pas de label smoothing applicable ici : la cible est un texte discret, la
  cross-entropy token est déjà le bon objectif.

### 14.5 Levier 4 — Modèle de base plus fort (GPU requis)

Même chaîne de code, seul `--base_model` change. Échelle pragmatique :

| Modèle | Taille | VRAM QLoRA (4-bit) | Gain qualitatif attendu |
|---|---|---|---|
| `TinyLlama/TinyLlama-1.1B-Chat-v1.0` (baseline) | 1.1 B | ~2-3 Go | — |
| `Qwen/Qwen2.5-3B` ou `google/gemma-2-2b` | 2-3 B | ~4-6 Go | FR correct, instruction following |
| `microsoft/Phi-3.5-mini-instruct` | 3.8 B | ~6-8 Go | très bon ratio qualité/taille |
| `mistralai/Mistral-7B-Instruct-v0.3` | 7 B | ≥ 8 Go | haute qualité, coût élevé |

**Nuance d'expertise cruciale** : pour une **classification à 3 classes dans le
domaine**, un encodeur (DistilBERT amélioré, ou `MiniLM` / `mDeBERTa` multi-langue)
dépasse souvent un LLM causal 1.1B en F1 — c'est exactement ce que le benchmark
SCRUM-52 (`benchmark.py`, DistilBERT vs LLM) doit révéler. La voie LLM se justifie
par la **robustesse hors distribution** (phrases courtes, FR oral, code-switch,
instructions), pas nécessairement par le F1. Mesurer les deux axes.

**Contrainte plateforme** : Render 512 Mo ne peut pas exécuter le fine-tuning LLM
(§10). L'expérimentation suppose un worker GPU dédié (Colab/HFU/box GPU) ; seul
l'adapter résultant est ensuite déployé pour l'inférence.
### 14.6 Levier 5 — Inférence robuste et parsing

#### a) Arrêt de génération contrôlé (stop strings)
`predict_llm.py` génère en greedy avec `max_new_tokens=32` sans condition d'arrêt
autre que `eos`. `transformers 5.15.0` supporte `stop_strings` dans
`GenerationConfig` / `generate` (vérifié dans le venv) :

```python
generated = model.generate(
    **inputs,
    max_new_tokens=16,               # 32 → 16 suffit pour un label
    stop_strings=["###", "\n"],      # coupe tout débordement hors format
    do_sample=False, temperature=0.0,
    pad_token_id=tokenizer.eos_token_id,
    eos_token_id=tokenizer.eos_token_id,
)
```

Cela élimine les réponses tronquées et les bouillies de tokens → le parsing
travaille sur du texte propre.

#### b) Contrainte de vocabulaire dans le prompt
Ajouter la consigne en fin d'instruction (dans le **template partagé** du
§14.3c, donc aligné train/inf) :

> « Réponds uniquement par l'un de ces mots exacts : negative, neutral, positive. »

Un petit modèle apprend moins de sorties hors format si la contrainte est posée
au moment de l'apprentissage ET de l'inférence.

#### c) Confiance calibrée par logits (remplace la confiance auto-déclarée)
Actuellement la confidence est **parseée dans le texte généré**
(`parse_generation` : `confidence:` / `confiance:`), donc hallucinable.
À la place, dériver la confiance des **logits du premier token généré**
(`output_scores=True, return_dict_in_generate=True`, softmax sur les ids des
tokens de label) :

- non calibrée mais beaucoup plus stable que le texte auto-déclaré ;
- base idéale pour une calibration Platt/isotonic (§14.7c).

#### d) Passe de repli sur `unknown`
Si le parsing renvoie `unknown` (ou une classe hors {negative, neutral,
positive}), rediriger vers DistilBERT (`Predictor`) ou le `FallbackClassifier`
lexical `ia/agent/classifiers/fallback.py` — déjà disponibles. La disponibilité
prime sur la perfection : meilleure latence totale qu'un régénération.

### 14.7 Optimisations avancées

| # | Optimisation | Détail | Garde-fou |
|---|---|---|---|
| a | **Fusion LoRA → modèle merged** (`merge_and_unload()`) | gain vitesse/stabilité à l'inférence (plus de `--base_model`) | en QLoRA, fusion = déquantification → RAM ×2 ; fusionner **hors** du training |
| b | **Continual training LLM** | repartir d'un adapter existant en initialisation (`PeftModel.from_pretrained(base, adapter)`) — symétrique de `base_model_version` (sentiment) | sauvegarder l'adapter source avant re-training |
| c | **Calibration post-training** | Platt / isotonic sur la confiance logits (§14.6c), sur un échantillon réservé | ne jamais calibrer sur le train ; ré-étalonner au replay |
| d | ~~Export ONNX LLM~~ | non rentable pour un génératif causal | non inscrit au plan |

### 14.8 Protocole d'évaluation (mesurer avant/après)

1. **Verrouiller un split de validation externe** : passer `--validation_file`
   (fixe) au lieu du split auto `train_test_split(0.1)` (dépend de l'évolution du
   `labeled.jsonl`) → comparaisons reproductibles entre runs.
2. **Baseline mesurée** : `benchmark.py --llm_path <adapter> --base_model <base>`
   sur un set hors distribution (**phrases longues, sarcasme, FR oral,
   code-switch**) + macro-F1 / précision par classe. Comparer au DistilBERT.
3. **Règle de décision** : ne conserver un changement que si **macro-F1 ≥ +0,5 pt**
   (seed 42, même split de validation), sinon annuler.
4. **Traçabilité** : les losses ne sont pas persistées (§13) ; à minima journaliser
   la eval loss finale par run et versionner la config — le `Trainer` écrit déjà
    `training_args.bin` dans `lora_model/`.

### 14.9 Checklist d'implémentation par ordre d'impact

| Ordre | Action | Où modifier | Impact | Contrainte |
|---|---|---|---|---|
| 1 | Relever `min_confidence` → 0.85-0.9 (+ audit volume) | `pipeline.example.yaml`, `PipelineRequest` (défaut) ; CLI | ⭐⭐⭐ | aucune |
| 2 | Corriger le collator (masquage prompt) | `finetune_llm.py` → `Trainer(data_collator=...)` | ⭐⭐⭐ | aucune |
| 3 | Réconcilier le `\n` final train/inf | `finetune_llm.py` (rsplit) + `predict_llm.py` (build_prompt) | ⭐⭐ | aucune |
| 4 | Template unique + format strict (consigne vocabulaire) | nouveau `core/prompt_template.py` ; 3 modules | ⭐⭐⭐ | aucune |
| 5 | Relecture manuelle 200-300 cas + hard negatives `neutral` | workflow review existant ; concat au train | ⭐⭐⭐ | humaine |
| 6 | Stop strings + `max_new_tokens=16` | `predict_llm.py` `generate()` | ⭐⭐ | aucune |
| 7 | Sweep LR → r/α → warmup → epochs (early stopping actif) | `finetune_llm.py` args ; grille §14.4 | ⭐⭐ | GPU (sauf lr/warmup CPU) |
| 8 | Batch effectif 16 → 64 | `--batch_size` × `--gradient_accumulation_steps` | ⭐⭐ | GPU |
| 9 | Confiance logits (au lieu du texte) + fallback `unknown` | `predict_llm.py` | ⭐⭐ | aucune |
| 10 | Modèle de base plus fort (Qwen2.5-3B / Phi-3.5-mini) | `--base_model` | ⭐⭐⭐ | GPU ≥ 4 Go |
| 11 | Fusion LoRA + continual training + calibration | post-processing / args | ⭐ | GPU |

**Définition of Done d'une itération** : baseline `benchmark.py` avant/après sur
un set OOD figé, macro-F1 ≥ +0,5 pt retenue, config + adapter versionnés.

---

## 15. Annexes — correspondance ancienne doc / plan

- §5.6 « seule la réponse contribue à la loss » : **intention** non respectée à
  l'exécution avec `DataCollatorForLanguageModeling(mlm=False)` → corrigée par
  §14.3a.
- §5.7 `DataCollatorForLanguageModeling(mlm=False)` : à remplacer par le collator
  custom de §14.3a.
- §6.2 génération (32 tokens, pas de stop) : enrichie par §14.6a/b/c.
- §13 « Pas de fusion / pas de continual training / métriques non persistées » :
  les évolutions sont chiffrées en §14.7-14.8.