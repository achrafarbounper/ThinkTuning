import argparse
import json
import os

import torch
from transformers import AutoTokenizer

from src.dataset.loader import load_raw_dataset, load_local_dataset, augment_dataset
from src.dataset.preprocess import create_dataloaders
from src.model.distilbert import build_model
from src.model.trainer import Trainer, compute_class_weights
from src.utils.config import load_config
from api import TEST_MODE


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
    if args.early_stopping_patience is not None:
        cfg["early_stopping_patience"] = args.early_stopping_patience
    if args.early_stopping_min_delta is not None:
        cfg["early_stopping_min_delta"] = args.early_stopping_min_delta
    if args.class_augment_weights is not None:
        cfg["class_augment_weights"] = args.class_augment_weights

    if args.device == "auto":
        cfg["device"] = "cuda" if torch.cuda.is_available() else "cpu"
    else:
        cfg["device"] = args.device

    # 3) Override en mode test
    if TEST_MODE:
        cfg["device"] = "cpu"

    if args.dataset_file:
        print(f"1. Chargement du dataset local : {args.dataset_file}...")
        raw = load_local_dataset(args.dataset_file)
    else:
        print(f"1. Chargement du dataset multilingue (max {args.max_per_lang}/langue)...")
        raw = load_raw_dataset(max_per_lang=args.max_per_lang)

    print("2. Split train/val (avant augmentation, pour éviter la fuite)...")
    split = raw.train_test_split(test_size=0.1, seed=42)
    raw_train, raw_val = split["train"], split["test"]

    print(f"3. Recomposition (augmentation) sur le train uniquement — fraction={args.augment_fraction}...")
    augmented_train = augment_dataset(
        raw_train,
        variants_per_example=args.variants_per_example,
        augment_fraction=args.augment_fraction,
        class_augment_weights=cfg.get("class_augment_weights"),
    )
    print(f"   -> {len(raw_train)} exemples originaux -> {len(augmented_train)} après recomposition")

    print("4. Création des DataLoaders...")
    train_loader, val_loader = create_dataloaders(augmented_train, raw_val, cfg)

    print("5. Calcul des poids des classes...")
    class_weights = compute_class_weights(augmented_train['label'])

    print(f"6. Chargement du modèle {cfg['model_name']} sur {cfg['device']}...")
    tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
    model = build_model(cfg)

    print("7. Entraînement...")
    trainer = Trainer(model, cfg, class_weights=class_weights)
    trainer.train(train_loader, val_loader)

    print("7. Sauvegarde du modèle final...")
    tokenizer.save_pretrained("sentiment_model_final")
    trainer.save("sentiment_model_final")

    print("\nTerminé ! Modèle sauvegardé dans ./sentiment_model_final")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--max_per_lang", type=int, default=500)
    parser.add_argument("--dataset_file", type=str, default=None,
                        help="Chemin vers un CSV/JSONL local (colonnes text/label/lang_code, "
                             "ex: sortie enrichie de merge_reviewed_data.py). "
                             "Défaut : dataset Hugging Face.")
    parser.add_argument("--augment_fraction", type=float, default=0.4)
    parser.add_argument("--variants_per_example", type=int, default=2)
    parser.add_argument("--class_augment_weights", type=json.loads, default=None,
                        help="JSON dict {label: poids} pour sur-échantillonner préférentiellement "
                             "certaines classes à l'augmentation (ex: '{\"1\": 3.0}'). "
                             "Défaut : surpoids sur la classe neutral défini dans la config.")
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--batch_size", type=int, default=None)
    parser.add_argument("--num_workers", type=int, default=None)
    parser.add_argument("--early_stopping_patience", type=int, default=None,
                        help="Epochs consécutives sans amélioration du F1 de validation avant arrêt (0 = désactivé)")
    parser.add_argument("--early_stopping_min_delta", type=float, default=None,
                        help="Amélioration minimale de F1 requise pour reset la patience")
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
