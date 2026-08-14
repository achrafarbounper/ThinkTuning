import os

import torch
from torch import nn
from torch.optim import AdamW
from transformers import get_scheduler
from tqdm import tqdm

from src.utils.metrics import compute_metrics

import numpy as np


def compute_class_weights(labels, num_classes=3):
    """
    Calcule des poids de classe inversement proportionnels à leur fréquence,
    pour compenser un dataset déséquilibré (ex: peu d'exemples 'neutral').

    Args:
        labels: liste ou array d'entiers (0, 1, 2)
        num_classes: nombre total de classes

    Returns:
        torch.Tensor de shape (num_classes,) à passer à nn.CrossEntropyLoss(weight=...)
    """
    labels = np.asarray(labels)
    counts = np.bincount(labels, minlength=num_classes)
    counts = np.maximum(counts, 1)  # évite division par zéro si une classe est absente

    total = counts.sum()
    weights = total / (num_classes * counts)

    return torch.tensor(weights, dtype=torch.float32)

class Trainer:
    def __init__(self, model, cfg, class_weights=None):
        self.device = cfg.get("device", "cpu")
        self.model = model.to(self.device)
        self.cfg = cfg

        # --- Réglages CPU ---
        if self.device == "cpu":
            n_threads = cfg.get("torch_threads", os.cpu_count() or 4)
            torch.set_num_threads(n_threads)
            os.environ.setdefault("OMP_NUM_THREADS", str(n_threads))
            os.environ.setdefault("MKL_NUM_THREADS", str(n_threads))

        # bf16 autocast : uniquement pertinent sur CPU moderne (et activable via cfg)
        self.bf16 = bool(cfg.get("bf16", False)) and self.device == "cpu"

        # Accumulation de gradient (utile pour simuler un plus gros batch en CPU)
        self.grad_accum_steps = max(1, cfg.get("gradient_accumulation_steps", 1))

        self.optimizer = AdamW(
            self.model.parameters(),
            lr=cfg["learning_rate"],
            weight_decay=cfg["weight_decay"],
        )

        self.scheduler = None
        
        # Utiliser les poids de classe si fournis
        if class_weights is not None:
            class_weights = class_weights.to(self.device)
        self.criterion = nn.CrossEntropyLoss(weight=class_weights)

    def train(self, train_loader, val_loader):
        # Le nombre de "vrais" pas d'optimisation tient compte de l'accumulation
        steps_per_epoch = -(-len(train_loader) // self.grad_accum_steps)  # ceil division
        total_steps = steps_per_epoch * self.cfg["epochs"]
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
        self.optimizer.zero_grad(set_to_none=True)

        for step, batch in enumerate(tqdm(loader, desc="Train")):
            batch = {
                k: (v.to(self.device) if isinstance(v, torch.Tensor) else v)
                for k, v in batch.items()
            }
            input_ids = batch["input_ids"]
            attention_mask = batch["attention_mask"]
            labels = batch["labels"]

            with torch.autocast(
                device_type="cpu", dtype=torch.bfloat16, enabled=self.bf16
            ):
                outputs = self.model(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                    labels=labels,
                )
                loss = outputs.loss / self.grad_accum_steps

            loss.backward()

            is_last_batch = (step + 1) == len(loader)
            if (step + 1) % self.grad_accum_steps == 0 or is_last_batch:
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(),
                    max_norm=self.cfg.get("gradient_clip", 1.0),
                )
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
                with torch.autocast(
                    device_type="cpu", dtype=torch.bfloat16, enabled=self.bf16
                ):
                    outputs = self.model(
                        input_ids=batch["input_ids"],
                        attention_mask=batch["attention_mask"],
                    )
                logits = outputs.logits
                preds.extend(logits.argmax(dim=-1).cpu().numpy())
                labels.extend(batch["labels"].cpu().numpy())

        metrics = compute_metrics(preds, labels)
        print(f"Eval — Acc={metrics['accuracy']:.4f}  F1={metrics['f1_macro']:.4f}")
        return metrics["f1_macro"]

    def save(self, path):
        os.makedirs(path, exist_ok=True)
        self.model.save_pretrained(path)