"""
Active learning : sélection des exemples les plus incertains pour annotation
manuelle prioritaire.

Pour un problème à 3 classes (negative / neutral / positive), l'incertitude
maximale du modèle correspond à une distribution de probabilité uniforme sur
les 3 classes, soit une confidence (probabilité de la classe prédite) de 1/3.
Plus la confidence prédite est proche de 1/3, plus l'exemple est incertain et
mérite une review manuelle en priorité (moins la confidence s'éloigne de 1,
plus le modèle est sûr de lui — mais ce n'est PAS ce qu'on utilise ici comme
critère : on mesure la proximité à 1/3, pas à 1).

Usage :
    python active_learning.py --input data/train.jsonl
        # utilise la DERNIÈRE version valide dans experiments/models
    python active_learning.py --input unlabeled.csv --text_column text --top_n 50
    python active_learning.py --input data/train.jsonl --model_path 20260819T151459Z
        # une version précise du dossier experiments/models
    python active_learning.py --input data/train.jsonl --output data/manual_review_template.csv --model_path experiments/models/20260819T151459Z
"""

import argparse
import csv
import os
from pathlib import Path
from typing import Iterable, List, Optional

from core.model_versioning import resolve_model_path
from label_dataset import load_texts_from_file
from src.inference.predictor import Predictor

# Incertitude maximale pour une classification à 3 classes : distribution
# uniforme sur les 3 classes -> probabilité de la classe prédite = 1/3.
MAX_UNCERTAINTY_CONFIDENCE = 1 / 3

DEFAULT_OUTPUT = "data/manual_review_template.csv"


def _chunked(iterable: Iterable[str], size: int):
    batch = []
    for item in iterable:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def compute_uncertainty(confidence: float) -> float:
    """
    Score d'incertitude basé sur la distance entre la confidence prédite et
    1/3 (incertitude maximale pour 3 classes).

    Renvoie une valeur dans [0, 1] : 1.0 quand confidence == 1/3 (incertitude
    maximale), et décroît à mesure que confidence s'éloigne de 1/3 (que ce
    soit vers 0 ou vers 1 — en pratique la confidence, étant un max de
    softmax sur 3 classes, ne descend jamais sous 1/3).
    """
    distance_to_third = abs(confidence - MAX_UNCERTAINTY_CONFIDENCE)
    return 1.0 - distance_to_third


def select_uncertain_examples(
    texts: List[str],
    model_path: Optional[str] = None,
    batch_size: int = 32,
    top_n: Optional[int] = None,
) -> List[dict]:
    """
    Prédit le sentiment de chaque texte puis calcule un score d'incertitude
    basé sur la proximité de la confidence à 1/3.

    Returns:
        Liste de dicts {text, predicted_label, confidence, uncertainty},
        triée par incertitude décroissante (exemples les plus incertains,
        donc les plus proches de 1/3, en premier).
    """
    if not texts:
        return []

    predictor = Predictor(resolve_model_path(model_path))
    records = []

    for batch in _chunked(texts, batch_size):
        predictions = predictor.predict(batch)
        for pred in predictions:
            confidence = float(pred.get("confidence", 0.0))
            records.append({
                "text": pred["text"],
                "predicted_label": pred["sentiment"],
                "confidence": confidence,
                "uncertainty": compute_uncertainty(confidence),
            })

    # Incertitude décroissante : exemples les plus proches de 1/3 en premier.
    records.sort(key=lambda r: r["uncertainty"], reverse=True)

    if top_n is not None:
        records = records[:top_n]

    return records


def write_manual_review_csv(records: List[dict], output_path: str):
    """Exporte les enregistrements dans le format attendu par le workflow de review manuelle."""
    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    with out_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=["text", "predicted_label", "manual_label", "status"])
        writer.writeheader()
        for record in records:
            writer.writerow({
                "text": record["text"],
                "predicted_label": record["predicted_label"],
                "manual_label": "",
                "status": "",
            })


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Sélectionne les exemples les plus incertains (confidence la plus "
            "proche de 1/3) pour prioriser leur review manuelle."
        )
    )
    parser.add_argument("--input", required=True, help="CSV/JSON/JSONL/TXT contenant les textes à évaluer.")
    parser.add_argument("--output", default=DEFAULT_OUTPUT, help=f"CSV de review en sortie (défaut: {DEFAULT_OUTPUT}).")
    parser.add_argument(
        "--model_path",
        default=None,
        help="Version dans experiments/models (ex: 20260819T151459Z) ou chemin de dossier. Défaut: dernière version.",
    )
    parser.add_argument("--text_column", default="text", help="Nom de la colonne texte pour les fichiers structurés.")
    parser.add_argument("--batch_size", type=int, default=32, help="Taille de batch utilisée pour l'inférence.")
    parser.add_argument(
        "--top_n",
        type=int,
        default=None,
        help="Nombre maximal d'exemples les plus incertains à exporter (défaut: tous, triés par incertitude).",
    )
    args = parser.parse_args()

    texts = load_texts_from_file(args.input, text_column=args.text_column)
    if not texts:
        print(f"Aucun texte trouvé dans {args.input}")
        return

    model_dir = resolve_model_path(args.model_path)
    print(f"Modèle utilisé : {model_dir}")

    records = select_uncertain_examples(
        texts,
        model_path=model_dir,
        batch_size=args.batch_size,
        top_n=args.top_n,
    )

    write_manual_review_csv(records, args.output)
    print(f"Exported {len(records)} examples to {args.output}")


if __name__ == "__main__":
    main()