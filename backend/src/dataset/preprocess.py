"""
Tokenisation et création des DataLoaders pour l'entraînement.
"""

from torch.utils.data import DataLoader
from transformers import AutoTokenizer, DataCollatorWithPadding
from datasets import Dataset
import torch


def tokenize_dataset(dataset, tokenizer, max_length=128):
    # --- Correction pour les tests E2E : Dataset peut être un objet HF ou dict-like ---
    if not hasattr(dataset, "column_names"):
        # Convertir en Dataset Hugging Face avec les colonnes attendues
        dataset = Dataset.from_dict({
            "text": getattr(dataset, 'text', []),
            "labels": getattr(dataset, 'label', []),      # conversion correcte
            "lang_code": getattr(dataset, 'lang_code', []),
        })

    if isinstance(tokenizer, str):
        tokenizer = AutoTokenizer.from_pretrained(tokenizer)

    # Renommage systématique pour HF
    if "label" in dataset.column_names and "labels" not in dataset.column_names:
        dataset = dataset.rename_column("label", "labels")

    def tokenize(batch):
        tokenized = tokenizer(batch["text"], truncation=True, max_length=max_length)
        if "labels" in batch:
            tokenized["labels"] = batch["labels"]
        return tokenized

    return dataset.map(tokenize, batched=True, num_proc=None)


def create_dataloaders(train_ds, val_ds, cfg):
    # S'assurer que les datasets sont bien formatés avant tokenisation
    train_ds = tokenize_dataset(train_ds, cfg["model_name"], max_length=cfg.get("max_length", 128))
    val_ds = tokenize_dataset(val_ds, cfg["model_name"], max_length=cfg.get("max_length", 128))

    # Renommage systématique pour HF (après tokenisation)
    if "label" in train_ds.column_names and "labels" not in train_ds.column_names:
        train_ds = train_ds.rename_column("label", "labels")
    if "label" in val_ds.column_names and "labels" not in val_ds.column_names:
        val_ds = val_ds.rename_column("label", "labels")

    # Vérifier que la colonne 'labels' existe avant de formater
    if "labels" not in train_ds.column_names:
        raise ValueError(f"Dataset train n'a pas la colonne 'labels'. Colonnes disponibles: {train_ds.column_names}")
    if "labels" not in val_ds.column_names:
        raise ValueError(f"Dataset val n'a pas la colonne 'labels'. Colonnes disponibles: {val_ds.column_names}")

    # IMPORTANT: Inclure 'labels' dans les colonnes à convertir en torch tensor
    # Cela garantit que le collator recevra bien cette clé dans le batch dictionary
    train_ds = train_ds.with_format("torch", columns=["input_ids", "attention_mask", "labels"])
    val_ds = val_ds.with_format("torch", columns=["input_ids", "attention_mask", "labels"])

    # Initialiser le tokenizer AVANT de définir la fonction collation
    tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])

    # Vérifier que les datasets ont bien la colonne 'labels' après formatage
    if "labels" not in train_ds.column_names:
        raise ValueError(f"Dataset train n'a pas la colonne 'labels' après with_format. Colonnes: {train_ds.column_names}")
    if "labels" not in val_ds.column_names:
        raise ValueError(f"Dataset val n'a pas la colonne 'labels' après with_format. Colonnes: {val_ds.column_names}")

    # Custom collation function that explicitly includes labels.
    # NOTE: with recent transformers versions, DataCollatorWithPadding only
    # pads the tokenizer-recognised sequence fields (input_ids, attention_mask,
    # ...) and drops extra columns such as `labels`. We therefore re-attach the
    # labels from the raw per-sample dicts after padding.
    def custom_collate(batch):
        result = DataCollatorWithPadding(tokenizer=tokenizer)(batch)
        if "labels" not in result:
            result["labels"] = torch.tensor(
                [item["labels"] for item in batch], dtype=torch.long
            )
        return result
    
    collator = custom_collate

    train_loader = DataLoader(
        train_ds, batch_size=cfg["batch_size"], shuffle=True,
        num_workers=cfg.get("num_workers", 0),
        collate_fn=collator,
    )
    val_loader = DataLoader(
        val_ds, batch_size=cfg["batch_size"], shuffle=False,
        num_workers=cfg.get("num_workers", 0),
        collate_fn=collator,
    )
    return train_loader, val_loader
