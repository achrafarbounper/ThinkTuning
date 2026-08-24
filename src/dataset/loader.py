"""
Chargement d'un dataset multilingue (français + anglais) pour la classification
de sentiments, avec possibilité d'appliquer une augmentation EDA.

Dataset : cardiffnlp/tweet_sentiment_multilingual
Chargé via les fichiers Parquet auto-convertis par Hugging Face.
"""

import csv
import hashlib
import json
import json
import logging
import random
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import numpy as np
import pandas as pd
from datasets import (
    ClassLabel,
    Dataset,
    Value,
    concatenate_datasets,
    load_dataset,
)

logger = logging.getLogger(__name__)

LABEL_NAMES = {0: "negative", 1: "neutral", 2: "positive"}

# Colonnes attendues dans un fichier de corrections locales (CSV ou JSONL).
# Format stabilisé par SCRUM-56 (merge_reviewed_data.py) :
#   - text      : str non vide
#   - label     : entier 0/1/2 ou nom ("negative" / "neutral" / "positive")
#   - lang_code : code langue supporté par le pipeline ("fr" / "en")
CORRECTIONS_REQUIRED_COLUMNS = ("text", "label", "lang_code")

# Extensions de fichiers acceptées pour les corrections locales.
_CORRECTIONS_CSV_EXTENSIONS = {".csv"}
_CORRECTIONS_JSONL_EXTENSIONS = {".jsonl", ".ndjson"}

# Pondération par défaut de la sélection des exemples à augmenter. La classe
# neutral (label 1) est surpondérée car typiquement sous-représentée dans le
# corpus : des exemples neutral sont donc préférentiellement sur-échantillonnés.
DEFAULT_CLASS_AUGMENT_WEIGHTS: Dict[int, float] = {0: 1.0, 1: 2.0, 2: 1.0}

_PARQUET_BASE = (
    "https://huggingface.co/datasets/cardiffnlp/tweet_sentiment_multilingual"
    "/resolve/refs%2Fconvert%2Fparquet"
)

_LANG_CONFIG = {"fr": "french", "en": "english"}


def _is_missing(value) -> bool:
    """True si la valeur est None ou un NaN pandas/numpy (cellule vide CSV/JSON)."""
    try:
        return value is None or bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _invalid_label_error(raw_label, row_index: int) -> ValueError:
    return ValueError(
        f"Fichier de corrections locales, ligne {row_index} : label invalide "
        f"{raw_label!r}. Valeurs acceptées : entiers 0/1/2 ou noms "
        f"{sorted(set(LABEL_NAMES.values()))} "
        f"(mapping : {LABEL_NAMES})."
    )


def _normalize_correction_label(raw_label, row_index: int) -> int:
    """
    Normalise un label de correction vers l'entier canonique 0/1/2.

    Accepte indifféremment :
      - un nom de classe : "negative", "neutral", "positive" (insensible à la casse)
      - un entier (numpy inclus) ou une chaîne numérique : 0, 1, 2 ("1", " 2 ", 2.0)

    Lève une ValueError explicite sinon.
    """
    if isinstance(raw_label, bool):
        raise _invalid_label_error(raw_label, row_index)

    # 1) Noms de classes ("negative" / "neutral" / "positive")
    name_to_id = {name: idx for idx, name in LABEL_NAMES.items()}
    normalized = str(raw_label).strip().lower()
    if normalized in name_to_id:
        return name_to_id[normalized]

    # 2) Entier direct ou chaîne numérique ("1", " 2 ", 2.0)
    try:
        value = float(str(raw_label).strip())
    except (TypeError, ValueError):
        raise _invalid_label_error(raw_label, row_index)

    # Garde-fous : 1.5 ne doit pas être tronqué en 1 ; NaN rejeté aussi.
    if not value.is_integer() or int(value) not in LABEL_NAMES:
        raise _invalid_label_error(raw_label, row_index)
    return int(value)


def _read_corrections_csv(corrections_path: Path) -> pd.DataFrame:
    """Lit un CSV de corrections (tolérant au BOM utf-8-sig)."""
    try:
        return pd.read_csv(corrections_path, encoding="utf-8-sig")
    except Exception as exc:
        raise ValueError(
            f"Impossible de lire le fichier CSV de corrections "
            f"{str(corrections_path)!r} : {exc}"
        ) from exc


def _read_corrections_jsonl(corrections_path: Path) -> pd.DataFrame:
    """Lit un JSONL de corrections, avec erreurs explicites par ligne."""
    rows = []
    with corrections_path.open("r", encoding="utf-8-sig") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Ligne JSON invalide dans {str(corrections_path)!r} "
                    f"(ligne {line_number}) : {exc}"
                ) from exc
            if not isinstance(payload, dict):
                raise ValueError(
                    f"Ligne JSON invalide dans {str(corrections_path)!r} "
                    f"(ligne {line_number}) : un objet JSON est attendu, "
                    f"reçu {type(payload).__name__}."
                )
            rows.append(payload)

    if not rows:
        return pd.DataFrame(columns=list(CORRECTIONS_REQUIRED_COLUMNS))
    return pd.DataFrame(rows)


def _validate_corrections_columns(df: pd.DataFrame, path_display: str) -> None:
    """Vérifie que les colonnes requises sont présentes dans le DataFrame."""
    columns = [str(col).strip() for col in df.columns]
    missing = [col for col in CORRECTIONS_REQUIRED_COLUMNS if col not in columns]
    if missing:
        raise ValueError(
            f"Fichier de corrections locales {path_display!r} invalide : "
            f"colonne(s) manquante(s) {missing}. Colonnes attendues : "
            f"{list(CORRECTIONS_REQUIRED_COLUMNS)}. Colonnes trouvées : {columns}."
        )


def load_local_corrections(path: str) -> Dataset:
    """
    Charge un fichier local de corrections manuelles (CSV ou JSONL) au format
    stabilisé par SCRUM-56 : colonnes ``text``, ``label``, ``lang_code``.

    Validation effectuée :
      - le chemin doit exister (FileNotFoundError explicite sinon) ;
      - l'extension doit être .csv / .jsonl / .ndjson ;
      - les trois colonnes attendues doivent être présentes ;
      - chaque ligne doit avoir un texte non vide, un label valide (0/1/2 ou
        negative/neutral/positive) et un lang_code supporté (fr/en).

    Args:
        path: chemin du fichier de corrections (.csv ou .jsonl)

    Returns:
        Dataset Hugging Face avec colonnes 'text' (str), 'label' (int64)
        et 'lang_code' (str). Vide si le fichier ne contient aucune ligne.
    """
    corrections_path = Path(path)

    if not corrections_path.exists():
        raise FileNotFoundError(
            f"Fichier de corrections locales introuvable : {path!r}. "
            "Vérifiez le chemin fourni via --local_corrections_path "
            "(CLI) ou local_corrections_path (POST /train)."
        )
    if not corrections_path.is_file():
        raise ValueError(
            f"Le chemin des corrections locales {path!r} n'est pas un fichier."
        )

    extension = corrections_path.suffix.lower()
    if extension in _CORRECTIONS_CSV_EXTENSIONS:
        df = _read_corrections_csv(corrections_path)
    elif extension in _CORRECTIONS_JSONL_EXTENSIONS:
        df = _read_corrections_jsonl(corrections_path)
    else:
        raise ValueError(
            f"Format de fichier de corrections non supporté : {path!r} "
            f"(extension {extension!r}). Formats acceptés : CSV (.csv) ou "
            f"JSONL ({sorted(_CORRECTIONS_JSONL_EXTENSIONS)})."
        )

    _validate_corrections_columns(df, str(corrections_path))

    texts = []
    labels = []
    lang_codes = []

    for row_index, row in enumerate(df.to_dict("records"), start=1):
        raw_text = row.get("text")
        text = "" if _is_missing(raw_text) else str(raw_text).strip()
        if not text:
            raise ValueError(
                f"Fichier de corrections locales {str(corrections_path)!r}, "
                f"ligne {row_index} : colonne 'text' vide ou manquante."
            )

        raw_lang = row.get("lang_code")
        lang_code = "" if _is_missing(raw_lang) else str(raw_lang).strip().lower()
        if lang_code not in _LANG_CONFIG:
            raise ValueError(
                f"Fichier de corrections locales {str(corrections_path)!r}, "
                f"ligne {row_index} : lang_code invalide {raw_lang!r}. "
                f"Langues supportées par le pipeline : {sorted(_LANG_CONFIG)}."
            )

        raw_label = row.get("label")
        if _is_missing(raw_label):
            raise _invalid_label_error(raw_label, row_index)

        texts.append(text)
        labels.append(int(_normalize_correction_label(raw_label, row_index)))
        lang_codes.append(lang_code)

    dataset = Dataset.from_dict({
        "text": texts,
        "label": labels,
        "lang_code": lang_codes,
    })

    logger.info(
        "load_local_corrections: %s correction(s) chargée(s) depuis %s",
        len(dataset),
        corrections_path,
    )
    return dataset


def load_raw_dataset(
    languages: Iterable[str] = ("fr", "en"),
    max_per_lang: Optional[int] = 3000,
    seed: int = 42,
    local_corrections_path: Optional[str] = None,
) -> Dataset:
    """
    Charge les sous-ensembles de langues demandées depuis les fichiers Parquet HF.

    Args:
        languages: liste des langues à charger ("fr", "en")
        max_per_lang: limite d'exemples par langue
        seed: graine aléatoire
        local_corrections_path: chemin optionnel d'un fichier local de
            corrections manuelles (CSV ou JSONL avec colonnes text, label,
            lang_code — voir load_local_corrections). Les corrections sont
            concaténées AU DATASET COMPLET, avant tout split train/val et avant
            l'augmentation EDA (principe « augmentation après split » pour
            éviter toute fuite de données). None => comportement historique
            inchangé (dataset HF seul).

    Returns:
        Dataset Hugging Face concaténé
    """
    subsets = []

    for lang_code in languages:
        config = _LANG_CONFIG[lang_code]
        data_files = {"train": f"{_PARQUET_BASE}/{config}/train/0000.parquet"}

        ds = load_dataset("parquet", data_files=data_files, split="train")

        if max_per_lang:
            ds = ds.shuffle(seed=seed).select(range(min(max_per_lang, len(ds))))

        ds = ds.add_column("lang_code", [lang_code] * len(ds))
        subsets.append(ds)

    if local_corrections_path is not None:
        corrections = load_local_corrections(local_corrections_path)
        if len(corrections) == 0:
            logger.warning(
                "load_raw_dataset: le fichier de corrections %s est vide, "
                "aucune concaténation effectuée.",
                local_corrections_path,
            )
        else:
            # Le Parquet HF type la colonne 'label' en ClassLabel alors que les
            # corrections sont en int64 : on aligne sur int64 pour que la
            # concaténation soit possible. Appliqué uniquement dans cette branche
            # => comportement par défaut inchangé.
            subsets = [
                subset.cast_column("label", Value("int64"))
                if isinstance(subset.features.get("label"), ClassLabel)
                else subset
                for subset in subsets
            ]
            base_size = sum(len(subset) for subset in subsets)
            merged = concatenate_datasets(subsets + [corrections])
            logger.info(
                "load_raw_dataset: %s correction(s) locale(s) concaténée(s) au "
                "dataset HF (%s exemples -> %s).",
                len(corrections),
                base_size,
                len(merged),
            )
            return merged

    return concatenate_datasets(subsets)


_TEXT_COLUMN_ALIASES = ("text", "input")
_LABEL_COLUMN_ALIASES = ("label", "output", "sentiment")


def coerce_label(value) -> int:
    """
    Convertit une valeur de label en entier valide selon LABEL_NAMES.

    Accepte les entiers (0, 1, 2), leurs représentations textuelles ("0",
    "2.0") ainsi que les noms de classes ("negative" / "neutral" / "positive").
    """
    name_to_int = {name: index for index, name in LABEL_NAMES.items()}

    if isinstance(value, bool):
        raise ValueError(f"Invalid label value: {value!r}")
    if isinstance(value, (int, np.integer)):
        label = int(value)
    else:
        text = "" if value is None else str(value).strip().lower()
        if not text:
            raise ValueError("Missing label value")
        if text in name_to_int:
            return name_to_int[text]
        try:
            label = int(float(text))
        except ValueError:
            raise ValueError(f"Unknown label value: {value!r}") from None

    if label in LABEL_NAMES:
        return label
    raise ValueError(f"Label out of range for {LABEL_NAMES}: {value!r}")


def _read_records_from_file(path: Path) -> List[dict]:
    """Lit un fichier CSV, JSON ou JSONL et renvoie une liste de dictionnaires."""
    suffix = path.suffix.lower()

    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            return [dict(row) for row in csv.DictReader(handle)]

    if suffix in {".json", ".jsonl"}:
        if suffix == ".jsonl":
            records = []
            with path.open("r", encoding="utf-8-sig") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        records.append(json.loads(line))
        else:
            with path.open("r", encoding="utf-8-sig") as handle:
                payload = json.load(handle)
            if isinstance(payload, list):
                records = payload
            elif isinstance(payload, dict):
                if isinstance(payload.get("records"), list):
                    records = payload["records"]
                elif isinstance(payload.get("data"), list):
                    records = payload["data"]
                else:
                    records = [payload]
            else:
                raise ValueError(
                    f"Unsupported JSON payload type in {path}: {type(payload).__name__}"
                )
        return [record for record in records if isinstance(record, dict)]

    raise ValueError(f"Unsupported input format for {path}: {suffix or 'unknown'}")


def load_local_dataset(
    file_path: str,
    default_lang_code: str = "fr",
) -> Dataset:
    """
    Charge un jeu de données local (CSV, JSON ou JSONL) en Dataset HF avec les
    colonnes 'text', 'label' (entier) et 'lang_code'.

    Colonnes reconnues : 'text' ou 'input' pour le texte ; 'label', 'output'
    ou 'sentiment' pour le label (entier ou nom de classe, cf. coerce_label) ;
    'lang_code' optionnel (sinon default_lang_code).

    Utilisée notamment par train.py --dataset_file pour consommer le dataset
    enrichi produit par merge_reviewed_data.py.
    """
    path = Path(file_path)
    if not path.exists():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    texts: List[str] = []
    labels: List[int] = []
    langs: List[str] = []

    for row in _read_records_from_file(path):
        text_value = next((row[column] for column in _TEXT_COLUMN_ALIASES if row.get(column)), None)
        if text_value is None:
            continue
        text = str(text_value).strip()
        if not text:
            continue

        # Attention : 0 est un label valide, on teste donc explicitement None.
        label_value = next(
            (row[column] for column in _LABEL_COLUMN_ALIASES if row.get(column) is not None),
            None,
        )
        label = coerce_label(label_value)

        lang = str(row.get("lang_code") or default_lang_code).strip() or default_lang_code
        texts.append(text)
        labels.append(label)
        langs.append(lang)

    if not texts:
        raise ValueError(f"No usable rows found in {file_path}")

    logger.info("load_local_dataset: loaded %s examples from %s", len(texts), file_path)
    return Dataset.from_dict({"text": texts, "label": labels, "lang_code": langs})


def augment_dataset(
    dataset: Dataset,
    variants_per_example: int = 2,
    augment_fraction: float = 0.5,
    seed: int = 42,
    deduplicate: bool = True,
    class_augment_weights: Optional[Dict[int, float]] = None,
) -> Dataset:
    """
    Applique la recomposition EDA sur une fraction du dataset.

    La sélection des exemples à augmenter est pondérée par classe : les poids
    de `class_augment_weights` (label -> poids) augmentent préférentiellement
    la probabilité de sélection des exemples de la classe concernée. Par défaut,
    la classe neutral (label 1) est surpondérée pour compenser sa
    sous-représentation typique dans le corpus.

    Args:
        dataset: dataset HF avec colonnes 'text', 'label', 'lang_code'
        variants_per_example: nombre de variantes générées par texte
        augment_fraction: proportion du dataset à augmenter
        seed: graine aléatoire
        deduplicate: supprime les doublons de texte normalisé avant sampling
        class_augment_weights: dict optionnel {label: poids} pour sur-échantillonner
            préférentiellement certaines classes (ex. {1: 3.0} pour surreprésenter
            la classe neutral). None => surpoids par défaut sur la classe neutral.

    Returns:
        Dataset augmenté
    """
    from src.augmentation.eda import recompose

    random.seed(seed)
    df = dataset.to_pandas().copy()
    if deduplicate:
        seen_hashes = set()
        keep_mask = []
        removed_duplicates = 0

        for text in df["text"].fillna("").astype(str):
            normalized_text = text.strip().lower()
            text_hash = hashlib.sha256(normalized_text.encode("utf-8")).hexdigest()
            if text_hash in seen_hashes:
                removed_duplicates += 1
                keep_mask.append(False)
                continue
            seen_hashes.add(text_hash)
            keep_mask.append(True)

        if removed_duplicates:
            logger.info(
                "augment_dataset: removed %s duplicate texts before augmentation",
                removed_duplicates,
            )
            df = df.loc[keep_mask].reset_index(drop=True).copy()

    n_to_augment = int(len(df) * augment_fraction)
    if n_to_augment <= 0:
        return Dataset.from_pandas(df.reset_index(drop=True))

    # Normalise les poids (accepte des clés int ou str issues de YAML/JSON) et
    # bascule sur le surpoids par défaut de la classe neutral si rien n'est fourni.
    if class_augment_weights:
        weights: Dict[int, float] = {}
        for key, weight in class_augment_weights.items():
            try:
                label = int(str(key).strip())
            except (TypeError, ValueError):
                raise ValueError(
                    f"class_augment_weights : clé invalide {key!r} — attendu un "
                    "label de classe entier (ex. 0, 1, 2). Les clés "
                    "'additionalProp1', 'additionalProp2'... sont des placeholders "
                    "Swagger UI envoyés tels quels : fournissez de vrais labels "
                    "(ex. {\"1\": 3.0}) ou omettez le champ pour utiliser les "
                    "poids par défaut."
                ) from None
            if float(weight) < 0:
                raise ValueError(
                    f"class_augment_weights : poids négatif interdit pour le "
                    f"label {label} ({weight})."
                )
            weights[label] = float(weight)
    else:
        weights = dict(DEFAULT_CLASS_AUGMENT_WEIGHTS)

    row_weights = df["label"].map(lambda lab: weights.get(lab, 1.0)).astype(float)
    # Échantillonnage pondéré SANS remplacement : pandas refuse de combiner
    # poids élevés + replace=False sur les petits datasets, on passe donc par
    # numpy qui renormalise les poids restants à chaque tirage.
    probs = row_weights.to_numpy(dtype=float)
    if probs.sum() <= 0:
        probs = None  # poids nuls partout => retombée sur un échantillonnage uniforme
    elif probs.sum() != 1.0:
        probs = probs / probs.sum()
    rng = np.random.RandomState(seed)
    selected_idx = rng.choice(len(df), size=n_to_augment, replace=False, p=probs)
    rows_to_augment = df.iloc[selected_idx]

    augmented_rows = []

    for _, row in rows_to_augment.iterrows():
        variants = recompose(
            row["text"],
            lang=row["lang_code"],
            num_variants=variants_per_example,
        )

        for v in variants:
            augmented_rows.append({
                "text": v,
                "label": row["label"],
                "lang_code": row["lang_code"],
            })

    augmented_df = pd.DataFrame(augmented_rows)
    full_df = pd.concat([df, augmented_df], ignore_index=True)
    full_df = full_df.sample(frac=1, random_state=seed).reset_index(drop=True)

    return Dataset.from_pandas(full_df)
