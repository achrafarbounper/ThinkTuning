import os

import torch
from torch import nn
from torch.optim import AdamW
from transformers import get_scheduler
from tqdm import tqdm

from src.utils.metrics import compute_metrics


class Trainer:
    def __init__(self, model, cfg):
        self.device = cfg.get("device", "cpu")
        self.model = model.to(self.device)
        self.cfg = cfg

        self.optimizer = AdamW(
            self.model.parameters(),
            lr=cfg["learning_rate"],
            weight_decay=cfg["weight_decay"],
        )

        self.scheduler = None
        self.criterion = nn.CrossEntropyLoss()

    def train(self, train_loader, val_loader):
        total_steps = len(train_loader) * self.cfg["epochs"]
        warmup_steps = int(total_steps * self.cfg["warmup_ratio"])

        self.scheduler = get_scheduler(
            name="linear",
            optimizer=self.optimizer,
            num_warmup_steps=warmup_steps,
            num_training_steps=total_steps,
        )

        best_f1 = 0.0

        for epoch in range(self.cfg["epochs"]):
            print(f"\n=== Epoch {epoch+1}/{self.cfg['epochs']} ===")
            self._train_epoch(train_loader)
            f1 = self._eval_epoch(val_loader)

            if f1 > best_f1:
                best_f1 = f1
                self.save("experiments/checkpoints/best_model.pt")
                print(f"✔ Nouveau meilleur modèle (F1={f1:.4f}) sauvegardé.")

    def _train_epoch(self, loader):
        self.model.train()
        for batch in tqdm(loader, desc="Train"):
            batch = {
                k: (v.to(self.device) if isinstance(v, torch.Tensor) else v)
                for k, v in batch.items()
            }
            input_ids = batch["input_ids"]
            attention_mask = batch["attention_mask"]
            labels = batch["label"]

            outputs = self.model(
                input_ids=input_ids,
                attention_mask=attention_mask,
                labels=labels,
            )

            loss = outputs.loss
            loss.backward()

            torch.nn.utils.clip_grad_norm_(self.model.parameters(), max_norm=self.cfg.get("gradient_clip", 1.0))
            self.optimizer.step()
            self.scheduler.step()
            self.optimizer.zero_grad(set_to_none=True)

    def _eval_epoch(self, loader):
        self.model.eval()
        preds, labels = [], []

        with torch.no_grad():
            for batch in tqdm(loader, desc="Eval"):
                batch = {
                    k: (v.to(self.device) if isinstance(v, torch.Tensor) else v)
                    for k, v in batch.items()
                }
                outputs = self.model(
                    input_ids=batch["input_ids"],
                    attention_mask=batch["attention_mask"],
                )
                logits = outputs.logits
                preds.extend(logits.argmax(dim=-1).cpu().numpy())
                labels.extend(batch["label"].cpu().numpy())

        metrics = compute_metrics(preds, labels)
        print(f"Eval — Acc={metrics['accuracy']:.4f}  F1={metrics['f1_macro']:.4f}")
        return metrics["f1_macro"]

    def save(self, path):
        os.makedirs(path, exist_ok=True)
        self.model.save_pretrained(path)
