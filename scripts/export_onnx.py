"""Export d'un modèle HuggingFace vers ONNX (Phase 3).

Usage :
    python scripts/export_onnx.py --model experiments/models/20260905T084158Z \
        --output experiments/onnx/sentiment --task sentiment [--opset 14]

Le modèle source est un dossier valide pour ``AutoModelForSequenceClassification``
(comme ``experiments/models/<version>`` ou ``sentiment_model_final/``). Le
fichier ``model.onnx`` produit est consommable par
``core.onnx_exporter.ONNXClassificationEngine``.

Note : la sortie PyTorch reste le chemin par défaut de l'inférence ; cet
export ONNX est une optimisation optionnelle (~30-50 % plus rapide sur CPU).
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core.onnx_exporter import export_model_to_onnx  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(message)s")
logger = logging.getLogger("export_onnx")

_TASK_LABELS = {
    "sentiment": ("negative", "neutral", "positive"),
    "intent": ("chat", "action"),
}


def _load_model(model_dir: Path, output_dir: Path, task: str, opset: int) -> None:
    """Charge tokenizer + modèle, exporte ONNX, affiche le résultat."""
    from transformers import AutoModelForSequenceClassification, AutoTokenizer

    if task not in _TASK_LABELS:
        raise SystemExit(f"Tâche inconnue : {task!r} (attendu : sentiment, intent)")

    config_path = model_dir / "config.json"
    num_labels = None
    if config_path.is_file():
        with open(config_path, encoding="utf-8") as fh:
            hf_config = json.load(fh)
        num_labels = hf_config.get("num_labels")

    logger.info("Chargement du modèle %s (num_labels=%s)", model_dir, num_labels)
    tokenizer = AutoTokenizer.from_pretrained(str(model_dir))
    model = AutoModelForSequenceClassification.from_pretrained(
        str(model_dir),
        num_labels=num_labels,
    )

    onnx_path = export_model_to_onnx(
        model,
        tokenizer,
        output_path=output_dir / "model.onnx",
        opset_version=opset,
    )
    logger.info(
        "Export réussi : %s | labels=%s | taille=%d octets",
        onnx_path,
        _TASK_LABELS[task],
        onnx_path.stat().st_size,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Export HuggingFace -> ONNX")
    parser.add_argument(
        "--model",
        required=True,
        help="Dossier du modèle source (ex. experiments/models/<version>)",
    )
    parser.add_argument(
        "--output",
        default="experiments/onnx/model",
        help="Dossier de sortie (le fichier model.onnx y sera écrit)",
    )
    parser.add_argument(
        "--task",
        choices=tuple(_TASK_LABELS),
        default="sentiment",
        help="Tâche servie par la tête (détermine les labels)",
    )
    parser.add_argument(
        "--opset",
        type=int,
        default=14,
        help="Version opset ONNX (défaut 14)",
    )
    args = parser.parse_args()

    _load_model(Path(args.model), Path(args.output), args.task, args.opset)


if __name__ == "__main__":
    main()
