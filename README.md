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
