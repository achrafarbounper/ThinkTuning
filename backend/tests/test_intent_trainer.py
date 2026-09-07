"""Tests du diagnostic classification_report chat↔action (INTENT_TRAINING.md §13 — checklist #1).

Cible : les helpers purs de ``core.intent_trainer`` qui rendent visibles les
confusions ``chat ↔ action`` (invisibles avec l'accuracy seule) pendant
l'entraînement :

  - ``_intent_classification_report`` : rapport sklearn (précision/rappel/F1
    par classe + macro), matrice de confusion et F1 macro / F1 par classe ;
  - ``_format_intent_report`` : mise en forme logs lisible (parité CLI/API,
    le bloc est capturé par ``core.job_logs`` → ``/train/stream``).

Aucune dépendance lourde (pas de torch/transformers) ni réseau : on teste les
fonctions pures sur des échantillons déterministes.
"""

from __future__ import annotations

import pytest

from core.intent_trainer import (
    _format_intent_report,
    _intent_classification_report,
)

LABELS = ["chat", "action"]


class TestIntentClassificationReport:
    def test_perfect_classification_f1_un(self) -> None:
        preds = [0, 0, 1, 1]
        golds = [0, 0, 1, 1]
        diag = _intent_classification_report(preds, golds, LABELS)

        assert diag["f1_macro"] == pytest.approx(1.0)
        assert diag["f1_per_class"] == {"chat": 1.0, "action": 1.0}
        assert diag["confusion_matrix"] == [[2, 0], [0, 2]]
        for name in LABELS:
            row = diag["report"][name]
            assert row["precision"] == pytest.approx(1.0)
            assert row["recall"] == pytest.approx(1.0)
            assert row["f1-score"] == pytest.approx(1.0)

    def test_confusions_chat_action_visibles(self) -> None:
        # 2 confusions : 1 chat prédit action, 1 action prédite chat.
        preds = [0, 1, 0, 1]
        golds = [0, 0, 1, 1]
        diag = _intent_classification_report(preds, golds, LABELS)

        # Ligne gold / colonne pred : [[chat→chat, chat→action],
        #                              [action→chat, action→action]].
        assert diag["confusion_matrix"] == [[1, 1], [1, 1]]
        assert diag["f1_macro"] == pytest.approx(0.5)
        assert diag["f1_per_class"] == {"chat": 0.5, "action": 0.5}

    def test_classe_absente_zero_division(self) -> None:
        # Aucune prédiction correcte et classe "action" absente des golds :
        # le rapport ne doit pas lever (zero_division=0) et reste défini.
        preds = [1, 1]
        golds = [0, 0]
        diag = _intent_classification_report(preds, golds, LABELS)

        assert diag["confusion_matrix"] == [[0, 2], [0, 0]]
        assert diag["f1_macro"] == pytest.approx(0.0)
        assert diag["f1_per_class"] == {"chat": 0.0, "action": 0.0}

    def test_accepte_numpy_and_lists(self) -> None:
        import numpy as np

        preds = np.array([0, 1, 0, 1])
        golds = np.array([0, 1, 1, 1])
        diag = _intent_classification_report(preds, golds, LABELS)

        # golds: chat,action,action,action | preds: chat,action,chat,action
        assert diag["confusion_matrix"] == [[1, 0], [1, 2]]
        assert diag["report"]["action"]["support"] == 3


class TestFormatIntentReport:
    def test_contient_header_et_matrice(self) -> None:
        diag = _intent_classification_report([0, 1, 0, 1], [0, 0, 1, 1], LABELS)
        text = _format_intent_report(diag, LABELS)

        assert "classification report" in text
        assert "precision" in text and "rappel" in text and "f1" in text
        for name in LABELS:
            assert name in text
        assert "matrice de confusion" in text
        # Les deux lignes de la matrice 2x2 sont rendues (2 colonnes par ligne).
        rows = [
            line for line in text.splitlines() if line.strip().startswith("1")
        ]
        assert len(rows) >= 1
        # La cellule chat→action vaut 1 (confusion visible).
        collapsed = " ".join(text.split())
        assert "1 1" in collapsed
        assert "macro" in text