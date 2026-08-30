import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import torch
from datasets import Dataset
from transformers import (
    AutoModelForCausalLM,
    AutoTokenizer,
    DataCollatorForLanguageModeling,
    Trainer,
    TrainingArguments,
)

try:
    from peft import LoraConfig, get_peft_model, TaskType
except Exception as exc:  # pragma: no cover
    raise SystemExit(
        "PEFT is required. Install it with: pip install peft sentencepiece accelerate"
    ) from exc

try:
    from transformers import BitsAndBytesConfig
except Exception:  # pragma: no cover
    BitsAndBytesConfig = None


DEFAULT_TEMPLATE = """### Instruction:\n{instruction}\n\n### Input:\n{input_text}\n\n### Response:\n"""


def gpu_available() -> bool:
    return torch.cuda.is_available()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Fine-tuning LoRA / QLoRA d'un modèle causal LLM sur un dataset Alpaca JSONL."
    )
    parser.add_argument("--train_file", type=str, required=True, help="Chemin du fichier JSONL d'entraînement.")
    parser.add_argument("--output_dir", type=str, required=True, help="Dossier de sortie du modèle adapté.")
    parser.add_argument(
        "--base_model",
        type=str,
        default="TinyLlama/TinyLlama-1.1B-Chat-v1.0",
        help="Modèle de base Hugging Face à peaufiner.",
    )
    parser.add_argument("--validation_file", type=str, default=None, help="Optionnel: fichier JSONL de validation.")
    parser.add_argument("--max_seq_length", type=int, default=512, help="Longueur maximale du contexte.")
    parser.add_argument("--batch_size", type=int, default=2, help="Taille de batch par GPU/CPU.")
    parser.add_argument("--gradient_accumulation_steps", type=int, default=8, help="Accumulation de gradient.")
    parser.add_argument("--learning_rate", type=float, default=2e-4, help="Learning rate.")
    parser.add_argument("--epochs", type=int, default=3, help="Nombre d'epochs d'entraînement.")
    parser.add_argument("--lr_scheduler_type", type=str, default="cosine", help="Type de scheduler.")
    parser.add_argument("--warmup_ratio", type=float, default=0.05, help="Ratio de warmup.")
    parser.add_argument("--weight_decay", type=float, default=0.01, help="Weight decay.")
    parser.add_argument("--logging_steps", type=int, default=10, help="Intervalle de log.")
    parser.add_argument("--save_steps", type=int, default=200, help="Fréquence de sauvegarde.")
    parser.add_argument("--eval_steps", type=int, default=200, help="Fréquence d'évaluation.")
    parser.add_argument("--lora_r", type=int, default=16, help="Taille de la matrice LoRA (r).")
    parser.add_argument("--lora_alpha", type=int, default=32, help="Facteur d'échelle LoRA.")
    parser.add_argument("--lora_dropout", type=float, default=0.05, help="Dropout LoRA.")
    parser.add_argument(
        "--target_modules",
        type=str,
        default="q_proj,k_proj,v_proj,o_proj,gate_proj,up_proj,down_proj",
        help="Modules cibles pour LoRA (séparés par des virgules).",
    )
    parser.add_argument("--use_qlora", action="store_true", help="Active la quantification 4-bit QLoRA si un GPU est disponible.")
    parser.add_argument("--no_qlora", dest="use_qlora", action="store_false", help="Forcer le mode LoRA standard (sans quantification 4-bit).")
    parser.set_defaults(use_qlora=True)
    parser.add_argument("--seed", type=int, default=42, help="Seed pour la reproductibilité.")
    parser.add_argument("--push_to_hub", action="store_true", help="Publier le modèle sur le Hub Hugging Face.")
    parser.add_argument("--hub_model_id", type=str, default=None, help="ID Hub si --push_to_hub est activé.")
    return parser.parse_args()


def set_seed(seed: int) -> None:
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed_all(seed)


def load_jsonl_records(file_path: str) -> List[Dict[str, Any]]:
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    records: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                obj = json.loads(stripped)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSON on line {line_number} in {file_path}") from exc
            if not isinstance(obj, dict):
                raise ValueError(f"Each line must be a JSON object in {file_path}, got {type(obj).__name__}.")
            records.append(obj)

    if not records:
        raise ValueError(f"No valid records found in {file_path}.")

    return records


def normalize_record(record: Dict[str, Any]) -> Dict[str, str]:
    instruction = str(record.get("instruction", "")).strip()
    user_input = str(record.get("input", "")).strip()
    output = str(record.get("output", "")).strip()

    if not instruction and not output:
        raise ValueError(f"Each record must contain an 'instruction' and 'output' field. Got: {record}")

    if not output:
        output = ""

    if user_input:
        prompt = DEFAULT_TEMPLATE.format(instruction=instruction, input_text=user_input)
    elif instruction:
        prompt = f"### Instruction:\n{instruction}\n\n### Response:\n"
    else:
        prompt = "### Response:\n"

    return {"prompt": prompt, "response": output}


def build_completion_text(record: Dict[str, Any]) -> str:
    normalized = normalize_record(record)
    response = normalized["response"]
    return f"{normalized['prompt']}{response}"


def build_tokenized_dataset(dataset: Dataset, tokenizer: AutoTokenizer, max_seq_length: int):
    def tokenize_example(example: Dict[str, Any]):
        text = build_completion_text(example)
        prompt = text.rsplit("\n", 1)[0] if "\n" in text else text
        response = example.get("output", "")
        if response is None:
            response = ""
        response_text = str(response).strip()

        prefix_tokens = tokenizer(prompt, add_special_tokens=False, truncation=True, max_length=max_seq_length)
        response_tokens = tokenizer(
            response_text + (tokenizer.eos_token or ""),
            add_special_tokens=False,
            truncation=True,
            max_length=max_seq_length,
        )

        input_ids = prefix_tokens["input_ids"] + response_tokens["input_ids"]
        labels = [-100] * len(prefix_tokens["input_ids"]) + response_tokens["input_ids"]

        if len(input_ids) > max_seq_length:
            input_ids = input_ids[:max_seq_length]
            labels = labels[:max_seq_length]

        attention_mask = [1] * len(input_ids)
        return {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "labels": labels,
        }

    return dataset.map(tokenize_example, remove_columns=dataset.column_names)


def build_quantization_config(use_qlora: bool):
    if not use_qlora or not gpu_available() or BitsAndBytesConfig is None:
        return None

    compute_dtype = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    return BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_use_double_quant=True,
        bnb_4bit_quant_type="nf4",
        bnb_4bit_compute_dtype=compute_dtype,
    )


def build_lora_config(target_modules: List[str], learning_rate: float) -> LoraConfig:
    return LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=16,
        lora_alpha=32,
        lora_dropout=0.05,
        target_modules=target_modules,
        bias="none",
    )


def main() -> None:
    args = parse_args()
    set_seed(args.seed)

    if not args.train_file:
        raise ValueError("--train_file is required.")

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("Loading dataset...")
    records = load_jsonl_records(args.train_file)
    train_dataset = Dataset.from_list(records)

    if args.validation_file:
        validation_records = load_jsonl_records(args.validation_file)
        eval_dataset = Dataset.from_list(validation_records)
    else:
        split = train_dataset.train_test_split(test_size=0.1, seed=args.seed)
        train_dataset = split["train"]
        eval_dataset = split["test"]

    print(f"Train set: {len(train_dataset)} items")
    print(f"Validation set: {len(eval_dataset)} items")

    print("Loading tokenizer...")
    tokenizer = AutoTokenizer.from_pretrained(args.base_model, trust_remote_code=False)
    if tokenizer.pad_token_id is None:
        tokenizer.pad_token = tokenizer.eos_token
    tokenizer.padding_side = "right"

    print("Loading base model...")
    quant_config = build_quantization_config(args.use_qlora)
    model_kwargs = {"trust_remote_code": False}
    if quant_config is not None:
        model_kwargs["quantization_config"] = quant_config
        model_kwargs["device_map"] = "auto"
    elif gpu_available():
        model_kwargs["device_map"] = "auto"
        model_kwargs["torch_dtype"] = torch.bfloat16 if torch.cuda.is_bf16_supported() else torch.float16
    else:
        model_kwargs["torch_dtype"] = torch.float32

    model = AutoModelForCausalLM.from_pretrained(args.base_model, **model_kwargs)
    model.config.use_cache = False

    target_modules = [m.strip() for m in args.target_modules.split(",") if m.strip()]
    lora_config = LoraConfig(
        task_type=TaskType.CAUSAL_LM,
        r=args.lora_r,
        lora_alpha=args.lora_alpha,
        lora_dropout=args.lora_dropout,
        target_modules=target_modules,
        bias="none",
    )
    model = get_peft_model(model, lora_config)
    model.print_trainable_parameters()

    print("Tokenizing dataset...")
    train_dataset = build_tokenized_dataset(train_dataset, tokenizer, args.max_seq_length)
    eval_dataset = build_tokenized_dataset(eval_dataset, tokenizer, args.max_seq_length)

    # warmup_ratio was removed in transformers 5.x ; convertir en warmup_steps.
    steps_per_epoch = max(1, len(train_dataset) // (args.batch_size * args.gradient_accumulation_steps))
    warmup_steps = int(steps_per_epoch * args.epochs * args.warmup_ratio)

    training_args = TrainingArguments(
        output_dir=str(output_dir),
        per_device_train_batch_size=args.batch_size,
        per_device_eval_batch_size=max(1, args.batch_size),
        gradient_accumulation_steps=args.gradient_accumulation_steps,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        num_train_epochs=args.epochs,
        lr_scheduler_type=args.lr_scheduler_type,
        warmup_steps=warmup_steps,
        logging_steps=args.logging_steps,
        save_steps=args.save_steps,
        eval_steps=args.eval_steps,
        eval_strategy="steps" if args.validation_file or len(eval_dataset) > 0 else "no",
        save_strategy="steps",
        load_best_model_at_end=(bool(args.validation_file) or len(eval_dataset) > 0),
        metric_for_best_model="loss",
        greater_is_better=False,
        remove_unused_columns=False,
        report_to=[]
    )

    if torch.cuda.is_available() and not quant_config:
        training_args.fp16 = torch.cuda.is_available() and not torch.cuda.is_bf16_supported()
        training_args.bf16 = torch.cuda.is_available() and torch.cuda.is_bf16_supported()

    trainer = Trainer(
        model=model,
        args=training_args,
        train_dataset=train_dataset,
        eval_dataset=eval_dataset if len(eval_dataset) > 0 else None,
        processing_class=tokenizer,
        data_collator=DataCollatorForLanguageModeling(tokenizer=tokenizer, mlm=False),
    )

    print("Start training...")
    trainer.train()

    print("Saving final adapter...")
    trainer.save_model(str(output_dir))
    tokenizer.save_pretrained(str(output_dir))

    if args.push_to_hub:
        print("Pushing model to the Hub...")
        trainer.push_to_hub(
            commit_message="Fine-tuning with LoRA / QLoRA",
            hub_model_id=args.hub_model_id or output_dir.name,
        )

    print(f"Fine-tuning completed. Model saved in: {output_dir}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("Interrupted by user.")
        sys.exit(130)
