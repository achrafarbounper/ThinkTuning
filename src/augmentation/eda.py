"""
Easy Data Augmentation (EDA) multilingue pour l'analyse de sentiments.

Opérations :
  - Synonym Replacement (SR)
  - Random Insertion (RI)
  - Random Swap (RS)
  - Random Deletion (RD)
  - Back Translation (BT) : FR→EN puis EN→FR via Helsinki-NLP/opus-mt
    (désactivée par défaut, activation via `use_back_translation`).

Compatible FR + EN via WordNet Open Multilingual (OMW).
"""

import random
import re
import logging
from typing import List

logger = logging.getLogger(__name__)


def _ensure_nltk_data():
    """Download NLTK resources only when augmentation is actually used."""
    try:
        import nltk
        from nltk.corpus import wordnet
    except ImportError as exc:  # pragma: no cover - dependency check is user-facing
        raise ImportError(
            "NLTK is required for EDA augmentation. Install it with `pip install nltk`."
        ) from exc

    for pkg in ["wordnet", "omw-1.4", "punkt", "punkt_tab"]:
        try:
            nltk.data.find(
                f"corpora/{pkg}" if "punkt" not in pkg else f"tokenizers/{pkg}"
            )
        except LookupError:
            nltk.download(pkg, quiet=True)

    return nltk, wordnet

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

# Mots de négation : ne doivent JAMAIS être supprimés/permutés, sous peine
# d'inverser silencieusement le sentiment d'une phrase (bruit d'étiquette).
NEGATION_WORDS = {
    "fr": {"pas", "ne", "n'", "jamais", "aucun", "aucune", "rien", "sans", "ni"},
    "en": {"not", "no", "never", "n't", "none", "without", "nor", "cannot", "can't"},
}


# ---------------------------------------------------------------------------
# Synonym extraction
# ---------------------------------------------------------------------------

def _get_synonyms(word: str, lang: str) -> List[str]:
    """Récupère des synonymes via WordNet dans la langue donnée.
    Robuste : ne plante jamais si WordNet est absent ou corrompu.
    """
    try:
        _, wordnet = _ensure_nltk_data()
        wn_lang = LANG_MAP.get(lang, "eng")
        synonyms = set()

        for syn in wordnet.synsets(word, lang=wn_lang):
            for lemma in syn.lemmas(lang=wn_lang):
                candidate = lemma.name().replace("_", " ")
                if candidate.lower() != word.lower():
                    synonyms.add(candidate)

        return list(synonyms)

    except Exception:
        # WordNet absent, corrompu, ou NLTK en mode zip cassé → on ne casse pas l'entraînement
        logger.debug(f"_get_synonyms : échec WordNet pour '{word}' ({lang}) -> aucun synonyme")
        return []



# ---------------------------------------------------------------------------
# EDA operations
# ---------------------------------------------------------------------------

def synonym_replacement(words: List[str], lang: str, n: int = 1) -> List[str]:
    """Remplace n mots (hors stopwords et négations) par un de leurs synonymes."""
    logger.debug(f"synonym_replacement : début | lang={lang}, n={n}")
    new_words = words.copy()
    protected = STOPWORDS.get(lang, set()) | NEGATION_WORDS.get(lang, set())
    candidates = [w for w in words if w.lower() not in protected]
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

    logger.debug(f"synonym_replacement : terminé | {replaced} remplacement(s)")
    return new_words


def random_insertion(words: List[str], lang: str, n: int = 1) -> List[str]:
    """Insère n synonymes de mots existants à des positions aléatoires."""
    logger.debug(f"random_insertion : début | lang={lang}, n={n}")
    new_words = words.copy()
    protected = STOPWORDS.get(lang, set()) | NEGATION_WORDS.get(lang, set())

    for _ in range(n):
        candidates = [
            w for w in new_words if w.lower() not in protected
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

    logger.debug(f"random_insertion : terminé -> {len(new_words)} mot(s)")
    return new_words


def random_swap(words: List[str], lang: str = "en", n: int = 1) -> List[str]:
    """
    Permute n paires de mots aléatoirement, en évitant de déplacer les
    mots de négation (ex: "pas", "not") pour ne pas casser leur portée
    syntaxique et inverser le sentiment de la phrase.
    """
    logger.debug(f"random_swap : début | lang={lang}, n={n}")
    new_words = words.copy()
    length = len(new_words)
    protected = NEGATION_WORDS.get(lang, set())

    if length < 2:
        return new_words

    swappable_idx = [i for i, w in enumerate(new_words) if w.lower() not in protected]
    if len(swappable_idx) < 2:
        return new_words

    for _ in range(n):
        idx1, idx2 = random.sample(swappable_idx, 2)
        new_words[idx1], new_words[idx2] = new_words[idx2], new_words[idx1]

    logger.debug(f"random_swap : terminé -> {len(new_words)} mot(s)")
    return new_words


def random_deletion(words: List[str], lang: str = "en", p: float = 0.1) -> List[str]:
    """
    Supprime chaque mot avec une probabilité p (garde au moins 1 mot).
    Les mots de négation sont protégés pour ne pas inverser le sentiment.
    """
    logger.debug(f"random_deletion : début | lang={lang}, p={p}")
    if len(words) == 1:
        return words

    protected = NEGATION_WORDS.get(lang, set())
    new_words = [
        w for w in words
        if w.lower() in protected or random.random() > p
    ]
    if not new_words:
        return [random.choice(words)]

    logger.debug(f"random_deletion : terminé -> {len(new_words)} mot(s)")
    return new_words


# ---------------------------------------------------------------------------
# Back Translation (BT) : FR→EN puis EN→FR via Helsinki-NLP/opus-mt
# ---------------------------------------------------------------------------

# Cache global des pipelines de traduction (chargés une seule fois par process).
_TRANSLATION_PIPELINES = {}

# Modèles Helsinki-NLP/opus-mt par paire de langues
_MT_MODELS = {
    ("fr", "en"): "Helsinki-NLP/opus-mt-fr-en",
    ("en", "fr"): "Helsinki-NLP/opus-mt-en-fr",
}


def _get_translation_pipeline(src_lang: str, tgt_lang: str):
    """Charge (et met en cache) le modèle opus-mt demandé.

    transformers >= 5 a supprimé le pipeline "translation" (tâche générique
    et tâches "translation_xx_to_yy") : on charge directement
    AutoModelForSeq2SeqLM + AutoTokenizer et on expose un wrapper avec la
    même interface que l'ancien pipeline (pipe(text)[0]["translation_text"]).
    """
    key = (src_lang, tgt_lang)
    if key not in _TRANSLATION_PIPELINES:
        model_name = _MT_MODELS.get(key)
        if model_name is None:
            raise ValueError(
                f"Pas de modèle opus-mt pour la paire {src_lang}->{tgt_lang} "
                f"(paires supportées : fr->en, en->fr)."
            )
        logger.info(f"Chargement du modèle de traduction {model_name}...")

        try:
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:  # pragma: no cover - dépendance user-facing
            raise ImportError(
                "transformers est requis pour la back-translation. "
                "Installez-le avec `pip install transformers torch sentencepiece`."
            ) from exc

        tokenizer = AutoTokenizer.from_pretrained(model_name)
        model = AutoModelForSeq2SeqLM.from_pretrained(model_name)
        model.eval()

        class _TranslationPipeline:
            """Mini-pipeline compatible avec l'API pipe(text)[0]['translation_text']."""

            def __call__(self, text: str, **kwargs) -> list:
                inputs = tokenizer(
                    text, return_tensors="pt", truncation=True, max_length=512
                )
                with __import__("torch").no_grad():
                    output_ids = model.generate(**inputs)
                translated = tokenizer.decode(
                    output_ids[0], skip_special_tokens=True
                )
                return [{"translation_text": translated}]

        _TRANSLATION_PIPELINES[key] = _TranslationPipeline()
    return _TRANSLATION_PIPELINES[key]


def _has_negation(text: str, lang: str) -> bool:
    """True si le texte contient un mot de négation de la langue donnée."""
    words = set(re.findall(r"\w+|n'", text.lower(), re.UNICODE))
    return bool(words & NEGATION_WORDS.get(lang, set()))


def back_translation(text: str, lang: str = "fr") -> str:
    """
    Augmente le texte par back-translation : traduction FR→EN puis EN→FR.

    Pour un texte anglais (`lang="en"`), le chemin inverse est appliqué
    (EN→FR puis FR→EN).

    Protections contre les inversions de sentiment :
      - les textes trop courts (< 3 mots) sont retournés inchangés (la
        traduction machine y est peu fiable) ;
      - la présence de négation est comparée avant/après : si la négation
        disparaît ou apparaît au cours de l'aller-retour (ex. « pas mauvais »
        → « bad »), le texte original est retourné tel quel ;
      - toute erreur (modèle indisponible, offline, etc.) retombe sur le
        texte original : la back-translation ne doit jamais casser un job
        d'entraînement.
    """
    text = text.strip()
    if not text:
        return text

    num_words = len(re.findall(r"\w+", text, re.UNICODE))
    if num_words < 3:
        logger.debug(
            f"back_translation : texte trop court ({num_words} mots) -> inchangé"
        )
        return text

    try:
        if lang == "fr":
            src, pivot = "fr", "en"
        elif lang == "en":
            src, pivot = "en", "fr"
        else:
            logger.warning(
                f"back_translation : langue non supportée '{lang}' -> inchangé"
            )
            return text

        had_negation_src = _has_negation(text, src)

        pipe_fwd = _get_translation_pipeline(src, pivot)
        pipe_bwd = _get_translation_pipeline(pivot, src)

        translated = pipe_fwd(text)[0]["translation_text"]
        back = pipe_bwd(translated)[0]["translation_text"].strip()

        # Protection inversion de sentiment : la négation doit être préservée.
        has_negation_back = _has_negation(back, src)
        if had_negation_src != has_negation_back:
            logger.debug(
                "back_translation : négation non préservée -> texte original retenu"
            )
            return text

        # Sécurité supplémentaire : résultat vide ou identique -> texte original
        if not back or back == text:
            return text

        logger.debug(f"back_translation : terminé -> {back!r}")
        return back

    except Exception:
        logger.warning(
            "back_translation : échec (modèle indisponible, offline, ...) -> "
            "texte original retenu",
            exc_info=True,
        )
        return text


# ---------------------------------------------------------------------------
# Main recomposition function
# ---------------------------------------------------------------------------

def recompose(
    text: str,
    lang: str = "fr",
    num_variants: int = 4,
    alpha: float = 0.1,
    use_back_translation: bool = False,
) -> List[str]:
    """
    Recompose un texte en `num_variants` nouvelles versions.

    Args:
        text: texte source
        lang: "fr" ou "en"
        num_variants: nombre de variantes à générer
        alpha: intensité de l'augmentation (proportion de mots affectés)
        use_back_translation: active l'opération back-translation (FR→EN→FR
            via Helsinki-NLP/opus-mt). Désactivée par défaut (coûteuse : le
            premier appel télécharge les modèles opus-mt). Quand elle est
            activée, la première variante est produite par back-translation,
            les suivantes par les opérations EDA classiques.

    Returns:
        Liste de textes recomposés
    """
    # Tokenisation simple (mots + ponctuation)
    words = re.findall(r"\w+|[^\w\s]", text, re.UNICODE)
    num_words = len(words)
    logger.debug(
        f"recompose : début | {num_words} mots, lang={lang}, "
        f"num_variants={num_variants}, alpha={alpha}, "
        f"use_back_translation={use_back_translation}"
    )
    n = max(1, int(alpha * num_words))

    variants = set()

    if use_back_translation:
        # La back-translation travaille sur le texte brut (pas les tokens) :
        # le premier variant vient de là, le reste via les ops EDA classiques.
        bt = back_translation(text, lang=lang)
        if bt.strip() and bt.strip() != text.strip():
            variants.add(bt.strip())

    operations = [
        lambda w: synonym_replacement(w, lang, n),
        lambda w: random_insertion(w, lang, n),
        lambda w: random_swap(w, lang, n),
        lambda w: random_deletion(w, lang, alpha),
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

    logger.debug(f"recompose : terminé -> {len(variants)} variante(s)")
    return list(variants)


# ---------------------------------------------------------------------------
# Debug mode
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s | %(name)s | %(message)s")

    examples = [
        ("Ce film était vraiment excellent, j'ai adoré chaque instant.", "fr"),
        ("This movie was absolutely terrible, I hated every minute.", "en"),
    ]

    for text, lang in examples:
        logger.info(f"\nOriginal ({lang}): {text}")
        for i, v in enumerate(recompose(text, lang=lang, num_variants=3), 1):
            logger.info(f"  Variante {i}: {v}")
