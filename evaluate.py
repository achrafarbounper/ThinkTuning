"""
Évaluation du modèle de sentiment multilingue sur un dataset FR/EN.

Usage :
    python evaluate.py --max_per_lang 500
    python evaluate.py --model_name 20260825T153054Z
"""

import argparse
import logging
import os

import matplotlib
matplotlib.use("Agg")  # backend non interactif : fonctionne même sans affichage graphique
import matplotlib.pyplot as plt
import numpy as np
import torch
from sklearn.metrics import ConfusionMatrixDisplay, confusion_matrix
from tqdm import tqdm

from src.dataset.loader import load_raw_dataset
from src.dataset.preprocess import tokenize_dataset
from src.utils.config import load_config
from src.utils.metrics import compute_metrics
from transformers import AutoTokenizer, AutoModelForSequenceClassification, DataCollatorWithPadding

logger = logging.getLogger(__name__)


# Ordre des classes, aligné sur les labels 0, 1, 2 du dataset.
LABEL_NAMES = ["negative", "neutral", "positive"]
LANGUAGES = ["fr", "en"]
LANG_DISPLAY_NAMES = {"fr": "Français (FR)", "en": "Anglais (EN)"}
OUTPUT_DIR = "outputs"


def evaluate(model, tokenizer, dataset, batch_size=16):
    """
    Évalue le modèle sur un dataset HuggingFace tokenisé.

    Retourne un dict contenant :
        accuracy / f1_macro   : métriques globales (pour rétro-compatibilité)
        metrics               : dict complet des métriques globales
        preds / labels        : arrays numpy des prédictions et labels réels
        langs                 : liste des codes de langue (ou None si indisponible)
    """
    preds, labels = [], []

    # La colonne 'lang_code' est une colonne texte : on la récupère AVANT de
    # passer le dataset en format torch (qui ne convertit que des tensors).
    has_lang = "lang_code" in dataset.column_names
    langs = list(dataset["lang_code"]) if has_lang else None

    if "label" in dataset.column_names and "labels" not in dataset.column_names:
        dataset = dataset.rename_column("label", "labels")

    label_key = "labels" if "labels" in dataset.column_names else "label"
    dataset.set_format(
        type="torch",
        columns=["input_ids", "attention_mask", label_key]
    )

    # Convertit les colonnes HF en tensors PyTorch et padde chaque batch
    # pour éviter les erreurs de stack sur des séquences de longueurs différentes.
    collator = DataCollatorWithPadding(tokenizer=tokenizer)
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
        collate_fn=collator,
    )

    model.eval()
    with torch.no_grad():
        for batch in tqdm(loader, desc="Evaluation"):
            input_ids = batch["input_ids"]
            attention_mask = batch["attention_mask"]
            label = batch.get("labels", batch.get(label_key))

            logits = model(
                input_ids=input_ids,
                attention_mask=attention_mask,
            ).logits

            pred = logits.argmax(dim=-1)

            preds.extend(pred.cpu().numpy())
            labels.extend(label.cpu().numpy())

    metrics = compute_metrics(preds, labels)

    return {
        "accuracy": metrics["accuracy"],
        "f1_macro": metrics["f1_macro"],
        "metrics": metrics,
        "preds": np.asarray(preds, dtype=int),
        "labels": np.asarray(labels, dtype=int),
        "langs": langs,
    }


def segment_by_language(results):
    """
    Découpe les prédictions / labels réels par langue à partir de la colonne
    lang_code présente dans `results["langs"]`.

    Retourne un dict {lang_code: {"preds": ..., "labels": ...}}.
    """
    langs = results.get("langs")
    if not langs:
        return {}

    preds = results["preds"]
    labels = results["labels"]
    langs_arr = np.asarray(langs)

    per_lang = {}
    for lang in LANGUAGES:
        mask = langs_arr == lang
        if mask.sum() == 0:
            continue
        per_lang[lang] = {
            "preds": preds[mask],
            "labels": labels[mask],
        }
    return per_lang


def _print_confusion_matrix(labels, preds, title):
    """Affiche une matrice de confusion numérique (lignes = vrai, colonnes = prédit)."""
    cm = confusion_matrix(labels, preds, labels=list(range(len(LABEL_NAMES))))
    counts = cm.tolist()

    width = max(8, max(len(name) for name in LABEL_NAMES))
    header = " " * width + "  " + "  ".join(f"{name:>{width}}" for name in LABEL_NAMES)
    logger.info(f"\n{title}")
    logger.info(header)
    for name, row in zip(LABEL_NAMES, counts):
        logger.info(f"{name:>{width}}  " + "  ".join(f"{v:>{width}}" for v in row))
    return cm


def plot_confusion_matrices(per_lang):
    """
    Génère et sauvegarde une figure regroupant une matrice de confusion par
    langue (FR et EN) via sklearn.metrics.ConfusionMatrixDisplay.
    """
    langs = [lang for lang in LANGUAGES if lang in per_lang]
    if not langs:
        return

    fig, axes = plt.subplots(1, len(langs), figsize=(6 * len(langs), 5.2), squeeze=False)
    for ax, lang in zip(axes[0], langs):
        entry = per_lang[lang]
        cm = confusion_matrix(
            entry["labels"],
            entry["preds"],
            labels=list(range(len(LABEL_NAMES))),
        )
        disp = ConfusionMatrixDisplay(confusion_matrix=cm, display_labels=LABEL_NAMES)
        disp.plot(ax=ax, cmap="Blues", colorbar=False, values_format="d")
        ax.set_title(
            f"{LANG_DISPLAY_NAMES.get(lang, lang)}\n(n={entry['labels'].size})"
        )

    fig.suptitle("Matrices de confusion par langue")
    fig.tight_layout(rect=[0, 0, 1, 0.95])

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    save_path = os.path.join(OUTPUT_DIR, "confusion_matrices_by_lang.png")
    fig.savefig(save_path, bbox_inches="tight")
    plt.close(fig)
    logger.info(f"\n[Confusion] Figure sauvegardée : {save_path}")

    # Affichage interactif si un backend graphique est disponible (sinon ignoré).
    try:
        plt.show()
    except Exception:
        pass


def report_by_language(results):
    """
    Affiche les métriques segmentées par langue ainsi que les matrices de
    confusion distinctes pour FR et EN.
    """
    per_lang = segment_by_language(results)

    if not per_lang:
        logger.warning("\nColonne 'lang_code' absente : pas de segmentation par langue possible.")
        return

    for lang in LANGUAGES:
        if lang not in per_lang:
            continue
        entry = per_lang[lang]
        metrics = compute_metrics(entry["preds"], entry["labels"])
        display = LANG_DISPLAY_NAMES.get(lang, lang)
        logger.info(f"\n=== Résultats {display} (n={entry['labels'].size}) ===")
        logger.info(f"Accuracy : {metrics['accuracy']:.4f}")
        logger.info(f"F1 macro : {metrics['f1_macro']:.4f}")
        _print_confusion_matrix(
            entry["labels"], entry["preds"],
            f"Matrice de confusion — {display}",
        )

    plot_confusion_matrices(per_lang)


def main(args):
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")
    # On réutilise le max_length de la config d'entraînement pour évaluer
    # avec exactement la même troncature qu'à l'entraînement : sinon le
    # modèle voit des séquences plus longues/complètes que celles vues
    # pendant l'entraînement, ce qui fausse les métriques.
    cfg = load_config("configs/default.yaml")
    max_length = args.max_length or cfg["max_length"]

    # Import paresseux pour éviter l'import circulaire : calibrate importe
    # evaluate (pour OUTPUT_DIR) au chargement du module.
    from calibrate import select_model_path

    # Même sélection de modèle que calibrate.py : version explicite
    # (--model_name) ou dernière version valide de experiments/models.
    # évite le décalage historique avec l'ancien MODEL_PATH codé en dur.
    model_path, model_label = select_model_path(args.model_name)

    logger.info(f"1. Chargement du dataset FR/EN (max {args.max_per_lang}/langue)...")
    raw = load_raw_dataset(max_per_lang=args.max_per_lang)

    logger.info(f"2. Chargement du tokenizer et du modèle ({model_label})...")
    tokenizer = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForSequenceClassification.from_pretrained(model_path)

    logger.info(f"3. Tokenisation du dataset (max_length={max_length})...")
    tokenized = tokenize_dataset(raw, tokenizer, max_length=max_length)

    logger.info("4. Évaluation...")
    results = evaluate(model, tokenizer, tokenized, batch_size=args.batch_size)

    logger.info("\n=== Résultats globaux ===")
    logger.info(f"Accuracy : {results['accuracy']:.4f}")
    logger.info(f"F1 macro : {results['f1_macro']:.4f}")

    # Métriques segmentées par langue + matrices de confusion FR / EN.
    report_by_language(results)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_per_lang", type=int, default=500)
    parser.add_argument("--batch_size", type=int, default=16)
    parser.add_argument("--max_length", type=int, default=None,
                         help="Par défaut, réutilise max_length de configs/default.yaml")
    parser.add_argument("--model_name", type=str, default=None,
                         help="Version à évaluer dans experiments/models "
                              "(ex. 20260825T153054Z). Par défaut : la dernière "
                              "version valide.")
    args = parser.parse_args()

    main(args)
