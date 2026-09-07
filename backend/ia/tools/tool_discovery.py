"""Découverte et recommandation d'outils. Phase B — flag AGENT_TOOL_ANALYTICS.

Cette brique expose le catalogue réel des outils à une expérience de type
« GitHub Copilot » : à partir d'un besoin exprimé en langage naturel (ou d'un
contexte/dernier résultat), elle classe les outils du registre par pertinence.

Stratégie de scoring (déterministe, sans modèle) :

    1. ``_score_text(query, meta)`` — correspondance lexicale : tokens du
       besoin vs description + nom de l'outil. Une mention exacte du nom ou
       d'un mot-clé synonyme pèse fortement ;
    2. ``suggest_tools(query, k)``   — renvoie les ``k`` outils les mieux
       scorés, triés, chacun avec ``score`` (0..1) et ``reason`` lisible.

Les scores sont NORMALISÉS pour être comparables. La liste ``KEYWORDS``
associe des synonymes métier (fr) à chaque outil pour les formulations non
littérales ; on peut l'enrichir sans casser l'API (clés inconnues ignorées).
"""

import math

from .tool_registry import TOOL_META  # import relatif dual (tests + runtime)


# Synonymes métier (français) par outil : amplifie le score quand le besoin
# contient un de ces mots. Enrichissable librement.
KEYWORDS = {
    "add": {"addition", "somme", "calculer", "total", "opération"},
    "calc": {"calcul", "expression", "math", "évaluer", "opération"},
    "read_file": {"lire", "contenu", "fichier", "affiche", "lister"},
    "write_file": {"écrire", "créer", "enregistrer", "modifier", "sauvegarder"},
    "append_file": {"ajouter", "append", "compléter", "fin"},
    "list_dir": {"répertoire", "dossier", "lister", "contenu", "liste"},
    "find_file": {"chercher", "trouver", "rechercher", "localiser"},
    "search_in_files": {"recherche", "grep", "mot", "contenu", "trouver"},
    "remove_path": {"supprimer", "effacer", "delete", "rm"},
    "copy_path": {"copier", "dupliquer", "cp"},
    "move_path": {"déplacer", "renommer", "mv"},
    "make_dir": {"répertoire", "dossier", "créer", "mkdir"},
    "run_command": {"commande", "bash", "shell", "exécuter", "terminal"},
    "run_python": {"python", "script", "exécuter", "code"},
    "web_search": {"rechercher", "internet", "web", "google", "actualité"},
    "web_fetch": {"page", "url", "récupérer", "html"},
    "http_get": {"requête", "get", "api", "endpoint"},
    "http_post": {"post", "envoyer", "formulaire", "api"},
    "download_file": {"télécharger", "download"},
    "gpu_info": {"gpu", "carte", "nvidia", "cuda"},
    "disk_usage": {"disque", "espace", "stockage", "df"},
    "env_info": {"environnement", "variable", "env", "système"},
    "now": {"heure", "date", "temps", "horaire"},
    "dataset_stats": {"dataset", "données", "analyse", "statistiques"},
    "start_training": {"entraîner", "training", "lancer", "fine-tune"},
    "train_model": {"entraîner", "training", "modèle"},
    "predict_sentiment": {"sentiment", "prédire", "classification", "avis"},
    "sqlite_query": {"sql", "base", "requête", "sqlite"},
    "postgres_query": {"sql", "postgres", "base", "requête"},
    "git_status": {"git", "état", "statut"},
    "git_log": {"git", "historique", "log", "commits"},
    "git_diff": {"git", "diff", "différence", "modifications"},
    "docker_ps": {"docker", "conteneur", "processus"},
    "docker_logs": {"docker", "logs", "journal"},
}


def _tokenize(text: str) -> set[str]:
    """Tokens d'un texte : minuscules, sans ponctuation ni accents forts."""
    out: set[str] = set()
    for chunk in (text or "").lower().replace("_", " ").replace("'", " ").replace("’", " ").split():
        word = chunk.strip(".,;:!?()[]{}<>\"'’")
        if word:
            out.add(word)
    return out


def _raw_tokens(text: str) -> set[str]:
    """Tokens bruts (underscores préservés) : permet de reconnaître les noms
    d'outils exacts comme ``read_file`` dans le besoin."""
    out: set[str] = set()
    for chunk in (text or "").lower().split():
        word = chunk.strip(".,;:!?()[]{}<>\"'’")
        if word:
            out.add(word)
    return out


def _fuzzy_hit(q_tokens: set[str], term: str) -> bool:
    """Correspondance souple : gère flexions (« évalue »/« évaluer ») par
    préfixe commun pour les termes suffisamment longs (>= 5 caractères)."""
    if term in q_tokens:
        return True
    if len(term) >= 5:
        return any(t.startswith(term) or term.startswith(t) for t in q_tokens if len(t) >= 5)
    return False


def _score_text(query: str, name: str, description: str) -> tuple[float, list[str]]:
    """Score lexical d'un outil pour un besoin donne + raisons (0..1)."""
    q_tokens = _tokenize(query)
    raw = _raw_tokens(query)
    if not q_tokens:
        return 0.0, []
    reasons: list[str] = []

    # 1. Mention exacte du nom de l'outil (underscore conservé) : tres discriminant.
    name_hit = name.lower() in raw
    if name_hit:
        reasons.append(f"nom « {name} »")

    # 2. Un terme de la description figure-t-il dans le besoin ?
    desc_tokens = _tokenize(description) | {name.lower()}
    hits = q_tokens & desc_tokens
    reasons.extend(f"terme « {t} »" for t in sorted(hits))

    # 3. Synonymes metier (avec flexions : évalue/évaluer, liste/lister...).
    kw = KEYWORDS.get(name, set())
    kw_hits = {k for k in kw if _fuzzy_hit(q_tokens, k)}
    reasons.extend(f"synonyme « {t} »" for t in sorted(kw_hits))

    # Ponderation (nom=2, description=1, synonyme=1.5) normalisee par la taille
    # du besoin pour ne pas avantager les requetes tres longues.
    raw_score = (2.0 if name_hit else 0.0) + 1.0 * len(hits) + 1.5 * len(kw_hits)
    score = min(1.0, raw_score / max(2.0, math.sqrt(len(q_tokens))))
    return score, reasons


def suggest_tools(query: str, k: int = 5, meta=None) -> list[dict]:
    """Classe le catalogue par pertinence pour un besoin ``query``.

    ``meta`` (optionnel) : mapping nom -> metadonnees. Par defaut le registre
    ``TOOL_META``. Retourne les ``k`` meilleurs ::

        [{"tool": str, "score": float, "reasons": [str, ...]}, ...]

    Seuls les outils a score > 0 sont renvoyes (jamais de recommandation a
    vide si rien ne correspond).
    """
    meta = meta if meta is not None else TOOL_META
    k = max(1, int(k))
    scored: list[tuple[float, str, list[str]]] = []
    for name, m in meta.items():
        desc = m.get("description") or ""
        score, reasons = _score_text(query, name, desc)
        if score > 0:
            scored.append((score, name, reasons))
    scored.sort(key=lambda t: (-t[0], t[1]))
    return [
        {"tool": name, "score": round(score, 3), "reasons": reasons[:4]}
        for score, name, reasons in scored[:k]
    ]


def suggest_tools_for_result(outcome: str, k: int = 5) -> list[dict]:
    """Recommandations « de suite » a partir d'un resultat/etat d'outil."""
    return suggest_tools(outcome or "", k=k)
