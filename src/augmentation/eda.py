"""
Easy Data Augmentation (EDA) multilingue pour l'analyse de sentiments.

Opérations :
  - Synonym Replacement (SR)
  - Random Insertion (RI)
  - Random Swap (RS)
  - Random Deletion (RD)

Compatible FR + EN via WordNet Open Multilingual (OMW).
"""

import random
import re
from typing import List

import nltk
from nltk.corpus import wordnet

# --- Setup NLTK (une seule fois) ---
for pkg in ["wordnet", "omw-1.4", "punkt", "punkt_tab"]:
    try:
        nltk.data.find(
            f"corpora/{pkg}" if "punkt" not in pkg else f"tokenizers/{pkg}"
        )
    except LookupError:
        nltk.download(pkg, quiet=True)

# Codes langue WordNet Open Multilingual (OMW)
LANG_MAP = {
    "fr": "fra",
    "en": "eng",
}

STOPWORDS = {
    "fr": {
        "le", "la", "les", "un", "une", "des", "de", "du", "et", "à", "est",
        "que", "qui", "dans", "pour", "sur", "avec", "ce", "cet", "cette"
    },
    "en": {
        "the", "a", "an", "and", "of", "to", "is", "that", "which", "in",
        "for", "on", "with", "this", "that"
    },
}


# ---------------------------------------------------------------------------
# Synonym extraction
# ---------------------------------------------------------------------------

def _get_synonyms(word: str, lang: str) -> List[str]:
    """Récupère des synonymes via WordNet dans la langue donnée."""
    wn_lang = LANG_MAP.get(lang, "eng")
    synonyms = set()

    for syn in wordnet.synsets(word, lang=wn_lang):
        for lemma in syn.lemmas(lang=wn_lang):
            candidate = lemma.name().replace("_", " ")
            if candidate.lower() != word.lower():
                synonyms.add(candidate)

    return list(synonyms)


# ---------------------------------------------------------------------------
# EDA operations
# ---------------------------------------------------------------------------

def synonym_replacement(words: List[str], lang: str, n: int = 1) -> List[str]:
    """Remplace n mots (hors stopwords) par un de leurs synonymes."""
    new_words = words.copy()
    candidates = [w for w in words if w.lower() not in STOPWORDS.get(lang, set())]
    random.shuffle(candidates)

    replaced = 0
    for word in candidates:
        synonyms = _get_synonyms(word, lang)
        if synonyms:
            synonym = random.choice(synonyms)
            new_words = [synonym if w == word else w for w in new_words]
            replaced += 1
        if replaced >= n:
            break

    return new_words


def random_insertion(words: List[str], lang: str, n: int = 1) -> List[str]:
    """Insère n synonymes de mots existants à des positions aléatoires."""
    new_words = words.copy()

    for _ in range(n):
        candidates = [
            w for w in new_words if w.lower() not in STOPWORDS.get(lang, set())
        ]
        if not candidates:
            continue

        random.shuffle(candidates)
        synonyms = None

        for w in candidates:
            syns = _get_synonyms(w, lang)
            if syns:
                synonyms = syns
                break

        if synonyms:
            insert_word = random.choice(synonyms)
            insert_pos = random.randint(0, len(new_words))
            new_words.insert(insert_pos, insert_word)

    return new_words


def random_swap(words: List[str], n: int = 1) -> List[str]:
    """Permute n paires de mots aléatoirement."""
    new_words = words.copy()
    length = len(new_words)

    if length < 2:
        return new_words

    for _ in range(n):
        idx1, idx2 = random.sample(range(length), 2)
        new_words[idx1], new_words[idx2] = new_words[idx2], new_words[idx1]

    return new_words


def random_deletion(words: List[str], p: float = 0.1) -> List[str]:
    """Supprime chaque mot avec une probabilité p (garde au moins 1 mot)."""
    if len(words) == 1:
        return words

    new_words = [w for w in words if random.random() > p]
    if not new_words:
        return [random.choice(words)]

    return new_words


# ---------------------------------------------------------------------------
# Main recomposition function
# ---------------------------------------------------------------------------

def recompose(
    text: str,
    lang: str = "fr",
    num_variants: int = 4,
    alpha: float = 0.1,
) -> List[str]:
    """
    Recompose un texte en `num_variants` nouvelles versions.

    Args:
        text: texte source
        lang: "fr" ou "en"
        num_variants: nombre de variantes à générer
        alpha: intensité de l'augmentation (proportion de mots affectés)

    Returns:
        Liste de textes recomposés
    """
    # Tokenisation simple (mots + ponctuation)
    words = re.findall(r"\w+|[^\w\s]", text, re.UNICODE)
    num_words = len(words)
    n = max(1, int(alpha * num_words))

    variants = set()

    operations = [
        lambda w: synonym_replacement(w, lang, n),
        lambda w: random_insertion(w, lang, n),
        lambda w: random_swap(w, n),
        lambda w: random_deletion(w, alpha),
    ]

    attempts = 0
    max_attempts = num_variants * 4

    while len(variants) < num_variants and attempts < max_attempts:
        op = random.choice(operations)
        new_words = op(words)

        new_text = " ".join(new_words)
        new_text = re.sub(r"\s+([?.!,'])", r"\1", new_text)

        if new_text.strip() and new_text.strip() != text.strip():
            variants.add(new_text.strip())

        attempts += 1

    return list(variants)


# ---------------------------------------------------------------------------
# Debug mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    examples = [
        ("Ce film était vraiment excellent, j'ai adoré chaque instant.", "fr"),
        ("This movie was absolutely terrible, I hated every minute.", "en"),
    ]

    for text, lang in examples:
        print(f"\nOriginal ({lang}): {text}")
        for i, v in enumerate(recompose(text, lang=lang, num_variants=3), 1):
            print(f"  Variante {i}: {v}")
