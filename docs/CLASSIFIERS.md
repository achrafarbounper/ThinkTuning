# Système de classification — Documentation

Cette documentation couvre les **deux systèmes de classification** du projet :
le **sentiment** (positif / neutre / négatif, FR/EN, DistilBERT fine-tuné) et
l'**intention** (chat / action, MiniLM ou repli règles).

Elle décrit l'architecture, les classes, l'API HTTP, le monitoring et les
scripts associés — alignée sur les conventions de `ARCHITECTURE.md`
(interface commune, une seule source de vérité, couche de service défensive).

---

## 1. Architecture

```
                    API FastAPI
        GET /classifiers   POST /classifiers/{name}/predict
        +  monitoring      +  reload (clé API)
               │
        ┌──────┴──────┐
        │  BaseClassifier (ABC)          ia/agent/classifiers/base.py
        │  · predict()  · load/reload()  · health_check()  · get_metrics()
        └──────┬──────┘
   ┌───────────┴────────────┐
   │ SentimentClassifier     │  DistilBERT (via core.predictor_cache)
   │ IntentClassifier        │  MiniLM ou repli règles (engine=auto)
   │ FallbackClassifier      │  règles lex. déterministes (aucun modèle)
   │ ResilientClassifier     │  CircuitBreaker (ia/agent/circuit_breaker.py)
   │                         │  + bascule automatique vers Fallback
   └───────────┬────────────┘
        ┌──────┴──────────┐
        │  Caches          │  core/prediction_result_cache.py (LRU + TTL)
        │  core.inference_executor / dynamic_batcher / model_warmup
        │  core.onnx_exporter (ONNX Runtime, optionnel)
        └─────────────────┘
```

**Règle du jeu** : l'inférence lourde (torch/transformers) n'est importée que
dans les chemins qui en ont besoin (imports paresseux). Les tests et les
environnements sans modèle restent légers et fonctionnels.

---

## 2. Module `ia/agent/classifiers`

| Fichier | Rôle |
|---|---|
| `base.py` | `BaseClassifier` (ABC), `PredictionResult` (dataclass normalisée), `ClassifierMetrics` (compteurs thread-safe). |
| `sentiment_classifier.py` | Classifieur de sentiment qui encapsule `core/predictor_cache.get_predictor` (inférence dans `src/inference/predictor.py`). Ajoute cache de résultats par texte, compteurs, warmup. |
| `intent_classifier.py` | Classifieur chat/action (`engine=auto` \| `rules` \| `onnx`). Sans modèle entraîné, bascule sur les règles métier. Seuil de sécurité : une `action` sous le seuil retombe en `chat`. |
| `fallback.py` | Règles lexicales (`fallback_sentiment` / `fallback_intent`) + `FallbackClassifier` + `ResilientClassifier` (wrapper CircuitBreaker → repli automatique). |
| `__init__.py` | Exports publics du package. |

### Exemple — `SentimentClassifier`

```python
from ia.agent.classifiers.sentiment_classifier import SentimentClassifier

clf = SentimentClassifier()            # modèle actif (version pointée / dernière)
results = clf.predict(["Excellent !", "Horrible."])
print([r.label for r in results])      # ["positive", "negative"]
print(clf.get_metrics())               # prédictions, cache hit rate, latence…
print(clf.health_check())              # sonde : ok/label/confiance/latence
```

### Exemple — `IntentClassifier`

```python
from ia.agent.classifiers.intent_classifier import IntentClassifier

clf = IntentClassifier()               # engine="auto"
results = clf.predict([
    "Peux-tu lancer l'entraînement du modèle ?",
    "Merci pour ton aide",
])
print([r.label for r in results])      # ["action", "chat"] (règles si pas de modèle)
```

### Exemple — `ResilientClassifier`

```python
from ia.agent.classifiers.fallback import ResilientClassifier
from ia.agent.classifiers.sentiment_classifier import SentimentClassifier

resilient = ResilientClassifier(SentimentClassifier())
results = resilient.predict(["Texte"])  # modèle OK → normal
# si le modèle échoue 3x de suite : le circuit s'ouvre, le repli règles sert
---

## 3. Stockage des modèles

### Sentiment — `experiments/models/`
Géré par `core/model_versioning.py` (la version active est résolue par
`core/predictor_cache`).

### Intention — `experiments/intent_models/`
Géré par **`core/intent_store.py`** (Phase 4) :

- chaque version = dossier horodaté `YYYYMMDDTHHMMSSZ/` avec `config.json` +
  `model.safetensors` ;
- pointeur optionnel `experiments/intent_models/active.json` ;
- `resolve_intent_model_dir(name|None)` → chemin de version valide
  (sinon `RuntimeError` → le classifieur retombe sur les règles) ;
- `set_active_intent_version(name)` → écrit le pointeur.

Entraînement d'un modèle d'intention :

```bash
# 1. Auto-labellisation d'un corpus (fichier texte ou CSV)
python scripts/build_intent_dataset.py --input conversations.txt \
    --output data/intent_dataset.jsonl

# 2. Fine-tune d'un MiniLM multilingue
python scripts/train_intent.py --dataset data/intent_dataset.jsonl \
    --base "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2" \
    --epochs 3 --activate
```

> Sans modèle entraîné, `IntentClassifier` reste 100 % fonctionnel via les
> règles métier — c'est le comportement par défaut du repo aujourd'hui.

---

## 4. API HTTP (Phase 3 & 5)

| Méthode | Route | Clé API | Description |
|---|---|---|---|
| `GET`  | `/classifiers` | non | Liste + synthèse de santé (`summary.ok/degraded/down`). |
| `GET`  | `/classifiers/{name}` | non | Instantané complet (info, métriques, health, warmup). |
| `POST` | `/classifiers/{name}/predict` | oui | Prédiction `{texts}` → `[{text, label, confidence, probabilities?}]` (bornes anti-DoS). |
| `POST` | `/classifiers/{name}/reload` | oui | Recharge le modèle actif depuis le disque. |

Classifieurs connus d'office : `sentiment` et `intent` (créés paresseusement
dans le registre singleton au premier accès — aucun modèle chargé à l'import).

Chaque prédiction expose, quand le moteur la connaît, la **distribution
complète des classes** (`probabilities: {label: proba, …}`) — pour le moteur
`intent`, elle permet de distinguer une décision nette d'une hésitation
proche de 50/50 (modèle sous-entraîné, question ambiguë…). La clé est omise
lorsqu'indisponible.

Exemple :

```bash
curl -X POST http://localhost:8000/classifiers/intent/predict \
  -H "Content-Type: application/json" -H "X-API-Key: $API_KEY" \
  -d '{"texts": ["Peux-tu lancer l'"'"'entraînement ?"]}'
---

## 5. Couche serveur / résilience

| Module | Rôle |
|---|---|
| `core/prediction_result_cache.py` | Cache de résultats LRU + TTL (par clé normalisée texte+classifieur). |
| `core/classifier_registry.py` | Registre singleton de classifieurs (`get_or_create` paresseux). |
| `core/inference_executor.py` | Pool de threads dédié (`run` / `run_async`), non-bloquant pour l'event loop. |
| `core/dynamic_batcher.py` | Regroupement temporel des requêtes concurrentes en un lot d'inférence (fenêtre + `max_batch_size`). |
| `core/model_warmup.py` | Préchargement des modèles en arrière-plan au démarrage (`CLASSIFIER_WARMUP=0` pour désactiver en tests). |
| `core/circuit_breaker.py` | Pattern Circuit Breaker existant (réutilisé par `ResilientClassifier`). |
| `core/onnx_exporter.py` | Export PyTorch → ONNX + moteur ONNX Runtime (optimisation additive). |

Route `POST /predict/batched` (Phase 2) : corps `{texts, use_batcher}` —
batching dynamique ou inférence via l'executor.

---

## 6. Monitoring

- **Endpoints** : `GET /classifiers` (listage + synthèse) et
  `GET /classifiers/{name}` (snapshot complet) sont les points d'entrée du
  monitoring ; ils s'appuient sur `core/classifier_monitoring.py`
  (défensif : un classifieur hors-service ne fait jamais tomber le snapshot).
- **Métriques exposées** (par classifieur) : prédictions, cache hits/misses,
  hit rate, erreurs, latence moyenne, état du warmup.
- **Dashboard** : pages `SentimentPage.tsx` (mini-panel état classifieur) et
  `IntentPage.tsx` (prédiction, monitoring, cache client) — navigation ajoutée
  dans `Sidebar.tsx` / `App.tsx`.

---

## 7. Tests

| Fichier | Couvre |
|---|---|
| `tests/test_classifiers.py` | Cache LRU+TTL, métriques, `SentimentClassifier`, registre. |
| `tests/test_async_batching.py` | Batcher, executor, warmup, route `/predict/batched`. |
| `tests/test_fallback_classifier.py` | Règles, `FallbackClassifier`, `ResilientClassifier`. |
| `tests/test_onnx_exporter.py` | Export ONNX + moteur ONNX Runtime (mini-BERT hors-ligne). |
| `tests/test_intent_classifier.py` | `IntentClassifier`, `intent_store`, intégration observatoire `AgentCore`. |
| `tests/test_classifier_api.py` | E2E routes classifieurs (monitoring, prédiction, reload). |
| `tests/test_circuit_breaker.py` | Circuit Breaker (existant, réutilisé). |

```bash
python -m pytest tests/test_classifiers.py tests/test_async_batching.py \
    tests/test_fallback_classifier.py tests/test_onnx_exporter.py \
    tests/test_intent_classifier.py tests/test_classifier_api.py -q
```

---

## 8. Métriques cibles

| Métrique | Objectif |
|---|---|
| Latence sentiment p50 | 20–60 ms (CPU, non quantifié) |
| Latence intention p50 | < 10 ms (règles) / ~2–8 ms (MiniLM CPU) |
| Cold start perçu | ≈ 0 ms (warmup arrière-plan au démarrage) |
| Cache hit rate | 60–80 % (répétitions de textes) |
| Disponibilité | 99,9 % (fallback règles quand le modèle est indisponible) |
| Taille modèle intention | ≤ ~6 Mo (quantification INT8) |
| Couverture de tests | > 80 % sur la couche classifieurs |
# → {"results": [{"text": "...", "label": "action", "confidence": 0.7}]}
```
# immédiatement (le 503 disparaît), rétablissement automatique en HALF_OPEN.
```

---

## 9. Entraînement du classifieur d'intention (SCRUM-95)

L'entraînement du classifieur d'intention n'est plus réservé au CLI
(`scripts/train_intent.py`) : il est exposé par l'API et pilotable depuis le
dashboard (page « Classification d'intention »), en réutilisant le même
pattern de jobs que l'entraînement sentiment.

### Routes (`api/routes/intent_train.py`)

| Méthode | Route | Clé API | Description |
|---|---|---|---|
| `POST` | `/train/intent` | oui | Lance l'entraînement (dataset JSONL `{"text","label"}`), réponse 202 + `TrainJob` (`kind="intent"`). |
| `GET`  | `/train/intent/status/{job_id}` | oui | Statut / étape / progression du job. |
| `GET`  | `/train/intent/jobs` | oui | Historique paginé, filtré sur `kind="intent"` (sans mélange avec sentiment/pipeline). |
| `POST` | `/train/intent/cancel/{job_id}` | oui | Annulation coopérative (Event vérifié batch par batch). |
| `GET`  | `/train/intent/versions` | oui | Versions valides de `experiments/intent_models/` + pointeur actif. |
| `POST` | `/train/intent/activate` | oui | Pointe `active.json` sur une version (l'IHM chaîne ensuite `/classifiers/intent/reload`). |

Validations précoces en 422 : dataset introuvable, `base_model_version`
invalide (continual training), hyper-paramètres hors bornes (pydantic).

### Runner (`core/intent_trainer.py`)

Refactor de `scripts/train_intent.py` en module importable, exécuté dans un
thread daemon (`run_intent_training(job_id, req)`) avec le contrat de job du
sentiment (`core/trainer_runner.py`) : étapes canoniques
`INTENT_TRAIN_JOB_STEPS` (`core/models.py`, alignées sur
`dashboard/src/api/jobSteps.ts`), `job.progress` (pourcentage global),
métriques par epoch dans la table `train_metrics` existante (diffusées par le
WebSocket `/train/stream/{job_id}` sans changement), logs capturés par
`core/job_logs.py`, annulation via `IntentTrainingCancelled` — l'exception est
attrapée AVANT `Exception` afin de conserver le statut `cancelled`.

Étapes : `queued → loading_dataset → splitting_dataset → loading_model →
training → saving_model → done`. Les imports lourds (torch / transformers /
datasets) sont faits dans le thread du job, à l'étape `loading_model` :
importer le module reste léger.

Requête (`IntentTrainRequest`, `core/models.py`) : `dataset_path`,
`base_model`, `base_model_version` (continual training depuis une version
d'intention existante), `epochs`, `batch_size`, `learning_rate`,
`max_length`, `test_size`, `quantize_int8`, `activate`.

### Dashboard

`IntentPage` (`#/intention`) expose : formulaire d'entraînement + tracker
(`IntentTrainJobTracker`, étapes `INTENT_TRAIN_STEPS` de
`dashboard/src/api/jobSteps.ts`), historique des jobs d'intention, tableau des
versions avec activation (activation → rechargement du classifieur, chaînage
côté client). La façade `dashboard/src/api/intentTrainApi.ts` regroupe les
appels et les types.

### Tests

```bash
python -m pytest tests/test_intent_train_api.py -q
cd dashboard && npx vitest run src/api/intentTrainApi.test.ts src/components/IntentTrainJobTracker.test.tsx
```