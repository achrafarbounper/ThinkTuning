"""
Tokenisation et création des DataLoaders pour l'entraînement.
"""

from torch.utils.data import DataLoader
from transformers import AutoTokenizer


def tokenize_dataset(dataset, tokenizer, max_length=128):
    if isinstance(tokenizer, str):
        tokenizer = AutoTokenizer.from_pretrained(tokenizer)

    def tokenize(batch):
        return tokenizer(
            batch["text"],
            truncation=True,
            padding="max_length",
            max_length=max_length,
        )
    return dataset.map(tokenize, batched=True)


def create_dataloaders(dataset, cfg):
    """
    Crée les DataLoaders d'entraînement et de validation.
    """
    dataset = tokenize_dataset(dataset, cfg["model_name"], max_length=cfg.get("max_length", 128))

    dataset = dataset.train_test_split(test_size=0.1, seed=42)
    train_ds = dataset["train"].with_format("torch", columns=["input_ids", "attention_mask", "label"])
    val_ds = dataset["test"].with_format("torch", columns=["input_ids", "attention_mask", "label"])

    train_loader = DataLoader(
        train_ds,
        batch_size=cfg["batch_size"],
        shuffle=True,
        num_workers=cfg.get("num_workers", 0),
        pin_memory=cfg.get("device", "cpu") == "cuda",
    )

    val_loader = DataLoader(
        val_ds,
        batch_size=cfg["batch_size"],
        shuffle=False,
        num_workers=cfg.get("num_workers", 0),
        pin_memory=cfg.get("device", "cpu") == "cuda",
    )

    return train_loader, val_loader
