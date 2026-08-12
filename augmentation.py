"""
Système de recomposition (EDA - Easy Data Augmentation) pour l'analyse de sentiments.

Ce module recompose des phrases existantes en générant des variantes qui
conservent le sentiment d'origine, via 4 opérations :
  1. Synonym Replacement (SR)   -> remplace des mots par des synonymes
  2. Random Insertion (RI)      -> insère des synonymes à des positions aléatoires
  3. Random Swap (RS)           -> permute des mots entre eux
  4. Random Deletion (RD)       -> supprime aléatoirement des mots

Fonctionne pour le français et l'anglais (extensible à d'autres langues
via WordNet Open Multilingual).
"""

import random
import re
from typing import List

import nltk
from nltk.corpus import wordnet

# --- Setup NLTK (une seule fois) ---
for pkg in ["wordnet", "omw-1.4", "punkt", "punkt_tab"]:
    try:
        nltk.data.find(f"corpora/{pkg}" if "punkt" not in pkg else f"tokenizers/{pkg}")
    except LookupError:
        nltk.download(pkg, quiet=True)

# Codes langue WordNet Open Multilingual (OMW)
LANG_MAP = {
    "fr": "fra",
    "en": "eng",
}

STOPWORDS = {
    "fr": {"le", "la", "les", "un", "une", "des", "de", "du", "et", "à", "est",
           "que", "qui", "dans", "pour", "sur", "avec", "ce", "cet", "cette"},
    "en": {"the", "a", "an", "and", "of", "to", "is", "that", "which", "in",
           "for", "on", "with", "this", "that"},
}


def _get_synonyms(word: str, lang: str) -> List[str]:
    """Récupère des synonymes d'un mot via WordNet, dans la langue donnée."""
    wn_lang = LANG_MAP.get(lang, "eng")
    synonyms = set()
    for syn in wordnet.synsets(word, lang=wn_lang):
        for lemma in syn.lemmas(lang=wn_lang):
            candidate = lemma.name().replace("_", " ")
            if candidate.lower() != word.lower():
                synonyms.add(candidate)
    return list(synonyms)


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
        candidates = [w for w in new_words if w.lower() not in STOPWORDS.get(lang, set())]
        if not candidates:
            continue
        synonyms = []
        random.shuffle(candidates)
        for w in candidates:
            synonyms = _get_synonyms(w, lang)
            if synonyms:
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


def recompose(text: str, lang: str = "fr", num_variants: int = 4,
              alpha: float = 0.1) -> List[str]:
    """
    Point d'entrée principal : recompose un texte en `num_variants` nouvelles
    versions qui conservent le sentiment d'origine.

    Args:
        text: phrase/texte source
        lang: "fr" ou "en"
        num_variants: nombre de variantes à générer
        alpha: intensité de l'augmentation (proportion de mots affectés)

    Returns:
        Liste de textes recomposés (n'inclut pas l'original)
    """
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
    while len(variants) < num_variants and attempts < num_variants * 3:
        op = random.choice(operations)
        new_words = op(words)
        new_text = " ".join(new_words)
        new_text = re.sub(r"\s+([?.!,'])", r"\1", new_text)  # nettoyage ponctuation
        if new_text.strip() and new_text.strip() != text.strip():
            variants.add(new_text.strip())
        attempts += 1

    return list(variants)


if __name__ == "__main__":
    examples = [
        ("Ce film était vraiment excellent, j'ai adoré chaque instant.", "fr"),
        ("This movie was absolutely terrible, I hated every minute.", "en"),
    ]
    for text, lang in examples:
        print(f"\nOriginal ({lang}): {text}")
        for i, v in enumerate(recompose(text, lang=lang, num_variants=3), 1):
            print(f"  Variante {i}: {v}")
