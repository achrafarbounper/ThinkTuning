import argparse
import json
import os
import re
from typing import List, Optional

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer

try:
    from peft import AutoPeftModelForCausalLM, PeftModel
except Exception:  # pragma: no cover
    AutoPeftModelForCausalLM = None
    PeftModel = None


INSTRUCTION = "Classify the sentiment of the following text as negative, neutral, or positive."

LABEL_ALIASES = {
    "negative": ["negative", "négatif", "negatif", "bad", "mauvais", "décevant", "decevant"],
    "neutral": ["neutral", "neutre", "mixed", "mitigé", "mitige", "okay", "ok", "average", "moyen"],
    "positive": ["positive", "positif", "good", "excellent", "bon", "satisfait", "content", "ravi"],
}


def parse_generation(raw_text: str) -> dict:
    text = (raw_text or "").strip()
    if not text:
        return {"sentiment": "unknown", "confidence": 0.0}

    normalized = text.lower()

    label = None
    for candidate_label, aliases in LABEL_ALIASES.items():
        pattern = r"\b(?:" + "|".join(re.escape(alias) for alias in aliases) + r")\b"
        if re.search(pattern, normalized):
            label = candidate_label
            break

    if label is None:
        for candidate in ("negative", "neutral", "positive"):
            if candidate in normalized:
                label = candidate
                break

    if label is None:
        match = re.search(
            r"(negative|neutral|positive|négatif|neutre|positif|negatif|mitigé|mitige|mauvais|bon|excellent)",
            normalized,
        )
        if match:
            candidate = match.group(1).lower()
            if candidate in {"négatif", "negatif"}:
                label = "negative"
            elif candidate in {"neutre"}:
                label = "neutral"
            elif candidate in {"positif"}:
                label = "positive"
            elif candidate in {"mitigé", "mitige"}:
                label = "neutral"
            elif candidate in {"mauvais", "bad"}:
                label = "negative"
            elif candidate in {"bon", "good", "excellent"}:
                label = "positive"

    confidence = 0.0
    confidence_match = re.search(r"(?:confidence|confiance)\s*[:=]?\s*(0?\.?\d+(?:e[-+]?\d+)?)", normalized)
    if confidence_match:
        try:
            confidence = float(confidence_match.group(1))
        except ValueError:
            confidence = 0.0

    if label is None:
        label = "unknown"

    return {"sentiment": label, "confidence": max(0.0, min(1.0, confidence))}


def build_prompt(text: str) -> str:
    return (
        f"### Instruction:\n{INSTRUCTION}\n\n"
        f"### Input:\n{text}\n\n"
        "### Response:\n"
    )


def extract_label(raw_text: str) -> Optional[str]:
    return parse_generation(raw_text).get("sentiment")


def load_model(model_path: str, base_model: Optional[str] = None):
    if not os.path.exists(model_path):
        raise FileNotFoundError(f"Model path not found: {model_path}")

    adapter_markers = [
        os.path.join(model_path, "adapter_config.json"),
        os.path.join(model_path, "adapter_model.safetensors"),
        os.path.join(model_path, "adapter_model.bin"),
    ]
    is_adapter = any(os.path.exists(marker) for marker in adapter_markers)

    if is_adapter:
        if AutoPeftModelForCausalLM is not None:
            model = AutoPeftModelForCausalLM.from_pretrained(model_path, device_map="auto")
            return model.eval()
        if PeftModel is None:
            raise RuntimeError("PEFT is required to load adapter models. Install with: pip install peft")
        if not base_model:
            raise ValueError("--base_model is required when loading a PEFT adapter directory.")
        base = AutoModelForCausalLM.from_pretrained(base_model, device_map="auto")
        model = PeftModel.from_pretrained(base, model_path)
        return model.eval()

    model = AutoModelForCausalLM.from_pretrained(model_path, device_map="auto")
    return model.eval()


def load_tokenizer(model_path: str, base_model: Optional[str] = None):
    if os.path.exists(os.path.join(model_path, "tokenizer_config.json")):
        return AutoTokenizer.from_pretrained(model_path)
    if base_model:
        return AutoTokenizer.from_pretrained(base_model)
    raise ValueError("Unable to find tokenizer. Provide a valid model path or --base_model.")


def predict_text(model, tokenizer, text: str, max_new_tokens: int = 32) -> dict:
    prompt = build_prompt(text)
    inputs = tokenizer(prompt, return_tensors="pt")
    device = next(model.parameters()).device
    inputs = {k: v.to(device) for k, v in inputs.items()}

    with torch.no_grad():
        generated = model.generate(
            **inputs,
            max_new_tokens=max_new_tokens,
            do_sample=False,
            temperature=0.0,
            pad_token_id=tokenizer.eos_token_id,
            eos_token_id=tokenizer.eos_token_id,
        )

    generated_text = tokenizer.decode(
        generated[0][inputs["input_ids"].shape[1]:],
        skip_special_tokens=True,
    ).strip()

    parsed = parse_generation(generated_text)
    label = parsed["sentiment"]
    confidence = parsed["confidence"]

    if label == "unknown":
        cleaned = generated_text.lower().strip(". ")
        return {
            "text": text,
            "sentiment": "unknown",
            "confidence": 0.0,
            "raw_output": generated_text,
            "normalized_output": cleaned,
        }

    return {
        "text": text,
        "sentiment": label,
        "confidence": confidence,
        "raw_output": generated_text,
        "normalized_output": label,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run sentiment prediction with a LoRA / QLoRA finetuned LLM.")
    parser.add_argument("--model_path", type=str, required=True, help="Path to the finetuned adapter or merged model directory.")
    parser.add_argument("--base_model", type=str, default=None, help="Base model used to load the adapter if needed.")
    parser.add_argument("--text", type=str, default=None, help="Text to classify.")
    parser.add_argument("--input_file", type=str, default=None, help="Optional file containing one text per line.")
    parser.add_argument("--max_new_tokens", type=int, default=32, help="Maximum number of generated tokens.")
    return parser.parse_args()


def load_texts(input_text: Optional[str], input_file: Optional[str]) -> List[str]:
    if input_text is not None:
        return [input_text]
    if input_file is not None:
        with open(input_file, "r", encoding="utf-8") as handle:
            return [line.strip() for line in handle if line.strip()]
    raise ValueError("Provide --text or --input_file.")


def main() -> None:
    args = parse_args()

    model = load_model(args.model_path, args.base_model)
    tokenizer = load_tokenizer(args.model_path, args.base_model)

    texts = load_texts(args.text, args.input_file)
    results = [predict_text(model, tokenizer, text, args.max_new_tokens) for text in texts]

    for result in results:
        print(json.dumps({
            "text": result["text"],
            "sentiment": result["sentiment"],
            "confidence": round(float(result["confidence"]), 3),
        }, ensure_ascii=False))


if __name__ == "__main__":
    main()
