"""Configuration centralisée des LOGS — affichage terminal (agent IA, pipeline…).

Pourquoi ce module ?
    Les loggers utilisés dans ia/ (`agent.*`, `tools.*`) ou core/
    (`thinktuning.pipeline_runner`) n'ont aucun handler par défaut : sans
    configuration, Python n'affiche que les WARNING+ via son handler « last
    resort ». Ces fonctions branchent UN handler console unique, coloré et
    lisible, qui affiche dans le terminal :

        - l'heure, le niveau (coloré selon sa gravité) et le nom du logger ;
        - les durées mesurées par l'agent (appels LLM, exécution des tools) ;
        - les étapes du pipeline (labeling → filtering → finetuning) ;
        - des tracebacks complets et mis en forme (rich) en cas d'échec.

    Utilisable depuis :
        - api/main.py           -> logs API + agent dans le terminal uvicorn ;
        - pipeline.py           -> logs des étapes du pipeline end-to-end (CLI) ;
        - n'importe quel script -> setup_logging() (ou setup_agent_logging())
          avant de journaliser ;
        - directement en démo   -> python ia/logging_setup.py

Niveau de log : variable d'environnement AGENT_LOG_LEVEL (DEBUG/INFO/WARNING/
ERROR), INFO par défaut. Idempotent : ré-appeler la fonction ne duplique
jamais les lignes (les handlers précédemment posés par elle sont retirés).

Le rendu repose sur `rich` (déjà requis par requirements.txt) ; si la
bibliothèque manque, repli sur un formateur ANSI/colorama équivalent.
"""

import logging
import os
import sys

DEFAULT_LOG_LEVEL = "INFO"

try:  # rich est présent dans requirements.txt ; le repli reste possible.
    from rich.console import Console
    from rich.logging import RichHandler
except ImportError:  # pragma: no cover - environnement sans rich
    Console = None
    RichHandler = None

# Marqueur posé sur NOS handlers console : permet de rester idempotent sans
# toucher aux handlers d'uvicorn ou de pytest.
_HANDLER_MARKER = "_thinktuning_agent_console"


class _AnsiColorFormatter(logging.Formatter):
    """Repli sans rich : colore la ligne entière selon son niveau."""

    COLORS = {
        "DEBUG": "\033[36m",        # cyan
        "INFO": "\033[32m",         # vert
        "WARNING": "\033[33m",      # jaune
        "ERROR": "\033[31m",        # rouge
        "CRITICAL": "\033[97;41m",  # blanc sur fond rouge
    }
    RESET = "\033[0m"

    def __init__(self):
        super().__init__(
            fmt="%(asctime)s | %(levelname)-8s | %(name)s:%(lineno)d | %(message)s",
            datefmt="%H:%M:%S",
        )
        try:  # Active les séquences ANSI sous l'ancien terminal Windows.
            import colorama

            colorama.just_fix_windows_console()
        except Exception:  # pragma: no cover - colorama absent
            pass

    def format(self, record):
        message = super().format(record)
        color = self.COLORS.get(record.levelname)
        return f"{color}{message}{self.RESET}" if color else message


def _resolve_level(level=None) -> int:
    """Niveau numérique : argument explicite, sinon AGENT_LOG_LEVEL, sinon INFO."""
    name = str(level or os.getenv("AGENT_LOG_LEVEL") or DEFAULT_LOG_LEVEL).upper()
    return getattr(logging, name, logging.INFO)


def _build_console_handler():
    """Handler console : RichHandler si disponible, sinon StreamHandler ANSI."""
    if RichHandler is not None:
        handler = RichHandler(
            console=Console(),
            show_time=True,
            show_level=True,
            show_path=False,
            rich_tracebacks=True,
            markup=False,  # les crochets des messages restent littéraux
        )
        # Seul le message passe dans le formatter : rich ajoute lui-même
        # l'heure, le niveau coloré et le nom du logger dans sa colonne.
        handler.setFormatter(logging.Formatter("%(message)s"))
    else:  # pragma: no cover - environnement sans rich
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(_AnsiColorFormatter())
    return handler


def setup_logging(level=None):
    """Branche le handler console coloré sur le logger racine.

    À appeler UNE fois au démarrage (api/main.py le fait déjà ; pipeline.py
    l'appelle aussi). Le niveau peut être passé explicitement (prioritaire) ou
    venir d'AGENT_LOG_LEVEL. Retourne le handler installé (pratique pour les
    tests).
    """
    numeric_level = _resolve_level(level)

    root = logging.getLogger()
    root.setLevel(numeric_level)

    # Idempotence : retire NOS anciens handlers (reload uvicorn, rappels dans
    # les tests) pour ne jamais afficher deux fois la même ligne.
    for existing in list(root.handlers):
        if getattr(existing, _HANDLER_MARKER, False):
            root.removeHandler(existing)

    handler = _build_console_handler()
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)

    # Bibliothèques tierces bavardes : jamais plus verbeuses que WARNING,
    # quel que soit le niveau demandé pour l'application.
    for noisy in ("urllib3", "httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))

    return handler


# Alias conservé pour compatibilité (api/main.py et les tests l'utilisent).
# Le comportement est identique : même handler console, niveau via
# AGENT_LOG_LEVEL par défaut.
setup_agent_logging = setup_logging


if __name__ == "__main__":  # démonstration du rendu terminal : python ia/logging_setup.py
    setup_agent_logging("DEBUG")
    demo = logging.getLogger("demo.agent")

    demo.debug("Message DEBUG : détail technique (activé via AGENT_LOG_LEVEL=DEBUG).")
    demo.info("=== Nouveau run de l'agent | prompt : « affiche le README »")
    demo.info("Tour 1/3 : appel du LLM 'llama3.1:8b' sur http://localhost:11434/api/chat...")
    demo.info("Tour 1/3 : réponse du LLM reçue en 0.42s.")
    demo.info('Exécution du tool \'read_file\' | arguments : {"path": "README.md"}')
    demo.info("Tool 'read_file' terminé en 0.01s | résultat : « # ThinkTuning — projet… »")
    demo.warning("Auto-correction (tour 1/3) : Tool inconnu : 'division'.")
    try:
        raise RuntimeError("connexion refusée (exemple de traceback)")
    except RuntimeError:
        demo.error("Échec de l'outil 'postgres_query' :", exc_info=True)
    demo.info("Réponse finale générée (128 caractères) | durée totale du run : 1.37s.")