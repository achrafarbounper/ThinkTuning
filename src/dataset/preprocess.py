"""
Tokenisation et création des DataLoaders pour l'entraînement.
"""


from torch.utils.data import DataLoader
from transformers import AutoTokenizer, DataCollatorWithPadding


def tokenize_dataset(dataset, tokenizer, max_length=128):
    if isinstance(tokenizer, str):
        tokenizer = AutoTokenizer.from_pretrained(tokenizer)

    if "label" in dataset.column_names and "labels" not in dataset.column_names:
        dataset = dataset.rename_column("label", "labels")

    def tokenize(batch):
        tokenized = tokenizer(batch["text"], truncation=True, max_length=max_length)
        if "labels" in batch:
            tokenized["labels"] = batch["labels"]
        return tokenized

    # Windows cannot safely spawn and then import the local Transformers package
    # in the worker process; running the map in-process avoids the deadlock.
    return dataset.map(tokenize, batched=True, num_proc=None)


def create_dataloaders(train_ds, val_ds, cfg):
    """
    Prend deux datasets DÉJÀ splittés (non augmentés pour val_ds)
    et retourne les DataLoaders.
    """
    train_ds = tokenize_dataset(train_ds, cfg["model_name"], max_length=cfg.get("max_length", 128))
    val_ds = tokenize_dataset(val_ds, cfg["model_name"], max_length=cfg.get("max_length", 128))

    label_col = "labels" if "labels" in train_ds.column_names else "label"
    train_ds = train_ds.with_format("torch", columns=["input_ids", "attention_mask", label_col])
    val_ds = val_ds.with_format("torch", columns=["input_ids", "attention_mask", label_col])

    tokenizer = AutoTokenizer.from_pretrained(cfg["model_name"])
    collator = DataCollatorWithPadding(tokenizer=tokenizer)

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