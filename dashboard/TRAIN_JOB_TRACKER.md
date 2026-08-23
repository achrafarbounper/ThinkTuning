# Panneau de suivi des jobs d'entraînement

## Vue d'ensemble

Le **TrainJobTracker** est un composant React qui affiche le suivi en temps réel d'un job d'entraînement avec :

- **Affichage du step courant** — l'étape précise où se trouve l'entraînement
- **Visualisation de la progression** — barre de progression et timeline des étapes
- **Statut du job** — pending, running, completed, failed, ou cancelled
- **Timing** — durée d'exécution du job
- **Bouton d'annulation** — permet d'interrompre un entraînement en cours
- **Messages d'erreur et succès** — affichage détaillé des résultats

## Intégration

Le composant est importé dans [dashboard-demo.jsx](src/dashboard-demo.jsx) :

```jsx
import TrainJobTracker from "./components/TrainJobTracker";
```

Et utilisé dans la section "Entraînement" :

```jsx
<TrainJobTracker
  job={currentJob}
  onCancel={handleCancelTraining}
  cancelLoading={false}
/>
```

## Fichiers modifiés

### 1. [components/TrainJobTracker.jsx](src/components/TrainJobTracker.jsx) (nouveau)

Composant React affichant le suivi d'un job d'entraînement avec :
- **Affichage du statut** avec couleur codée (rouge=erreur, vert=succès, orange=annulé, bleu=en cours)
- **Barre de progression** avec nombre d'étapes complétées
- **Étape courante** avec spinner animé si en cours
- **Timeline compacte** — points de couleur pour chaque étape (✓ = complétée, • = active, ... = en attente)
- **Bouton d'annulation** (visible seulement quand le job est en cours)
- **Messages d'erreur/succès** affichables/repliables

### 2. [dashboard-demo.jsx](src/dashboard-demo.jsx) (modifié)

Modifications :
- **Ligne 3** : Import du composant `TrainJobTracker`
- **Lignes 652-654** : Remplacement de la visualisation manuelle du job par le composant
- **Lignes 857-887** : Ajout des styles CSS pour tous les éléments du tracker

## Étapes d'entraînement suivies

Le tracker affiche les 10 étapes définies dans le pipeline d'entraînement :

| Étape | Libellé | Description |
|---|---|---|
| `queued` | En file d'attente | Job en attente de démarrage |
| `loading_dataset` | Chargement du dataset | Téléchargement/chargement depuis HuggingFace |
| `splitting_dataset` | Split train/val | Séparation train/validation 90/10 |
| `augmenting_dataset` | Recomposition (EDA) | Application de l'augmentation de données |
| `building_dataloaders` | Construction DataLoaders | Préparation des loaders PyTorch |
| `computing_class_weights` | Poids des classes | Calcul des poids pour l'équilibrage |
| `loading_model` | Chargement du modèle | Chargement de DistilBERT depuis HuggingFace |
| `training` | Entraînement | Boucle d'entraînement (epochs) |
| `saving_model` | Sauvegarde du modèle | Sauvegarde sur disque en `/experiments/models/{timestamp}` |
| `done` | Terminé | Succès complet |

## Styles CSS

Les styles sont intégrés directement dans le fichier CSS global de `dashboard-demo.jsx`.

Classes principales :
- `.tt-tracker` — conteneur principal
- `.tt-tracker-header` — en-tête avec ID du job et boutons
- `.tt-progress-bar` — barre de progression
- `.tt-tracker-step` — affichage de l'étape courante
- `.tt-steps-timeline` — timeline compacte des étapes
- `.tt-step-dot` — point individuel dans la timeline
- `.tt-tracker-error`, `.tt-tracker-success`, `.tt-tracker-cancelled` — messages de résultat

### Animations

- **tt-spin** — rotation continue du spinner pendant le job
- **tt-pulse** — pulsation du point actif dans la timeline

## Utilisation

### Affichage simple

```jsx
<TrainJobTracker
  job={currentJob}
  onCancel={handleCancelTraining}
  cancelLoading={false}
/>
```

### Props

| Prop | Type | Description |
|---|---|---|
| `job` | `TrainJob \| null` | L'objet job actuel (ou `null` pour masquer le composant) |
| `onCancel` | `() => void` | Callback appelé au clic sur le bouton "Annuler" |
| `cancelLoading` | `boolean` | Si `true`, le bouton affiche "Annulation…" et est désactivé |

### Objets modèle

La structure d'un `TrainJob` vient de [api.py](../../api.py#L208) :

```python
class TrainJob(BaseModel):
    job_id: str
    status: JobStatus  # pending, running, completed, failed, cancelled
    step: str  # une des étapes de TRAIN_STEPS
    started_at: Optional[float]  # timestamp Unix
    finished_at: Optional[float]  # timestamp Unix
    error: Optional[str]  # message d'erreur complet (s'il y a)
    model_path: Optional[str]  # chemin du modèle sauvegardé
```

## API backend

Le tracker utilise ces endpoints de [api.py](../../api.py) :

```bash
# Démarrer un entraînement (retourne un job_id)
POST /train
Body: { max_per_lang: 500, epochs: 2, ... }
→ 202 { job_id, status: "pending", ... }

# Suivi en polling (4s par défaut)
GET /train/status/{job_id}
→ 200 { job_id, status: "running", step: "training", ... }

# Annuler un job
POST /train/cancel/{job_id}
→ 200 { job_id, status: "cancelled", ... }

# Lister tous les jobs (paginé, filtrable)
GET /train/jobs?status=completed&limit=20&offset=0
→ 200 { total, items: [{ job_id, status, ... }, ...], limit, offset }
```

## Fonctionnement du polling

Le dashboard implémente un polling avec `setInterval` toutes les 4 secondes (paramètre `TRAIN_POLL_MS`) :

```javascript
trainPollRef.current = setInterval(async () => {
  const job = await clientRef.current.getTrainingStatus(jobId);
  setCurrentJob(job);
  // Stop le polling si terminal
  if (["completed", "failed", "cancelled"].includes(job.status)) {
    stopTrainPolling();
  }
}, TRAIN_POLL_MS);
```

Le polling s'arrête automatiquement quand le job atteint un état terminal.

## Exemple d'affichage

```
┌─────────────────────────────────────────────────────────────┐
│  Suivi du job [a1b2c3d4]                         [Annuler]  │
│  ✓ RUNNING · ⏱ 3m                                           │
│                                                             │
│  ████████████████████░░░░░░░░░░░░░░░░░  (6 / 10)         │
│                                                             │
│  Étape courante: Entraînement ⟳                           │
│                                                             │
│  En file • Dataset • Split • EDA • Loaders • Poids        │
│  ✓ Modèle • [Train actif] • Sauvegarde • ✓               │
│                                                             │
│  Entraînement annulé par l'utilisateur                    │
└─────────────────────────────────────────────────────────────┘
```

## Historique des jobs

Un tableau récapitulatif en bas affiche tous les jobs, du plus récent au plus ancien :

```
| Job        | Statut      | Étape            | Démarré                | Terminé                |
|------------|-------------|------------------|------------------------|------------------------|
| a1b2c3d4   | ✓ completed | Terminé          | 2026-08-16 14:32:10   | 2026-08-16 14:48:23   |
| ...        | ...         | ...              | ...                    | ...                    |
```

## Configuration de l'API

Voir [api.py](../../api.py) pour les détails sur :
- `JobStatus` enum
- `TrainJob` modèle Pydantic
- `PersistentJobStore` (stockage SQLite)
- Endpoints `/train/*`
