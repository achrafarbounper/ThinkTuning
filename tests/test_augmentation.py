import random

import pandas as pd
import pytest
from datasets import Dataset

import src.augmentation.eda as eda
from src.augmentation.eda import (
    back_translation,
    random_deletion,
    random_swap,
    recompose,
)
from src.dataset.loader import augment_dataset

# Synonymes déterministes injectés par la fixture `fake_synonyms` : rend les
# tests de recompose indépendants des corpus NLTK (WordNet/OMW), qui sont
# téléchargés à la demande (réseau) et peuvent être absents en CI / Docker.
_FAKE_SYNONYMS = {
    "bonjour": ["salut"],
    "monde": ["planète", "globe", "univers"],
    "film": ["movie", "long-métrage"],
    "vraiment": ["réellement"],
    "génial": ["super", "excellent"],
    "excellent": ["génial"],
}


@pytest.fixture()
def fake_synonyms(monkeypatch):
    """Remplace _get_synonyms par un dictionnaire déterministe.

    Court-circuite entièrement _ensure_nltk_data (aucun accès réseau) : le
    comportement de recompose est identique avec ou sans corpus installés.
    """
    monkeypatch.setattr(
        eda,
        "_get_synonyms",
        lambda word, lang: _FAKE_SYNONYMS.get(word.lower(), []),
    )


def test_random_swap_preserves_length():
    words = ["un", "deux", "trois"]
    swapped = random_swap(words, n=1)
    assert len(swapped) == len(words)
    assert set(swapped) == set(words)


def test_random_deletion_never_returns_empty():
    words = ["un", "deux"]
    deleted = random_deletion(words, p=1.0)
    assert len(deleted) >= 1
    assert all(w in words for w in deleted)


def test_recompose_returns_expected_variants(fake_synonyms):
    """Hermétique : le test de la LOGIQUE de recompose ne doit pas dépendre
    des corpus WordNet (téléchargement réseau via _ensure_nltk_data). En CI,
    si les corpus sont absents, _get_synonyms retourne [] et les opérations
    synonym_replacement / random_insertion retombent sur le texte original :
    recompose ne produisait qu'une seule variante (le random swap) et le test
    échouait (« assert 1 == 2 »). La fixture `fake_synonyms` injecte des
    synonymes déterministes -> comportement identique en CI et en local."""
    random.seed(42)
    variants = recompose("Bonjour le monde", lang="fr", num_variants=2, alpha=0.5)
    assert len(variants) == 2
    assert variants[0] != "Bonjour le monde"
    assert variants[1] != "Bonjour le monde"


def test_augment_dataset_deduplicates_before_eda():
    data = pd.DataFrame(
        {
            "text": [
                "J'aime vraiment ce film",
                "  J'AIME VRAIMENT CE FILM  ",
                "C'est un film très mauvais",
                "C'est un film très mauvais",
                "Autre phrase neutre pour tester",
            ],
            "label": [1, 1, 0, 0, 2],
            "lang_code": ["fr", "fr", "fr", "fr", "fr"],
        }
    )
    dataset = Dataset.from_pandas(data)

    augmented = augment_dataset(
        dataset,
        variants_per_example=3,
        augment_fraction=1.0,
        seed=42,
        deduplicate=True,
    )

    original_texts = [row["text"] for row in dataset]
    unique_normalized = {
        text.strip().lower() for text in original_texts
    }
    assert len(unique_normalized) == 3
    assert len(augmented) == 3 + (3 * 3)

    generated_variants = {
        row["text"]
        for row in augmented
        if row["text"] not in {"J'aime vraiment ce film", "C'est un film très mauvais", "Autre phrase neutre pour tester"}
    }
    assert len(generated_variants) > 1
    assert any(row["text"] != "J'aime vraiment ce film" for row in augmented)
    assert any(row["text"] != "C'est un film très mauvais" for row in augmented)


def test_augment_dataset_targets_neutral_class_by_default():
    """Sans class_augment_weights, la classe neutral (label 1) est sur-représentée."""
    n_neutral, n_negative = 8, 40
    data = pd.DataFrame(
        {
            "text": [f"phrase neutre numéro {i} sans émotion forte" for i in range(n_neutral)]
            + [f"phrase négative numéro {i} film vraiment mauvais" for i in range(n_negative)],
            "label": [1] * n_neutral + [0] * n_negative,
            "lang_code": ["fr"] * (n_neutral + n_negative),
        }
    )
    dataset = Dataset.from_pandas(data)
    original_neutral_frac = sum(l == 1 for l in dataset["label"]) / len(dataset)

    augmented = augment_dataset(
        dataset,
        variants_per_example=3,
        augment_fraction=0.5,
        seed=42,
    )

    neutral_frac = sum(1 for row in augmented if row["label"] == 1) / len(augmented)
    assert neutral_frac > original_neutral_frac


def test_augment_dataset_class_augment_weights_only_selects_neutral():
    """Un poids nul sur les autres classes force la sélection d'exemples neutral."""
    n_neutral, n_negative = 40, 40
    data = pd.DataFrame(
        {
            "text": [f"texte neutre numéro {i} sans jugement" for i in range(n_neutral)]
            + [f"texte négatif numéro {i} très décevant" for i in range(n_negative)],
            "label": [1] * n_neutral + [0] * n_negative,
            "lang_code": ["fr"] * (n_neutral + n_negative),
        }
    )
    dataset = Dataset.from_pandas(data)

    # Rows sélectionnées pour l'augmentation = uniquement des neutral (label 1).
    augmented = augment_dataset(
        dataset,
        variants_per_example=1,
        augment_fraction=0.5,  # 40 samples parmi 80 -> tous parmi les 40 neutral
        seed=42,
        class_augment_weights={0: 0.0, 1: 1.0, 2: 0.0},
    )

    # Chaque variante générée (nouveau texte) doit porter le label neutral.
    original_texts = set(row["text"] for row in dataset)
    for row in augmented:
        if row["text"] not in original_texts:
            assert row["label"] == 1


# ---------------------------------------------------------------------------
# Back-translation (SCRUM-44)
# ---------------------------------------------------------------------------


class _FakePipeline:
    """Pipeline de traduction factice : renvoie une sortie fixe."""

    def __init__(self, output: str):
        self.output = output
        self.calls = []

    def __call__(self, text, **kwargs):
        self.calls.append(text)
        return [{"translation_text": self.output}]


def test_back_translation_disabled_by_default(monkeypatch, fake_synonyms):
    """Sans use_back_translation, aucun modèle de traduction n'est chargé.

    `fake_synonyms` garantit que recompose produit bien num_variants variantes
    même sans corpus WordNet installés (sinon les opérations SR/RI sont
    inopérantes en CI et la boucle ne génère qu'une variante)."""
    def _boom(*args, **kwargs):
        raise AssertionError("Le modèle de traduction ne doit pas être chargé")

    monkeypatch.setattr(eda, "_get_translation_pipeline", _boom)

    random.seed(42)
    variants = recompose("Bonjour le monde", lang="fr", num_variants=2, alpha=0.5)
    assert len(variants) == 2


def test_back_translation_fr_to_en_to_fr(monkeypatch):
    """Le flux FR→EN puis EN→FR est appliqué dans le bon ordre."""
    fwd = _FakePipeline("This movie was really great")
    bwd = _FakePipeline("Ce film était vraiment génial")
    monkeypatch.setattr(
        eda, "_get_translation_pipeline",
        lambda src, tgt: fwd if (src, tgt) == ("fr", "en") else bwd,
    )

    result = back_translation("Ce film était vraiment génial", lang="fr")
    assert result == "Ce film était vraiment génial"
    # FR→EN appelé avant EN→FR
    assert fwd.calls == ["Ce film était vraiment génial"]
    assert bwd.calls == ["This movie was really great"]


def test_back_translation_en_to_fr_to_en(monkeypatch):
    """Pour un texte anglais, le flux EN→FR puis FR→EN est appliqué."""
    fwd = _FakePipeline("Ce film était vraiment mauvais")
    bwd = _FakePipeline("This movie was really bad")
    monkeypatch.setattr(
        eda, "_get_translation_pipeline",
        lambda src, tgt: fwd if (src, tgt) == ("en", "fr") else bwd,
    )

    result = back_translation("This movie was really bad", lang="en")
    assert result == "This movie was really bad"
    assert fwd.calls == ["This movie was really bad"]
    assert bwd.calls == ["Ce film était vraiment mauvais"]


def test_back_translation_short_text_unchanged(monkeypatch):
    """Les textes trop courts ne sont pas traduits (peu fiable)."""
    def _boom(*args, **kwargs):
        raise AssertionError("Aucun modèle ne doit être chargé")

    monkeypatch.setattr(eda, "_get_translation_pipeline", _boom)
    assert back_translation("Super !", lang="fr") == "Super !"


def test_back_translation_negation_not_lost(monkeypatch):
    """Si la traduction perd la négation, le texte original est retenu."""
    fwd = _FakePipeline("This movie is bad")  # "pas mauvais" -> "bad" : négation perdue
    bwd = _FakePipeline("Ce film est mauvais")
    monkeypatch.setattr(
        eda, "_get_translation_pipeline",
        lambda src, tgt: fwd if (src, tgt) == ("fr", "en") else bwd,
    )

    result = back_translation("Ce film n'est pas mauvais", lang="fr")
    assert result == "Ce film n'est pas mauvais"


def test_back_translation_negation_not_added(monkeypatch):
    """Si la traduction introduit une négation absente du texte source,
    le texte original est retenu (inversion de sentiment potentielle)."""
    fwd = _FakePipeline("Ce n'est pas un bon film")
    bwd = _FakePipeline("c'est pas un bon film du tout")
    monkeypatch.setattr(
        eda, "_get_translation_pipeline",
        lambda src, tgt: fwd if (src, tgt) == ("fr", "en") else bwd,
    )

    result = back_translation("C'est un bon film du tout", lang="fr")
    assert result == "C'est un bon film du tout"


def test_back_translation_failure_falls_back_to_original(monkeypatch):
    """Toute erreur (modèle indisponible, offline...) retombe sur l'original."""
    def _boom(*args, **kwargs):
        raise RuntimeError("model unavailable")

    monkeypatch.setattr(eda, "_get_translation_pipeline", _boom)
    text = "Ce film était vraiment excellent"
    assert back_translation(text, lang="fr") == text


def test_recompose_uses_back_translation_when_enabled(monkeypatch):
    """Avec use_back_translation=True, la 1re variante vient de la BT."""
    fwd = _FakePipeline("This movie was really great")
    bwd = _FakePipeline("Ce film était vraiment excellent")
    monkeypatch.setattr(
        eda, "_get_translation_pipeline",
        lambda src, tgt: fwd if (src, tgt) == ("fr", "en") else bwd,
    )

    random.seed(42)
    variants = recompose(
        "Ce film était vraiment génial",
        lang="fr",
        num_variants=3,
        alpha=0.2,
        use_back_translation=True,
    )
    # La variante de back-translation est présente parmi les variantes
    assert "Ce film était vraiment excellent" in variants
    assert fwd.calls, "Le pipeline FR→EN doit avoir été appelé"

