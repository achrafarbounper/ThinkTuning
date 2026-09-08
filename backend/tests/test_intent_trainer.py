"""Tests des helpers purs de ``core.intent_trainer`` (INTENT_TRAINING.md §13).

Checklist #1 — diagnostic classification_report chat↔action :
les confusions ``chat ↔ action`` (invisibles avec l'accuracy seule) pendant
l'entraînement :

  - ``_intent_classification_report`` : rapport sklearn (précision/rappel/F1
    par classe + macro), matrice de confusion et F1 macro / F1 par classe ;
  - ``_format_intent_report`` : mise en forme logs lisible (parité CLI/API,
    le bloc est capturé par ``core.job_logs`` → ``/train/stream``).

Checklist #2 — split train/val stratifié :
  - ``_split_records`` : ``train_test_split(stratify=labels)`` → répartition
    des classes préservée en val, y compris pour une classe minoritaire ;
    cas limites : 1 seul exemple (pas de split), classe à 1 occurrence (repli
    shuffle documenté).

Checklist #3 — tokenisation « padding dynamique + bucketisation » :
  - ``_padding_bucket_config`` : source unique CLI/API — pas de padding fixe
    (``padding=False``), padding par batch (``DataCollatorWithPadding``),
    ``train_sampling_strategy="group_by_length"`` (v5, LengthGroupedSampler HF).

Checklist #3 — scheduler LR :
  - ``_scheduler_training_args`` : source unique CLI/API — ``cosine`` + warmup
    10 % (v5 : ``warmup_ratio`` retiré, ``warmup_steps`` float ∈ [0, 1[ =
    fraction du nombre total de steps).

Checklist #3 — meilleur checkpoint + early stopping :
  - ``_best_checkpoint_training_args`` : source unique CLI/API — checkpoints
    par epoch bornés (``save_total_limit=2``), meilleur epoch (``accuracy`` de
    val) rechargé avant la sauvegarde finale, ``EarlyStoppingCallback``
    (patience 2) ; repli « une seule version finale » sans val.

Aucune dépendance lourde (pas de torch/transformers) ni réseau : on teste les
fonctions pures sur des échantillons déterministes.
"""

from __future__ import annotations

import logging
from collections import Counter

import pytest

from core.intent_trainer import (
    EARLY_STOPPING_PATIENCE,
    _best_checkpoint_training_args,
    _format_intent_report,
    _intent_classification_report,
    _padding_bucket_config,
    _scheduler_training_args,
    _split_records,
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


class TestSplitRecords:
    """Split train/val stratifié (`_split_records`, §13 checklist #2)."""

    def _records(self, n: int, minority_ratio: float = 0.5) -> list:
        """n exemples ; label `action` minoritaire si ratio < 0.5."""
        records = [
            {"text": f"chat-{i}", "label": "chat"}
            for i in range(round(n * (1 - minority_ratio)))
        ]
        records += [
            {"text": f"action-{i}", "label": "action"}
            for i in range(round(n * minority_ratio))
        ]
        return records

    def test_stratifie_minorite_representee_en_val(self) -> None:
        # 90 % chat / 10 % action : la classe minoritaire doit apparaître en
        # val (un shuffle pouvait l'en exclure par malchance) et sa proportion
        # doit refléter le dataset.
        records = self._records(100, minority_ratio=0.1)
        train, val = _split_records(records, 0.1)

        assert len(train) + len(val) == len(records)
        assert val
        val_counts = Counter(r["label"] for r in val)
        assert "action" in val_counts
        assert "chat" in val_counts
        share = val_counts["action"] / len(val)
        assert share == pytest.approx(0.1, abs=0.05)

    def test_deterministe_meme_graine(self) -> None:
        records = self._records(60, minority_ratio=1 / 3)
        t1, v1 = _split_records(records, 0.15, seed=42)
        t2, v2 = _split_records(records, 0.15, seed=42)
        assert [r["text"] for r in t1] == [r["text"] for r in t2]
        assert [r["text"] for r in v1] == [r["text"] for r in v2]

    def test_single_exemple_pas_de_split(self) -> None:
        records = [{"text": "seul exemple", "label": "chat"}]
        train, val = _split_records(records, 0.1)
        assert train == records
        assert val == []

    def test_classe_a_une_occurrence_repli_shuffle(self, caplog) -> None:
        # 19 chat + 1 classe « rare » : stratify impossible (ValueError
        # sklearn) → repli shuffle documenté, l'entraînement ne plante pas.
        records = [{"text": f"x{i}", "label": "chat"} for i in range(19)]
        records.append({"text": "phrase rare", "label": "rare"})
        with caplog.at_level(logging.WARNING, logger="core.intent_trainer"):
            train, val = _split_records(records, 0.1)
        assert len(train) + len(val) == len(records)
        assert val
        assert "repli shuffle" in caplog.text

    def test_jeu_trop_petit_pour_test_size(self) -> None:
        # 2 exemples, test_size=0.6 → train totalement vide côté sklearn →
        # repli shuffle ; on conserve un split 1/1 utilisable.
        records = [
            {"text": "bonjour", "label": "chat"},
            {"text": "fais une action", "label": "action"},
        ]
        train, val = _split_records(records, 0.6)
        assert len(train) == 1 and len(val) == 1
        assert {r["label"] for r in train + val} == {"chat", "action"}

    def test_taille_val_par_defaut(self) -> None:
        records = self._records(200, minority_ratio=0.5)
        train, val = _split_records(records, 0.1)
        assert len(val) == 20  # ceil(200 * 0.1)
        assert len(train) == 180


class TestPaddingBucketConfig:
    """Config « padding dynamique + bucketisation » (§13 checklist #3)."""

    def test_pas_de_padding_fixe(self) -> None:
        cfg = _padding_bucket_config(64)["tokenizer"]
        assert cfg == {"padding": False, "truncation": True, "max_length": 64}

    def test_max_length_propage_comme_troncature(self) -> None:
        cfg = _padding_bucket_config(128)["tokenizer"]
        assert cfg["max_length"] == 128
        assert cfg["padding"] is False
        assert cfg["truncation"] is True

    def test_padding_par_batch_et_bucketisation(self) -> None:
        cfg = _padding_bucket_config(128)
        # Le DataCollator pad à la longueur réelle de chaque batch.
        assert cfg["collator"] == {"padding": True}
        # v5 : train_sampling_strategy="group_by_length" (LengthGroupedSampler
        # HF) — remplace l'ancien group_by_length=True retiré en v5.
        assert cfg["training_args"] == {"train_sampling_strategy": "group_by_length"}


class TestSchedulerTrainingArgs:
    """Config « scheduler LR » (§13 checklist #3) — cosine + warmup 10 %."""

    def test_cosine_et_warmup_10pct(self) -> None:
        cfg = _scheduler_training_args()
        assert cfg["lr_scheduler_type"] == "cosine"
        # v5 : `warmup_ratio` a été retiré des TrainingArguments ; un float
        # ∈ [0, 1[ dans `warmup_steps` est interprété comme une fraction du
        # nombre total de steps (TrainingArguments.get_warmup_steps).
        assert cfg["warmup_steps"] == pytest.approx(0.1)

    def test_kwargs_consommes_tels_quels_par_api_et_cli(self) -> None:
        # Le dict est étalé (**kwargs) tel quel dans les deux appels
        # TrainingArguments (API _run_intent_pipeline et CLI train_intent) :
        # parité mécanique CLI/API, aucune clé superflue.
        assert set(_scheduler_training_args()) == {
            "lr_scheduler_type",
            "warmup_steps",
        }


class TestBestCheckpointTrainingArgs:
    """Config « meilleur checkpoint + early stopping » (§13 checklist #3)."""

    def test_mode_meilleur_checkpoint_avec_val(self) -> None:
        cfg = _best_checkpoint_training_args(True)
        assert cfg["eval_strategy"] == "epoch"
        assert cfg["save_strategy"] == "epoch"
        assert cfg["save_total_limit"] == 2
        assert cfg["metric_for_best_model"] == "accuracy"
        assert cfg["load_best_model_at_end"] is True

    def test_repli_historique_sans_val(self) -> None:
        # Dataset à 1 exemple → val vide : aucune métrique → repli « une seule
        # version finale » ; load_best_model_at_end exige de plus que save/eval
        # strategies matchent (v5) — d'où eval/save à "no".
        cfg = _best_checkpoint_training_args(False)
        assert cfg == {
            "eval_strategy": "no",
            "save_strategy": "no",
            "load_best_model_at_end": False,
        }

    def test_patience_early_stopping(self) -> None:
        assert EARLY_STOPPING_PATIENCE == 2