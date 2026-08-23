"""
Tests d'intégration end-to-end (E2E) de l'API FastAPI — totalement offline.

Valide le cycle complet :
    POST /train  ->  GET /train/status/{job_id} (polling)  ->  POST /predict

Avec un VRAI petit modèle torch (couches Linear + embedding) entraîné en
mémoire, et un mini-tokenizer local. Aucun appel réseau HuggingFace :
dataset, augmentation, tokenizer, modèle et entraînement sont tous remplacés
par des doubles locaux (pipeline mocké), sauf le véritable entraînement
forward/backward du TinyModel.

Cas d'erreur couverts :
  - modèle absent  -> 503
  - job inconnu    -> 404
"""
import json
import os
import threading
import time
from types import SimpleNamespace

import torch
import torch.nn as nn
import torch.nn.functional as F
import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("API_KEY", "test-key")

import api
from api import app

from core import trainer_runner as _trainer_runner
from core import predictor_cache as _predictor_cache
from core import model_versioning as _model_versioning

HEADERS = {"X-API-Key": "test-key"}

# Défense en profondeur : coupe tout passage réseau HuggingFace.
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
os.environ.setdefault("HF_HUB_OFFLINE", "1")

SENTIMENTS = ["negative", "neutral", "positive"]

# ---------------------------------------------------------------------------#
# Mini-vocabulaire local : inutile de charger un tokenizer HuggingFace.
# ---------------------------------------------------------------------------#
VOCAB = {
    "[PAD]": 0,
    "[UNK]": 1,
    "ce": 2,
    "produit": 3,
    "est": 4,
    "bon": 5,
    "je": 6,
    "suis": 7,
    "decu": 8,
    "tres": 9,
    "bien": 10,
    "mauvais": 11,
    "super": 12,
    ".": 13,
    "!": 14,
    "le": 15,
    "service": 16,
    "fantastique": 17,
    "a": 18,
    "la": 19,
    "qualite": 20,
}
VOCAB_SIZE = len(VOCAB)


def _normalized_tokens(text: str):
    """Tokenisation très simple, sans dépendance : lowercase + split espaces."""
    return [tok for tok in text.lower().split() if tok.strip()]

class TinyTokenizer:
    """Mini-tokenizer local compatible avec l'interface attendue par api.py
    (méthode save_pretrained) sans rien charger depuis HuggingFace."""

    def __init__(self, vocab: dict = None):
        self.vocab = vocab if vocab is not None else dict(VOCAB)
        self.unk_id = self.vocab.get("[UNK]", 1)
        self.pad_id = self.vocab.get("[PAD]", 0)

    def tokenize(self, text: str):
        return _normalized_tokens(text)


    # --- Compatibilité HuggingFace minimale ---
    @property
    def pad_token_id(self):
        return self.pad_id

    @property
    def pad_token(self):
        return "[PAD]"

    @property
    def model_max_length(self):
        return 16

    @property
    def padding_side(self):
        return "right"

    def pad(
    self,
    encoded_inputs,
    padding=True,
    max_length=None,
    pad_to_multiple_of=None,
    return_tensors=None,
    **kwargs
    ):
        """
        Version compatible HuggingFace : accepte tous les arguments possibles
        et gère input_ids sous forme de list OU de Tensor.
        """
        if isinstance(encoded_inputs, dict):
            encoded_inputs = [encoded_inputs]

        # Déterminer la longueur max
        if max_length is None:
            max_length = max(
                len(item["input_ids"]) if isinstance(item["input_ids"], list)
                else item["input_ids"].shape[0]
                for item in encoded_inputs
            )

        # Alignement optionnel
        if pad_to_multiple_of is not None:
            if max_length % pad_to_multiple_of != 0:
                max_length = ((max_length // pad_to_multiple_of) + 1) * pad_to_multiple_of

        padded_ids = []
        padded_masks = []

        for item in encoded_inputs:
            ids = item["input_ids"]
            mask = item["attention_mask"]

            # Convertir Tensor → list
            if not isinstance(ids, list):
                ids = ids.tolist()
            if not isinstance(mask, list):
                mask = mask.tolist()

            pad_len = max_length - len(ids)

            padded_ids.append(ids + [self.pad_id] * pad_len)
            padded_masks.append(mask + [0] * pad_len)

        if return_tensors == "pt":
            return {
                "input_ids": torch.tensor(padded_ids, dtype=torch.long),
                "attention_mask": torch.tensor(padded_masks, dtype=torch.long),
            }

        return {
            "input_ids": padded_ids,
            "attention_mask": padded_masks,
        }



    def encode(self, texts, max_length: int = 16):
        """Retourne (input_ids, attention_mask) tensors torch."""
        ids, masks = [], []
        for text in texts:
            tokens = self.tokenize(text)[:max_length]
            row = [self.vocab.get(tok, self.unk_id) for tok in tokens]
            row = row[:max_length]
            mask = [1] * len(row)
            row += [self.pad_id] * (max_length - len(row))
            mask += [0] * (max_length - len(mask))
            ids.append(row)
            masks.append(mask)
        return torch.tensor(ids, dtype=torch.long), torch.tensor(masks, dtype=torch.long)

    def __call__(self, texts, padding=True, truncation=True, max_length=16,
             return_tensors=None, return_token_type_ids=None, **kwargs):
        # On ignore padding/truncation/return_token_type_ids comme HF
        input_ids, attention_mask = self.encode(texts, max_length=max_length)

        if return_tensors == "pt":
            return {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
            }

        # Compatibilité minimale si return_tensors=None
        return {
            "input_ids": input_ids.tolist(),
            "attention_mask": attention_mask.tolist(),
        }


    def save_pretrained(self, path):
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "vocab.json"), "w", encoding="utf-8") as fh:
            json.dump(self.vocab, fh)

class TinyOutput:
    """Mini wrapper imitant outputs.logits des modèles HF."""

    def __init__(self, logits):
        self.logits = logits


class TinyModel(nn.Module):
    """Mini-modèle torch avec une vraie couche Linear de tête (3 classes)."""

    def __init__(self, embedding_dim: int = 32, num_labels: int = 3):
        super().__init__()
        self.embedding = nn.Embedding(VOCAB_SIZE, embedding_dim)
        self.dropout = nn.Dropout(0.1)
        self.head = nn.Linear(embedding_dim, num_labels)

    def forward(self, input_ids, attention_mask=None):
        x = self.embedding(input_ids)
        if attention_mask is not None:
            mask = attention_mask.unsqueeze(-1).float()
            x = (x * mask).sum(dim=1) / mask.sum(dim=1).clamp(min=1.0)
        else:
            x = x.mean(dim=1)
        x = self.dropout(x)
        logits = self.head(x)
        return TinyOutput(logits)
class FakeTrainer:
    """Doublon du Trainer HF : entraîne réellement le TinyModel en < 0,5 s."""

    def __init__(self, model, cfg, class_weights=None):
        self.model = model
        self.cfg = cfg
        self.epoch_metrics = []
        self.final_metrics = {}
        self.training_duration_seconds = 0.05
        self.train_examples = 12
        self.val_examples = 4

    def train(self, train_loader, val_loader, cancel_event=None):
        # Mini-entraînement réel : une boucle forward/backward en mémoire
        # pour prouver qu'un vrai gradient fonctionne de bout en bout.
        optimizer = torch.optim.SGD(self.model.parameters(), lr=0.2)
        for _ in range(10):
            ids = torch.randint(0, VOCAB_SIZE, (8, 16))
            labels = torch.randint(0, 3, (8,))
            output = self.model(input_ids=ids)
            loss = F.cross_entropy(output.logits, labels)
            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        self.epoch_metrics = [{"epoch": 1, "accuracy": 0.99, "f1_macro": 0.98}]
        self.final_metrics = {"accuracy": 0.99, "f1_macro": 0.98}

    def save(self, path):
        os.makedirs(path, exist_ok=True)
        torch.save(self.model.state_dict(), os.path.join(path, "model.pt"))


class TinyDataset:
    """Petit dataset local (pas de HuggingFace datasets)."""

    def __init__(self, texts=None, labels=None, lang_codes=None):
        self.text = texts or []
        self.labels = labels or []
        self.lang_code = lang_codes or ["fr"] * len(self.text)

    @property
    def label(self):
        """Alias to match the column name used by the training pipeline."""
        return self.labels

    def __len__(self):
        return len(self.text)

    def __getitem__(self, key):
        if isinstance(key, str):
            # Return the entire attribute (e.g., for iteration)
            return getattr(self, key)
        elif isinstance(key, slice):
            return {
                "text": self.text[key],
                "labels": self.labels[key],
                "lang_code": self.lang_code[key],
            }
        else:
            # Integer index
            return {
                "text": self.text[key],
                "labels": self.labels[key],
                "lang_code": self.lang_code[key],
            }

    def train_test_split(self, test_size=0.1, seed=42):
        n = len(self)
        n_test = max(1, int(n * test_size))
        n_train = n - n_test
        return {
            "train": TinyDataset(self.text[:n_train], self.labels[:n_train], self.lang_code[:n_train]),
            "test": TinyDataset(self.text[n_train:], self.labels[n_train:], self.lang_code[n_train:]),
        }


class TinyPredictor:
    """Chargement du modèle entraîné et prédiction — aucun appel réseau."""

    def __init__(self, model_path):
        if not os.path.isdir(model_path):
            raise FileNotFoundError(f"Model path not found: {model_path}")
        self.model_path = model_path
        vocab_path = os.path.join(model_path, "vocab.json")
        if not os.path.isfile(vocab_path):
            raise FileNotFoundError("vocab.json")
        with open(vocab_path, "r", encoding="utf-8") as fh:
            self.vocab = json.load(fh)
        self.tokenizer = TinyTokenizer(self.vocab)
        state = torch.load(os.path.join(model_path, "model.pt"), map_location="cpu", weights_only=True)
        self.model = TinyModel()
        self.model.load_state_dict(state)
        self.model.eval()

    def predict(self, texts):
        input_ids, attention_mask = self.tokenizer.encode(texts, max_length=16)
        with torch.no_grad():
            logits = self.model(input_ids, attention_mask).logits
        probs = torch.softmax(logits, dim=-1)
        preds = torch.argmax(probs, dim=-1)
        results = []
        for text, pred, prob in zip(texts, preds, probs):
            idx = int(pred.item())
            results.append({
                "text": text,
                "sentiment": SENTIMENTS[idx],
                "confidence": round(float(prob[idx].item()), 3),
            })
        return results
# ---------------------------------------------------------------------------#
# Fixtures pytest
# ---------------------------------------------------------------------------#
@pytest.fixture(autouse=True)
def isolated_api(monkeypatch, tmp_path):
    """Isole l'API : job store en mémoire, chemins de modèles dans un tmp,
    modèle/predictor réinitialisés, maintenance et rate limit neutres."""
    monkeypatch.setattr(api, "TEST_MODE", True)
    monkeypatch.setattr(api, "_MAINTENANCE_MODE", False)
    monkeypatch.setattr(api, "RATE_LIMIT_PER_MINUTE", 100000)
    monkeypatch.setattr(api, "RATE_LIMIT_ENABLED", True)
    monkeypatch.setattr(api, "_get_api_key", lambda: "test-key")

    root = str(tmp_path / "experiments" / "models")
    legacy = str(tmp_path / "sentiment_model_final")
    os.makedirs(root, exist_ok=True)
    monkeypatch.setattr(api, "MODEL_ROOT", root)
    monkeypatch.setattr(api, "MODELS_ROOT", root)
    monkeypatch.setattr(api, "LEGACY_MODEL_DIR", legacy, raising=False)
    monkeypatch.setattr(api, "JOB_STORE_PATH", str(tmp_path / "jobs.db"), raising=False)

    # Isole aussi les modules refactorisés (core.*), qui sont utilisés par le
    # trainer runner et la résolution de modèles réellement appelés.
    monkeypatch.setattr(_model_versioning, "MODEL_ROOT", root)
    monkeypatch.setattr(_model_versioning, "MODELS_ROOT", root)

    # Réinitialise le store de jobs et le cache predictor entre chaque test.
    previous_jobs = api._jobs
    previous_events = api._job_cancel_events
    previous_predictor = api._predictor
    previous_core_predictor = _predictor_cache._predictor
    api._jobs = {}
    api._job_cancel_events = {}
    api._predictor = None
    _predictor_cache._predictor = None

    yield

    api._jobs = previous_jobs
    api._job_cancel_events = previous_events
    api._predictor = previous_predictor
    _predictor_cache._predictor = previous_core_predictor


@pytest.fixture()
def pipeline_mock(monkeypatch):
    """Mocke le pipeline d'entraînement HuggingFace et le remplace par des
    doubles locaux (dataset, augmentation, tokenizer, modèle, Trainer).

    IMPORTANT : POST /train exécute core.trainer_runner.run_training, qui
    référence ses dépendances par imports directs dans core.trainer_runner
    (et non via le module api). Les patches doivent donc viser les modules
    core.* utilisés au runtime pour rester réellement offline.
    """
    monkeypatch.setattr(_trainer_runner, "load_config", lambda path=None: {
        "model_name": "tiny-local-model",
        "max_length": 16,
        "learning_rate": 0.2,
        "weight_decay": 0.0,
        "warmup_ratio": 0.0,
        "epochs": 1,
        "batch_size": 4,
        "num_workers": 0,
        "device": "cpu",
        "class_augment_weights": {},
        "gradient_accumulation_steps": 1,
        "bf16": False,
        "torch_threads": 1,
        "gradient_clip": 1.0,
        "early_stopping_patience": 0,
        "early_stopping_min_delta": 0.0,
    })
    monkeypatch.setattr(_trainer_runner, "load_raw_dataset", _fake_raw_dataset)
    monkeypatch.setattr(_trainer_runner, "augment_dataset", lambda ds, **kwargs: ds)
    monkeypatch.setattr(_trainer_runner, "create_dataloaders", lambda train, val, cfg: (None, None))
    monkeypatch.setattr(_trainer_runner, "compute_class_weights", lambda labels, **kwargs: None)
    monkeypatch.setattr(_trainer_runner, "build_model", lambda cfg: TinyModel())
    monkeypatch.setattr(_trainer_runner, "Trainer", FakeTrainer)
    # On garde TEST_MODE à False ici : dans ce mode le runner importe des
    # modules "tiny" (src.inference.tiny_tokenizer / src.model.tiny_model) qui
    # n'existent pas hors des tests. Avec False, il passe par AutoTokenizer
    # (patché) et build_model (patché) pour obtenir les doublons Tiny.
    monkeypatch.setattr(_trainer_runner, "TEST_MODE", False)
    monkeypatch.setattr(
        _trainer_runner.AutoTokenizer,
        "from_pretrained",
        staticmethod(lambda *a, **k: TinyTokenizer()),
    )
    # Prédiction offline : remplace la classe Predictor (chargement HF).
    monkeypatch.setattr(_predictor_cache, "Predictor", TinyPredictor)
    monkeypatch.setattr(_predictor_cache, "_predictor", None)

    # ------------------------------------------------------------------ #
    # SHIM DE SAUVEGARDE DANS LE DOSSIER VERSIONNÉ.
    # save_model_version() persiste le tokenizer + le rapport dans
    # experiments/models/<timestamp>/ mais n'écrit PAS les poids du modèle
    # (ils partent dans sentiment_model_final via trainer.save()). Or /predict
    # résout le modèle via resolve_model_dir() qui exige un fichier de poids
    # (model.pt) dans ce dossier. Le shim ajoute donc la persistance des
    # poids dans le dossier versionné, en conservant le comportement d'origine.
    # ------------------------------------------------------------------ #
    _real_save_model_version = _model_versioning.save_model_version

    def _save_model_version_with_weights(
        tokenizer, trainer, job_id, train_examples, val_examples, started_at, finished_at
    ):
        model_dir = _real_save_model_version(
            tokenizer, trainer, job_id, train_examples, val_examples, started_at, finished_at
        )
        # Persist le TinyModel réel dans experiments/models/<timestamp>/.
        trainer.save(model_dir)
        return model_dir

    monkeypatch.setattr(_trainer_runner, "save_model_version", _save_model_version_with_weights)


def _fake_raw_dataset(max_per_lang=None, languages=None, local_corrections_path=None):
    # local_corrections_path : propage depuis SCRUM-57 (corrections locales
    # concaténées dans load_raw_dataset). Le double reste offline et ignore
    # simplement le fichier.
    texts = [
        "ce produit est bon",
        "je suis tres decu par ce produit",
        "le service est fantastique",
        "mauvais produit tres mauvaise qualite",
        "ce service est super",
        "bien bon produit",
    ]
    labels = [2, 0, 2, 0, 2, 1]
    if max_per_lang:
        texts = texts[:max_per_lang]
        labels = labels[:max_per_lang]
    return TinyDataset(texts, labels)


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client
# ---------------------------------------------------------------------------#
# Tests
# ---------------------------------------------------------------------------#
def test_train_status_predict_end_to_end(client, pipeline_mock):
    """Cycle complet : POST /train -> polling /train/status -> POST /predict."""
    # 1) Démarre un entraînement (dans un thread dédié).
    resp = client.post(
        "/train",
        json={"max_per_lang": 6, "epochs": 1, "batch_size": 2},
        headers=HEADERS,
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    assert job_id

    # 2) L'entraînement part réellement dans un thread (comme en prod).
    req = api.TrainRequest(max_per_lang=6, epochs=1, batch_size=2)
    api_thread = threading.Thread(target=api._run_training, args=(job_id, req), daemon=True)
    api_thread.start()

    # 3) Polling du statut jusqu'à un état terminal.
    terminal = {"completed", "failed", "cancelled"}
    final_status = None
    for _ in range(200):
        status_resp = client.get(f"/train/status/{job_id}", headers=HEADERS)
        assert status_resp.status_code == 200, status_resp.text
        body = status_resp.json()
        if body["status"] in terminal:
            final_status = body
            break
        time.sleep(0.05)
    api_thread.join(timeout=5)

    assert final_status is not None, "L'entraînement n'a pas abouti à un état terminal"
    assert final_status["status"] == "completed", f"Échec entraînement: {final_status.get('error')}"

    # 4) Prédiction avec le modèle entraîné de bout en bout.
    predict_resp = client.post(
        "/predict",
        json={"texts": ["ce produit est super", "produit tres mauvaise qualite"]},
        headers=HEADERS,
    )
    assert predict_resp.status_code == 200, predict_resp.text
    payload = predict_resp.json()
    assert "results" in payload
    assert len(payload["results"]) == 2
    for item in payload["results"]:
        assert item["sentiment"] in SENTIMENTS, item
        assert 0.0 <= item["confidence"] <= 1.0, item


def test_predict_returns_503_when_no_model(client):
    """Aucun modèle entraîné disponible -> /predict doit répondre 503."""
    resp = client.post(
        "/predict",
        json={"texts": ["bonjour"]},
        headers=HEADERS,
    )
    assert resp.status_code == 503, resp.text
    detail = resp.json().get("detail", "")
    assert "aucun" in detail.lower() or "model" in detail.lower(), detail


def test_train_status_404_for_unknown_job(client):
    """GET /train/status/{id} avec un id inconnu -> 404."""
    resp = client.get("/train/status/does-not-exist", headers=HEADERS)
    assert resp.status_code == 404, resp.text
    assert "introuvable" in resp.json().get("detail", "")