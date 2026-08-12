"""
Évaluation du modèle de sentiment multilingue sur un dataset FR/EN.

Usage :
    python evaluate.py --max_per_lang 500
"""

import argparse
import torch
from tqdm import tqdm

from src.dataset.loader import load_raw_dataset
from src.dataset.preprocess import tokenize_dataset
from src.utils.metrics import compute_metrics
from transformers import AutoTokenizer, AutoModelForSequenceClassification


MODEL_PATH = "./sentiment_model_final"


def evaluate(model, tokenizer, dataset, batch_size=16):
    """
    Évalue le modèle sur un dataset HuggingFace tokenisé.
    """
    preds, labels = [], []
    
    # Convertit les colonnes HF en tensors PyTorch
    dataset.set_format(
        type="torch",
        columns=["input_ids", "attention_mask", "label"]
    )

    # Conversion HF → PyTorch DataLoader
    loader = torch.utils.data.DataLoader(
        dataset,
        batch_size=batch_size,
        shuffle=False,
    )

    model.eval()
    with torch.no_grad():
        for batch in tqdm(loader, desc="Evaluation"):
            input_ids = batch["input_ids"]
            attention_mask = batch["attention_mask"]
            label = batch["label"]

            logits = model(
                input_ids=input_ids,
                attention_mask=attention_mask,
            ).logits

            pred = logits.argmax(dim=-1)

            preds.extend(pred.cpu().numpy())
            labels.extend(label.cpu().numpy())

    return compute_metrics(preds, labels)


def main(args):
    print(f"1. Chargement du dataset FR/EN (max {args.max_per_lang}/langue)...")
    raw = load_raw_dataset(max_per_lang=args.max_per_lang)

    print("2. Chargement du tokenizer et du modèle...")
    tokenizer = AutoTokenizer.from_pretrained(MODEL_PATH)
    model = AutoModelForSequenceClassification.from_pretrained(MODEL_PATH)

    print("3. Tokenisation du dataset...")
    tokenized = tokenize_dataset(raw, tokenizer)

    print("4. Évaluation...")
    metrics = evaluate(model, tokenizer, tokenized, batch_size=args.batch_size)

    print("\n=== Résultats ===")
    print(f"Accuracy : {metrics['accuracy']:.4f}")
    print(f"F1 macro : {metrics['f1_macro']:.4f}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_per_lang", type=int, default=500)
    parser.add_argument("--batch_size", type=int, default=16)
    args = parser.parse_args()

    main(args)
