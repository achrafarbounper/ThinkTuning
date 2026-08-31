import os
import logging

import torch
from torch import nn
from torch.optim import AdamW
from transformers import get_scheduler
from tqdm import tqdm

from src.utils.metrics import compute_metrics

import numpy as np

logger = logging.getLogger(__name__)


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
    logger.debug(
        f"compute_class_weights : début | {len(labels)} échantillons, {num_classes} classes"
    )
    counts = np.bincount(labels, minlength=num_classes)
    counts = np.maximum(counts, 1)  # évite division par zéro si une classe est absente

    total = counts.sum()
    weights = total / (num_classes * counts)

    result = torch.tensor(weights, dtype=torch.float32)
    logger.debug(f"compute_class_weights : terminé -> shape={tuple(result.shape)}")
    return result


class TrainingCancelledError(RuntimeError):
    pass


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

    def train(self, train_loader, val_loader, cancel_event=None):
        logger.debug(
            f"Trainer.train : début | epochs={self.cfg['epochs']}, device={self.device}"
        )
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

        self.epoch_metrics = []
        self.final_metrics = {}
        self.training_duration_seconds = None
        self.train_examples = len(train_loader.dataset) if hasattr(train_loader, "dataset") else None
        self.val_examples = len(val_loader.dataset) if hasattr(val_loader, "dataset") else None

        start_time = __import__("time").time()

        # Paramètres d'early stopping (désactivé si patience <= 0)
        patience = int(self.cfg.get("early_stopping_patience", 0))
        min_delta = float(self.cfg.get("early_stopping_min_delta", 0.0))

        # Suivi du meilleur checkpoint : on garde un cliché des poids pour le
        # restaurer en fin d'entraînement (équivalent exact du checkpoint sauvé
        # par self.save dans experiments/checkpoints/best_model.pt).
        best_f1 = -1.0
        best_epoch = None
        best_epoch_record = None
        best_state = None
        epochs_without_improvement = 0
        early_stopped = False

        for epoch in range(self.cfg["epochs"]):
            if cancel_event is not None and cancel_event.is_set():
                raise TrainingCancelledError("Training cancelled by user")
            logger.info(f"\n=== Epoch {epoch+1}/{self.cfg['epochs']} ===")
            self._train_epoch(train_loader, cancel_event=cancel_event)
            if cancel_event is not None and cancel_event.is_set():
                raise TrainingCancelledError("Training cancelled by user")
            metrics = self._eval_epoch(val_loader, cancel_event=cancel_event)

            if cancel_event is not None and cancel_event.is_set():
                raise TrainingCancelledError("Training cancelled by user")

            epoch_record = {
                "epoch": epoch + 1,
                "accuracy": float(metrics["accuracy"]),
                "f1_macro": float(metrics["f1_macro"]),
            }
            self.epoch_metrics.append(epoch_record)

            f1 = metrics["f1_macro"]
            if f1 > best_f1 + min_delta:
                best_f1 = f1
                best_epoch = epoch + 1
                best_epoch_record = epoch_record.copy()
                epochs_without_improvement = 0
                best_state = {
                    k: v.detach().clone() for k, v in self.model.state_dict().items()
                }
                self.save("experiments/checkpoints/best_model.pt")
                logger.info(f"✔ Nouveau meilleur modèle (F1={f1:.4f}) sauvegardé.")
            else:
                epochs_without_improvement += 1

            # Early stopping : on arrête si le F1 de validation n'a pas progressé
            # pendant `patience` epochs consécutives (patience configurable).
            if patience > 0 and epochs_without_improvement >= patience:
                logger.info(
                    f"⏹ Early stopping : F1 non amélioré pendant {patience} epochs "
                    f"consécutives — arrêt à l'epoch {epoch + 1} "
                    f"(meilleur F1={best_f1:.4f} à l'epoch {best_epoch})."
                )
                early_stopped = True
                break

        self.training_duration_seconds = __import__("time").time() - start_time

        # Restaurer le meilleur checkpoint à la fin de l'entraînement :
        # le modèle en mémoire doit correspondre au meilleur modèle sauvé sur
        # disque (même si la dernière epoch est moins bonne, ou si l'entraînement
        # a été arrêté tôt par l'early stopping).
        if best_state is not None:
            self.model.load_state_dict(best_state)
            self.final_metrics = best_epoch_record
            logger.info(
                f"↩ Meilleur checkpoint restauré (epoch {best_epoch}, F1={best_f1:.4f})."
            )
        else:
            self.final_metrics = (
                self.epoch_metrics[-1].copy() if self.epoch_metrics else {}
            )

        # État d'early stopping exposé pour le rapport (API / dashboard)
        self.early_stopped = early_stopped
        self.best_epoch = best_epoch
        self.best_f1 = float(best_f1)

        logger.debug(
            f"Trainer.train : terminé | {len(self.epoch_metrics)} epoch(s), "
            f"early_stopped={early_stopped}, durée={self.training_duration_seconds}s"
        )

        return {
            "epoch_metrics": self.epoch_metrics,
            "final_metrics": self.final_metrics,
            "training_duration_seconds": self.training_duration_seconds,
            "early_stopped": early_stopped,
        }

    def _train_epoch(self, loader, cancel_event=None):
        logger.debug(f"_train_epoch : début | {len(loader)} batch(s)")
        self.model.train()
        self.optimizer.zero_grad(set_to_none=True)

        for step, batch in enumerate(tqdm(loader, desc="Train")):
            if cancel_event is not None and cancel_event.is_set():
                raise TrainingCancelledError("Training cancelled by user")
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
                # On NE passe PAS labels au modèle : sinon HF calcule en
                # interne une CrossEntropyLoss non pondérée dans outputs.loss,
                # et self.criterion (qui porte les poids de classe) n'est
                # jamais réellement utilisé pour l'optimisation.
                outputs = self.model(
                    input_ids=input_ids,
                    attention_mask=attention_mask,
                )
                loss = self.criterion(outputs.logits, labels) / self.grad_accum_steps

            loss.backward()

            is_last_batch = (step + 1) == len(loader)
            if (step + 1) % self.grad_accum_steps == 0 or is_last_batch:
                if cancel_event is not None and cancel_event.is_set():
                    raise TrainingCancelledError("Training cancelled by user")
                torch.nn.utils.clip_grad_norm_(
                    self.model.parameters(),
                    max_norm=self.cfg.get("gradient_clip", 1.0),
                )
                self.optimizer.step()
                self.scheduler.step()
                self.optimizer.zero_grad(set_to_none=True)

        logger.debug("_train_epoch : terminé")

    def _eval_epoch(self, loader, cancel_event=None):
        logger.debug(f"_eval_epoch : début | {len(loader)} batch(s)")
        self.model.eval()
        preds, labels = [], []

        with torch.no_grad():
            for batch in tqdm(loader, desc="Eval"):
                if cancel_event is not None and cancel_event.is_set():
                    raise TrainingCancelledError("Training cancelled by user")
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
        logger.info(f"Eval — Acc={metrics['accuracy']:.4f}  F1={metrics['f1_macro']:.4f}")
        logger.debug(f"_eval_epoch : terminé -> {len(loader)} batch(s) évalués")
        return metrics

    def save(self, path):
        os.makedirs(path, exist_ok=True)
        if hasattr(self.model, "save_pretrained"):
            self.model.save_pretrained(path)
            return

        state_dict_path = os.path.join(path, "model_state_dict.pt")
        torch.save(self.model.state_dict(), state_dict_path)