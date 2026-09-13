"""Persistiere annotation store oder Active Learning review cycle."""

import hashlib
import json
import logging
import os
import threading
import time
import unicodedata
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# Empty-string literal built at runtime (avoids any adjacent-quote run in source).
E = str()

LABEL_TO_INT = {"negative": 0, "neutral": 1, "positive": 2}
INT_TO_LABEL = {v: k for k, v in LABEL_TO_INT.items()}
LABEL_ALIASES = {
    "negatif": "negative",
    "negatif": "negative",
    "neutre": "neutral",
    "positif": "positive",
}
DEFAULT_ANNOTATIONS_PATH = os.path.join("data", "annotations.jsonl")


def get_annotations_path() -> str:
    return os.getenv("ANNOTATIONS_PATH", DEFAULT_ANNOTATIONS_PATH)


def _fold_accents(text: str) -> str:
    return E.join(
        ch for ch in unicodedata.normalize("NFD", text)
        if unicodedata.category(ch) != "Mn"
    )


def normalize_label(raw) -> int:
    if isinstance(raw, bool):
        raise ValueError("Label invalide : " + repr(raw) + "; attendu negative/neutral/positive.")
    name = _fold_accents(str(raw or E).strip().lower())
    if name in LABEL_ALIASES:
        return LABEL_TO_INT[LABEL_ALIASES[name]]
    if name in LABEL_TO_INT:
        return LABEL_TO_INT[name]
    try:
        value = float(name)
    except (TypeError, ValueError):
        raise ValueError("Label invalide : " + repr(raw) + "; attendu negative/neutral/positive.")
    if not value.is_integer() or int(value) not in INT_TO_LABEL:
        raise ValueError("Label invalide : " + repr(raw) + "; attendu negative/neutral/positive.")
    return int(value)


def normalize_text(text) -> str:
    return str(text or E).strip().lower()


def _text_key(text) -> str:
    return normalize_text(text)


class AnnotationStore:
    """Thread-safe JSONL annotation journal, deduplicated by normalized text."""

    def __init__(self, path: Optional[str] = None):
        self.path = path or get_annotations_path()
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._annotations: Dict[str, Dict] = {}
        self._load()

    def _load(self):
        records = {}
        if os.path.isfile(self.path):
            try:
                with open(self.path, "r", encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        rec = json.loads(line)
                        key = _text_key(rec.get("text"))
                        records[key] = rec
            except Exception as exc:
                logger.warning("Annotation journal illisible : %s", exc)
        self._annotations = records

    def _persist(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            for rec in self._annotations.values():
                handle.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(tmp, self.path)

    def annotate(self, text: str, label, force: bool = False) -> Dict:
        text = str(text or E).strip()
        if not text:
            raise ValueError("Texte vide : impossible annote.")
        label_int = normalize_label(label)
        now = time.time()
        with self._lock:
            key = _text_key(text)
            existing = self._annotations.get(key)
            rec = None
            if existing is not None and not force:
                existing["label"] = label_int
                existing["updated_at"] = now
                rec = existing
            else:
                rec = {
                    "text": text,
                    "label": label_int,
                    "created_at": now,
                    "updated_at": now,
                }
                self._annotations[key] = rec
            self._persist()
            return dict(rec)

    def list(self, limit: Optional[int] = None, offset: int = 0) -> List[Dict]:
        records = sorted(
            self._annotations.values(),
            key=lambda r: float(r.get("updated_at", 0.0)),
            reverse=True,
        )
        end = (offset + limit) if limit else None
        return [dict(r) for r in records[offset:end]]

    def count(self) -> int:
        return len(self._annotations)

    def remove(self, text: str) -> bool:
        with self._lock:
            key = _text_key(text)
            if key in self._annotations:
                del self._annotations[key]
                self._persist()
                return True
        return False

    def corrections(self) -> List[Dict]:
        return [
            {"text": rec["text"], "label": int(rec["label"])}
            for rec in self._annotations.values()
        ]

    def export_review_csv(self, output_path: Optional[str] = None) -> str:
        import csv
        out = output_path or os.path.join(os.path.dirname(self.path), "annotations_review.csv")
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        rows = sorted(
            self._annotations.values(),
            key=lambda r: float(r.get("updated_at", 0.0)),
        )
        with open(out, "w", encoding="utf-8-sig", newline=E) as handle:
            writer = csv.DictWriter(
                handle, fieldnames=["text", "predicted_label", "manual_label", "status"]
            )
            writer.writeheader()
            for rec in rows:
                label_name = INT_TO_LABEL[int(rec["label"])]
                writer.writerow({
                    "text": rec["text"],
                    "predicted_label": label_name,
                    "manual_label": label_name,
                    "status": "reviewed",
                })
        return out

    def merge_annotations(
        self,
        output_path: Optional[str] = None,
        source: str = "hf",
        default_lang_code: str = "fr",
    ) -> Dict:
        from merge_reviewed_data import load_source_records, merge_corrections_into_source
        corrections = self.corrections()
        source_records = load_source_records(source, ["fr", "en"], None)
        merged, stats = merge_corrections_into_source(
            corrections,
            source_records,
            default_lang_code=default_lang_code,
        )
        out = output_path or os.path.join("data", "train_enriched.jsonl")
        Path(out).parent.mkdir(parents=True, exist_ok=True)
        with open(out, "w", encoding="utf-8") as handle:
            for rec in merged:
                handle.write(json.dumps({
                    "text": rec["text"],
                    "label": int(rec["label"]),
                    "lang_code": rec["lang_code"],
                }, ensure_ascii=False) + "\n")
        stats["output_path"] = out
        return stats


_store_singleton = None
_singleton_lock = threading.Lock()


def get_annotation_store() -> AnnotationStore:
    """Lazy thread-safe access to the shared store."""
    global _store_singleton
    if _store_singleton is None:
        with _singleton_lock:
            if _store_singleton is None:
                _store_singleton = AnnotationStore()
    return _store_singleton
