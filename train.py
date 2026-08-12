import argparse
import os

import torch
from transformers import AutoTokenizer

from src.dataset.loader import load_raw_dataset, augment_dataset
from src.dataset.preprocess import create_dataloaders
from src.model.distilbert import build_model
from src.model.trainer import Trainer
from src.utils.config import load_config


def main(args):
    cfg = load_config("configs/default.yaml")

    if args.max_length is not None:
        cfg["max_length"] = args.max_length
    if args.learning_rate is not None:
        cfg["learning_rate"] = args.learning_rate
    if args.weight_decay is not None:
        cfg["weight_decay"] = args.weight_decay
    if args.warmup_ratio is not None:
        cfg["warmup_ratio"] = args.warmup_ratio
    if args.epochs is not None:
        cfg["epochs"] = args.epochs
    if args.batch_size is not None:
        cfg["batch_size"] = args.batch_size
    if args.num_workers is not None:
        cfg["num_workers"] = args.num_workers

    if args.device == "auto":
        cfg["device"] = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        cfg["device"] = args.device

    print(f"1. Chargement du dataset multilingue (max {args.max_per_lang}/langue)...")
    raw = load_raw_dataset(max_per_lang=args.max_per_lang)

    print(f"2. Recomposition (augmentation) — fraction={args.augment_fraction}...")
    augmented = augment_dataset(
        raw,
        variants_per_example=args.variants_per_example,
        augment_fraction=args.augment_fraction,
    )
    print(f"   -> {len(raw)} exemples originaux -> {len(augmented)} après recomposition")

    print("3. Création des DataLoaders...")
    train_loader, val_loader = create_dataloaders(augmented, cfg)

    print(f"4. Chargement du modèle {cfg['model_name']} sur {cfg['device']}...")
    tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
    model = build_model(cfg)

    print("5. Entraînement...")
    trainer = Trainer(model, cfg)
    trainer.train(train_loader, val_loader)

    print("6. Sauvegarde du modèle final...")
    tokenizer.save_pretrained("sentiment_model_final")
    trainer.save("sentiment_model_final")

    print("\nTerminé ! Modèle sauvegardé dans ./sentiment_model_final")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_per_lang", type=int, default=500)
    parser.add_argument("--augment_fraction", type=float, default=0.4)
    parser.add_argument("--variants_per_example", type=int, default=2)
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch_size", type=int, default=None)
    parser.add_argument("--num_workers", type=int, default=None)
    parser.add_argument("--max_length", type=int, default=None)
    parser.add_argument("--learning_rate", type=float, default=None)
    parser.add_argument("--weight_decay", type=float, default=None)
    parser.add_argument("--warmup_ratio", type=float, default=None)
    parser.add_argument("--device", type=str, default="auto")
    args = parser.parse_args()

    if args.num_workers is None:
        args.num_workers = max(0, min(2, max(1, (os.cpu_count() or 1) // 2)))
        print(f"Auto num_workers={args.num_workers} based on CPU cores")

    main(args)
