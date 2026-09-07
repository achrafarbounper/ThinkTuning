"""Validation de class_augment_weights (fix ValueError 'additionalProp1').

Les placeholders 'additionalProp1', 'additionalProp2'... sont pré-remplis par
Swagger UI pour les dicts libres (Dict[str, float]) : envoyés tels quels ils
provoquaient un ValueError obscur en plein job d'entraînement. On garantit ici
que :
- TrainRequest accepte les clés str numériques et renvoie une 422 explicite
  sur les placeholders ou poids négatifs ;
- augment_dataset lève aussi une erreur claire en appel direct (train.py).
"""

import pandas as pd
import pytest
from datasets import Dataset
from pydantic import ValidationError

from core.models import TrainRequest
from src.dataset.loader import augment_dataset


def test_train_request_accepts_string_label_keys():
    """Les clés JSON sont des strings : {'1': 3.0} doit être accepté tel quel."""
    req = TrainRequest(class_augment_weights={"0": 1.0, "1": 3.0})
    assert req.class_augment_weights == {"0": 1.0, "1": 3.0}


def test_train_request_default_is_none():
    assert TrainRequest().class_augment_weights is None


def test_train_request_rejects_swagger_placeholders():
    """'additionalProp1' (placeholder Swagger UI) => ValidationError explicite."""
    with pytest.raises(ValidationError) as err:
        TrainRequest(class_augment_weights={"additionalProp1": 0})
    message = str(err.value)
    assert "additionalProp1" in message
    assert "Swagger" in message


def test_train_request_rejects_negative_weights():
    with pytest.raises(ValidationError) as err:
        TrainRequest(class_augment_weights={"1": -0.5})
    assert "négatif" in str(err.value)


def test_train_request_normalizes_key_spacing():
    """' 1 ' doit être normalisé en '1' (les int restent acceptés côté loader)."""
    req = TrainRequest(class_augment_weights={" 1 ": 2.0})
    assert req.class_augment_weights == {"1": 2.0}


def test_augment_dataset_rejects_non_integer_keys_with_clear_error():
    df = pd.DataFrame(
        {
            "text": ["j'adore ce film", "ça va", "super produit"],
            "label": [2, 1, 2],
            "lang_code": ["fr", "fr", "fr"],
        }
    )
    ds = Dataset.from_pandas(df)
    with pytest.raises(ValueError, match="clé invalide.*additionalProp1"):
        augment_dataset(
            ds,
            augment_fraction=0.5,
            class_augment_weights={"additionalProp1": 1.0},
        )


def test_augment_dataset_rejects_negative_weights():
    df = pd.DataFrame(
        {
            "text": ["j'adore ce film", "ça va", "super produit"],
            "label": [2, 1, 2],
            "lang_code": ["fr", "fr", "fr"],
        }
    )
    ds = Dataset.from_pandas(df)
    with pytest.raises(ValueError, match="poids négatif"):
        augment_dataset(
            ds,
            augment_fraction=0.5,
            class_augment_weights={0: -1.0},
        )
