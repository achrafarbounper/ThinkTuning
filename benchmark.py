"""
Benchmark de comparaison DistilBERT (Predictor) vs LLM fine-tuné (predict_llm.py).

Évalue les deux approches sur un même jeu de test et produit :
    * un rapport JSON (--output)
    * un tableau console via ``rich``

Usage :
    python benchmark.py --distilbert_path ./sentiment_model_final \
        --llm_path CHEMIN_VERSION_LLM --test_file data/val.jsonl --output report.json

Le LLM est optionnel : si ``--llm_path`` est absent, seul DistilBERT est évalué.
Le benchmark fonctionne par défaut sur CPU (aucun GPU requis).

Compatibilité d'entrée :
  - ``data/val.jsonl`` (format Alpaca produit par label_dataset.py : clés
    "input"/"output"), également géré par evaluate_weak_supervision.py via les
    clés ``text``/``label``/``sentiment``.
  - N'importe quel fichier CSV (colonnes ``text_column`` / ``label_column``).
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Tuple

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Choix du device AVANT l'import de torch/transformers : en forçant la variable
# d'environnement CUDA_VISIBLE_DEVICES, torch ne voit aucun GPU et les modèles
# sont chargés sur CPU (exigence : pas de GPU nécessaire).
# --------------------------------------------------------------------------- #
def _select_device_from_argv(argv: List[str]) -> str:
    if "--device" in argv:
        idx = argv.index("--device")
        if idx + 1 < len(argv) and not argv[idx + 1].startswith("-"):
            return argv[idx + 1]
    return "cpu"


_DEVICE_AT_IMPORT = _select_device_from_argv(sys.argv[1:])
if _DEVICE_AT_IMPORT == "cpu":
    os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

import psutil  # noqa: E402  (torch/transformers importés paresseusement plus bas)


LABELS = ["negative", "neutral", "positive"]

# Alias acceptés pour normaliser les labels (EN/FR) vers LABELS.
_LABEL_ALIASES: Dict[str, str] = {
    "negative": "negative",
    "négatif": "negative",
    "negatif": "negative",
    "mauvais": "negative",
    "décevant": "negative",
    "decevant": "negative",
    "bad": "negative",
    "terrible": "negative",
    "neutral": "neutral",
    "neutre": "neutral",
    "mixed": "neutral",
    "mitigé": "neutral",
    "mitige": "neutral",
    "moyen": "neutral",
    "okay": "neutral",
    "ok": "neutral",
    "average": "neutral",
    "positive": "positive",
    "positif": "positive",
    "bon": "positive",
    "good": "positive",
    "excellent": "positive",
    "satisfait": "positive",
    "content": "positive",
    "ravi": "positive",
}


def normalize_label(value) -> str:
    """Normalise un label brut en l'un de LABELS, ou 'unknown' sinon."""
    if value is None:
        return "unknown"
    normalized = str(value).strip().lower()
    if not normalized:
        return "unknown"
    if normalized in _LABEL_ALIASES:
        return _LABEL_ALIASES[normalized]
    for canonical in LABELS:
        if canonical in normalized:
            return canonical
    return "unknown"


def resolve_prediction(prediction: str, gold: str) -> str:
    """
    Convertit une prédiction brute en l'un de LABELS.

    Une prédiction 'unknown' (LLM qui n'a pas produit de sentiment lisible)
    est convertie en une classe DÉLIBÉRÉMENT fausse (différente du gold) :
    elle compte alors comme une erreur (false negative sur le gold + false
    positive sur la classe prédite), ce qui pénalise honnêtement le modèle.
    """
    normalized = normalize_label(prediction)
    if normalized in LABELS:
        return normalized
    # oracle faux : choix déterministe garantissant prédiction != gold
    for candidate in LABELS:
        if candidate != gold:
            return candidate
    return "unknown"


def compute_metrics(predictions: List[str], golds: List[str]) -> Dict[str, float]:
    """Calcule accuracy, F1 macro et F1 par classe (negative/neutral/positive)."""
    from sklearn.metrics import accuracy_score, f1_score

    resolved = [resolve_prediction(p, g) for p, g in zip(predictions, golds)]
    n = len(golds)

    accuracy = accuracy_score(golds, resolved) if n else 0.0
    f1_per_class = f1_score(
        golds, resolved, labels=LABELS, average=None, zero_division=0
    )
    f1_macro = (
        float(f1_score(golds, resolved, average="macro", zero_division=0)) if n else 0.0
    )

    return {
        "accuracy": float(accuracy),
        "f1_macro": f1_macro,
        "f1_negative": float(f1_per_class[0]),
        "f1_neutral": float(f1_per_class[1]),
        "f1_positive": float(f1_per_class[2]),
    }


# --------------------------------------------------------------------------- #
# Chargement du jeu de test (compatibilité evaluate_weak_supervision.py)
# --------------------------------------------------------------------------- #
def _first_value(record: dict, keys: Iterable[str]):
    for key in keys:
        if key in record and record.get(key) is not None:
            return record[key]
    return None


def _text_and_label(
    record: dict, text_column: str, label_column: str
) -> Optional[Tuple[str, str]]:
    text = _first_value(record, (text_column, "input", "text", "sentence"))
    if text is None or not str(text).strip():
        return None
    label = _first_value(record, (label_column, "output", "label", "sentiment"))
    if label is None:
        label = ""
    return str(text).strip(), str(label)


def load_records(
    test_file: str, text_column: str, label_column: str
) -> List[Tuple[str, str]]:
    """Charge textes + labels gold depuis JSONL (Alpaca) ou CSV.

    Retourne une liste de tuples (texte, label). Les lignes sans texte sont
    ignorées. Format compatible avec evaluate_weak_supervision.py.
    """
    path = Path(test_file)
    if not path.exists():
        raise FileNotFoundError(f"Test file not found: {test_file}")

    suffix = path.suffix.lower()
    records: List[Tuple[str, str]] = []

    if suffix == ".jsonl":
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                row = json.loads(line)
                if isinstance(row, dict):
                    parsed = _text_and_label(row, text_column, label_column)
                    if parsed is not None:
                        records.append(parsed)
        return records

    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as handle:
            for row in csv.DictReader(handle):
                parsed = _text_and_label(row, text_column, label_column)
                if parsed is not None:
                    records.append(parsed)
        return records

    raise ValueError(
        f"Unsupported test file format: {suffix or 'unknown'} (JSONL/CSV only)."
    )


# --------------------------------------------------------------------------- #
# Mesure de la mémoire RSS (peak) et utilitaires
# --------------------------------------------------------------------------- #
def rss_mb() -> float:
    """Mémoire RSS courante du processus, en Mo."""
    return psutil.Process().memory_info().rss / (1024 * 1024)


def _chunked(iterable: Iterable[str], size: int) -> Iterable[List[str]]:
    batch: List[str] = []
    for item in iterable:
        batch.append(item)
        if len(batch) == size:
            yield batch
            batch = []
    if batch:
        yield batch


def measure_engine(
    infer_fn: Callable[[List[str]], List[dict]], texts: List[str]
) -> dict:
    """Mesure latence (ms/texte) et pic de RSS pendant l'inférence.

    `infer_fn(texts)` doit renvoyer une liste de dicts avec la clé `sentiment`
    (une entrée par texte, dans le même ordre).
    """
    if not texts:
        return {
            "predicted_labels": [],
            "latency_per_text_ms": 0.0,
            "total_time_ms": 0.0,
            "rss_mb": rss_mb(),
        }

    peak = rss_mb()
    start = time.perf_counter()
    results = infer_fn(texts)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    peak = max(peak, rss_mb())

    return {
        "predicted_labels": [r.get("sentiment") for r in results],
        "total_time_ms": elapsed_ms,
        "latency_per_text_ms": elapsed_ms / len(texts),
        "rss_mb": peak,
        "n_predictions": len(results),
    }


def create_distilbert_runner(
    model_path: str, device: str, batch_size: int
) -> Callable[[List[str]], List[dict]]:
    """Fabrique la fonction d'inférence utilisant Predictor (DistilBERT)."""
    from src.inference.predictor import Predictor

    predictor = Predictor(model_path)
    if hasattr(predictor, "model"):
        predictor.model.to(device)
        predictor.model.eval()

    def infer(texts: List[str]) -> List[dict]:
        results: List[dict] = []
        for batch in _chunked(texts, batch_size):
            results.extend(predictor.predict(batch))
        return results

    return infer


def create_llm_runner(
    model_path: str,
    base_model: Optional[str],
    max_new_tokens: int,
    device: str,
) -> Callable[[List[str]], List[dict]]:
    """Fabrique la factory d'inférence pour le LLM fine-tuné (predict_text)."""
    from predict_llm import load_model, load_tokenizer, predict_text

    model = load_model(model_path, base_model)
    tokenizer = load_tokenizer(model_path, base_model)
    model = model.to(device)
    model.eval()

    def infer(texts: List[str]) -> List[dict]:
        return [predict_text(model, tokenizer, text, max_new_tokens) for text in texts]

    return infer


# --------------------------------------------------------------------------- #
# Rapport JSON + table console (rich)
# --------------------------------------------------------------------------- #
def build_report(config: dict, dataset: dict, models: Dict[str, Dict]) -> dict:
    return {
        "config": config,
        "dataset": dataset,
        "models": models,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def display_table(models: Dict[str, Dict]):
    """Affiche un tableau de comparaison via rich."""
    from rich.console import Console
    from rich.table import Table

    console = Console()

    if not models:
        console.print("[yellow]Aucun modèle évalué.[/yellow]")
        return None

    table = Table(title="Benchmark DistilBERT vs LLM fine-tuné")
    for column in (
        "Modèle",
        "Accuracy",
        "F1 macro",
        "F1 neg",
        "F1 neut",
        "F1 pos",
        "Latence (ms/texte)",
        "RSS (Mo)",
    ):
        table.add_column(column, justify="right" if column != "Modèle" else "left")

    best_model = None
    best_accuracy = -1.0
    for name, entry in sorted(models.items()):
        metrics = entry.get("metrics")
        if metrics is None:
            continue  # modèle en erreur : on ne l'affiche pas dans le tableau
        table.add_row(
            entry.get("display_name", name),
            f"{metrics['accuracy']:.4f}",
            f"{metrics['f1_macro']:.4f}",
            f"{metrics['f1_negative']:.4f}",
            f"{metrics['f1_neutral']:.4f}",
            f"{metrics['f1_positive']:.4f}",
            f"{entry['latency_per_text_ms']:.2f}",
            f"{entry['rss_mb']:.1f}",
        )
        if metrics["accuracy"] > best_accuracy:
            best_accuracy = metrics["accuracy"]
            best_model = name

    console.print(table)

    if best_model is not None:
        console.print(
            f"\n[green]→ Meilleure accuracy :[/green] [bold]{best_model}[/bold] "
            f"(accuracy = {best_accuracy:.4f})."
        )
    return table


# --------------------------------------------------------------------------- #
# Interface CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Benchmark DistilBERT (Predictor) vs LLM fine-tuné sur un même jeu de test."
    )
    parser.add_argument(
        "--distilbert_path",
        default="./sentiment_model_final",
        help="Chemin vers le modèle DistilBERT (dossier ou state_dict).",
    )
    parser.add_argument(
        "--llm_path",
        default=None,
        help="Chemin vers le modèle LLM fine-tuné (adapter ou merged). Optionnel :"
        " sans cette option, seul DistilBERT est évalué.",
    )
    parser.add_argument(
        "--base_model",
        default=None,
        help="Modèle de base nécessaire pour charger un adapter LoRA/QLoRA.",
    )
    parser.add_argument(
        "--test_file",
        default="data/val.jsonl",
        help="Jeu de test JSONL (Alpaca) ou CSV (défaut: data/val.jsonl).",
    )
    parser.add_argument(
        "--output",
        dest="output_file",
        default="report.json",
        help="Chemin du rapport JSON.",
    )
    parser.add_argument(
        "--text_column",
        default="text",
        help="Nom de la colonne texte pour les fichiers CSV.",
    )
    parser.add_argument(
        "--label_column",
        default="label",
        help="Nom de la colonne label pour les fichiers CSV (label ou sentiment).",
    )
    parser.add_argument(
        "--device",
        default="cpu",
        help="Device d'inférence : 'cpu' (défaut, aucun GPU requis) ou 'cuda'.",
    )
    parser.add_argument(
        "--batch_size", type=int, default=32, help="Batch size pour DistilBERT."
    )
    parser.add_argument(
        "--max_new_tokens", type=int, default=32, help="Tokens générés max pour le LLM."
    )
    parser.add_argument(
        "--max_texts",
        type=int,
        default=None,
        help="Limite le nombre de textes évalués (pratique pour un smoke test).",
    )
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> dict:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    args = parse_args(argv)

    if args.device == "cpu":
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    texts_and_golds = load_records(args.test_file, args.text_column, args.label_column)
    if not texts_and_golds:
        raise ValueError(f"No text records found in {args.test_file}")

    if args.max_texts is not None:
        texts_and_golds = texts_and_golds[: args.max_texts]

    # Ne garde que les lignes dont le gold est interprétable (compat à
    # evaluate_weak_supervision : tout label 'unknown' est ignoré).
    valid = []
    skipped = 0
    for text, gold in texts_and_golds:
        resolved_gold = normalize_label(gold)
        if resolved_gold in LABELS:
            valid.append((text, resolved_gold))
        else:
            skipped += 1

    if not valid:
        raise ValueError(
            f"Aucun label gold résolvable dans {args.test_file} "
            "(clés attendues : output/label/sentiment)."
        )

    texts = [t for t, _ in valid]
    golds = [g for _, g in valid]

    from collections import Counter

    distribution = Counter(golds)
    dataset_info = {
        "path": os.path.abspath(args.test_file),
        "n_texts_total": len(texts_and_golds),
        "n_evaluated": len(texts),
        "n_skipped_unresolvable_gold": skipped,
        "class_distribution": {label: distribution.get(label, 0) for label in LABELS},
    }

    run_config = {
        "distilbert_path": args.distilbert_path,
        "llm_path": args.llm_path,
        "base_model": args.base_model,
        "device": args.device,
        "batch_size": args.batch_size,
        "max_new_tokens": args.max_new_tokens,
    }

    models: Dict[str, Dict] = {}

    # --- DistilBERT ---
    if args.distilbert_path:
        runner = create_distilbert_runner(
            args.distilbert_path, args.device, args.batch_size
        )
        measured = measure_engine(runner, texts)
        metrics = compute_metrics(measured.pop("predicted_labels"), golds)
        models["distilbert"] = {
            "display_name": "DistilBERT (Predictor)",
            "model_path": os.path.abspath(args.distilbert_path),
            "metrics": metrics,
            **measured,
        }

    # --- LLM (optionnel) ---
    if args.llm_path:
        try:
            runner = create_llm_runner(
                args.llm_path, args.base_model, args.max_new_tokens, args.device
            )
            measured = measure_engine(runner, texts)
            metrics = compute_metrics(measured.pop("predicted_labels"), golds)
            models["llm"] = {
                "display_name": "LLM fine-tuned",
                "model_path": os.path.abspath(args.llm_path),
                "metrics": metrics,
                **measured,
            }
        except Exception as exc:  # pragma: no cover - runtime failure path
            models["llm"] = {
                "display_name": "LLM fine-tuned",
                "model_path": os.path.abspath(args.llm_path),
                "metrics": None,
                "error": str(exc),
            }
            logger.warning(f"[benchmark] LLM skipped, error: {exc}")

    report = build_report(run_config, dataset_info, models)
    display_table(models)

    # Sauvegarde du rapport JSON
    out_path = Path(args.output_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    logger.info(f"Rapport écrit : {out_path}")
    return report


if __name__ == "__main__":
    main()