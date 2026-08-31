#!/usr/bin/env python
"""Pipeline end-to-end en une commande :

    labeling (label_dataset.py) → filtrage par confidence → fine-tuning LLM
    (finetune_llm.py)

Exemples :

    # CLI seule
    python pipeline.py --input data/unlabeled.csv --output_dir runs/lora_model \
        --min_confidence 0.8 --epochs 2

    # Avec un fichier de configuration YAML (CLI > YAML > défauts)
    python pipeline.py --input data/unlabeled.csv --output_dir runs/lora_model \
        --config pipeline.example.yaml
"""

import argparse
import logging
import os
import subprocess
import sys
import time

from core.models import PipelineRequest
from core.pipeline_runner import build_finetune_cmd, PROJECT_ROOT, run_labeling

logger = logging.getLogger(__name__)


def load_yaml_config(path: str) -> dict:
    """Charge un YAML de configuration (sections optionnelles labeling / finetune).

    Retourne un dict plat fusionné (clés de PipelineRequest).
    """
    try:
        import yaml
    except ImportError as exc:  # pragma: no cover
        raise SystemExit(
            "PyYAML est requis pour --config. Installez-le avec : pip install pyyaml"
        ) from exc

    if not os.path.exists(path):
        raise SystemExit(f"Fichier de configuration introuvable : {path}")

    with open(path, "r", encoding="utf-8-sig") as handle:
        payload = yaml.safe_load(handle) or {}

    if not isinstance(payload, dict):
        raise SystemExit(f"Configuration YAML invalide dans {path} : mapping attendu.")

    merged: dict = {}
    for section in ("labeling", "finetune"):
        values = payload.get(section)
        if values is None:
            continue
        if not isinstance(values, dict):
            raise SystemExit(
                f"Section '{section}' invalide dans {path} : mapping attendu."
            )
        merged.update(values)

    # Tolère aussi une config plate (clés PipelineRequest au premier niveau).
    for key, value in payload.items():
        if key not in ("labeling", "finetune"):
            merged.setdefault(key, value)
    return merged


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Pipeline end-to-end : label_dataset -> filtrage confidence -> finetune_llm.",
    )
    parser.add_argument("--input", required=True, help="CSV/JSON/JSONL/TXT contenant les textes à labeler.")
    parser.add_argument("--labeled_out", default=None, help="JSONL Alpaca intermédiaire (défaut : runs/pipeline_<ts>/labeled.jsonl).")
    parser.add_argument("--output_dir", required=True, help="Dossier de sortie du modèle LoRA fine-tuné.")
    parser.add_argument("--config", default=None, help="Fichier YAML de configuration (sections labeling/finetune). CLI > YAML > défauts.")

    # --- labeling ---
    parser.add_argument("--model_path", default=None, help="Version experiments/models pour le labeling (défaut : dernière).")
    parser.add_argument("--text_column", default=None, help="Colonne texte (défaut : text).")
    parser.add_argument("--min_confidence", type=float, default=None, help="Seuil de confidence (défaut : 0.7).")
    parser.add_argument("--label_batch_size", type=int, default=None, help="Batch DistilBERT (défaut : 32).")

    # --- fine-tuning ---
    parser.add_argument("--base_model", default=None, help="Modèle HF de base (défaut : TinyLlama-1.1B-Chat).")
    parser.add_argument("--validation_file", default=None, help="JSONL de validation optionnel.")
    parser.add_argument("--epochs", type=int, default=None)
    parser.add_argument("--finetune_batch_size", type=int, default=None, help="Batch d'entraînement (défaut : 2).")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=None)
    parser.add_argument("--learning_rate", type=float, default=None)
    parser.add_argument("--max_seq_length", type=int, default=None)
    parser.add_argument("--lora_r", type=int, default=None)
    parser.add_argument("--lora_alpha", type=int, default=None)
    parser.add_argument("--lora_dropout", type=float, default=None)
    parser.add_argument("--target_modules", default=None)
    parser.add_argument("--no_qlora", action="store_true", help="Désactiver la quantification 4-bit.")
    parser.add_argument("--seed", type=int, default=None)
    return parser


def merge_params(args: argparse.Namespace) -> PipelineRequest:
    """Fusionne YAML puis CLI (les valeurs CLI non renseignées laissent la
    place au YAML, lui-même au-dessus des défauts)."""
    cli_values = {k: v for k, v in vars(args).items() if k != "config"}
    yaml_values = load_yaml_config(args.config) if args.config else {}

    merged = dict(yaml_values)
    for key, value in cli_values.items():
        if value is not None:
            merged[key] = value

    # Alias CLI/YAML acceptés pour rester proche des CLIs d'origine.
    if "input" in merged:
        merged.setdefault("input_path", merged.pop("input"))
    if "labeled_out" in merged:
        merged.setdefault("labeled_output", merged.pop("labeled_out"))
    if "batch_size" in merged and "label_batch_size" not in merged:
        merged["label_batch_size"] = merged.pop("batch_size")
    # --no_qlora (flag CLI) ne force le mode LoRA standard que s'il est actif ;
    # sinon la valeur use_qlora du YAML reste valable.
    if merged.pop("no_qlora", False):
        merged["use_qlora"] = False

    merged.setdefault("use_qlora", True)
    merged.setdefault("text_column", "text")
    merged.setdefault("min_confidence", 0.7)
    merged.setdefault("label_batch_size", 32)
    merged.setdefault("seed", 42)

    input_path = merged.get("input_path") or args.input
    output_dir = merged.get("output_dir") or args.output_dir
    if not input_path or not output_dir:
        raise SystemExit("--input et --output_dir sont requis (CLI ou YAML).")
    merged["input_path"] = input_path
    merged["output_dir"] = output_dir

    unknown = set(merged) - set(PipelineRequest.model_fields)
    if unknown:
        raise SystemExit(f"Clés de configuration inconnues : {sorted(unknown)}")

    return PipelineRequest(**merged)


def main() -> int:
    # Même convention que api/main.py : on branche le handler console coloré
    # (rich, cf. ia/logging_setup.py) pour afficher les logs du pipeline.
    _ia_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ia")
    if _ia_dir not in sys.path:
        sys.path.insert(0, _ia_dir)
    from logging_setup import setup_logging
    setup_logging()

    args = build_parser().parse_args()
    params = merge_params(args)

    timestamp = time.strftime("%Y%m%dT%H%M%SZ")
    labeled_out = params.labeled_output or os.path.join("runs", f"pipeline_{timestamp}", "labeled.jsonl")

    logger.info(
        "Étape 1/3 — Labeling + filtrage par confidence (min_confidence=%s)",
        params.min_confidence,
    )
    started = time.time()
    records = run_labeling(params, labeled_out)
    logger.info(
        "%d record(s) conservé(s) -> %s (%.1fs)",
        len(records), labeled_out, time.time() - started,
    )

    if not records:
        logger.error(
            "Aucun record au-dessus du seuil de confidence. "
            "Le fine-tuning n'est pas lancé. Baissez --min_confidence "
            "ou vérifiez l'entrée."
        )
        return 1

    logger.info("Étape 2/3 — Fine-tuning LLM (subprocess finetune_llm.py)")
    cmd = build_finetune_cmd(params, labeled_out, params.output_dir)
    logger.info("Commande : %s", " ".join(cmd))
    result = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if result.returncode != 0:
        logger.error(
            "finetune_llm.py a échoué (code %s).", result.returncode
        )
        return result.returncode or 1

    logger.info("Terminé.")
    logger.info("Dataset labelé : %s", labeled_out)
    logger.info("Modèle LoRA    : %s", params.output_dir)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        logger.warning("Interrompu par l'utilisateur.")
        sys.exit(130)
