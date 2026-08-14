"""
Tokenisation et création des DataLoaders pour l'entraînement.
"""


from torch.utils.data import DataLoader
from transformers import AutoTokenizer, DataCollatorWithPadding


def tokenize_dataset(dataset, tokenizer, max_length=128):
    if isinstance(tokenizer, str):
        tokenizer = AutoTokenizer.from_pretrained(tokenizer)

    def tokenize(batch):
        return tokenizer(batch["text"], truncation=True, max_length=max_length)  # pas de padding ici

    return dataset.map(tokenize, batched=True, num_proc=2)  # (b) tokenisation parallèle


def create_dataloaders(train_ds, val_ds, cfg):
    """
    Prend deux datasets DÉJÀ splittés (non augmentés pour val_ds)
    et retourne les DataLoaders.
    """
    train_ds = tokenize_dataset(train_ds, cfg["model_name"], max_length=cfg.get("max_length", 128))
    val_ds = tokenize_dataset(val_ds, cfg["model_name"], max_length=cfg.get("max_length", 128))

    train_ds = train_ds.with_format("torch", columns=["input_ids", "attention_mask", "label"])
    val_ds = val_ds.with_format("torch", columns=["input_ids", "attention_mask", "label"])

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