"""Client HTTP vers le LLM (Ollama) avec journalisation complète.

Chaque appel est tracé dans les logs :
    INFO  : départ de l'appel (modèle, URL, nb de messages, température)
            puis réception (durée, taille de la réponse) ;
    DEBUG : aperçu de la réponse brute ;
    ERROR : timeout / connexion impossible / erreur HTTP / réponse illisible,
            avant de relancer l'exception (la traduction en codes HTTP reste
            dans core/agent_cache.py : Timeout -> 504, ConnectionError -> 502).
"""

import logging
import os
import time

import requests

# Température basse par défaut : réduit drastiquement les sorties hors-format
# (prose autour du JSON) et les hallucinations de contenu. Surchargeable via
# la variable d'environnement AGENT_LLM_TEMPERATURE.
DEFAULT_TEMPERATURE = float(os.getenv("AGENT_LLM_TEMPERATURE", "0.2"))

logger = logging.getLogger(__name__)


class LLMClient:
    def __init__(self, url, model, timeout=None, temperature=None):
        self.url = url
        self.model = model
        # Timeout en secondes pour requests.post ; None = comportement historique
        # (attente indéfinie), utilisé par ia/main.py.
        self.timeout = timeout
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else float(temperature)

    def call(self, messages):
        logger.info(
            "Appel LLM '%s' sur %s (%d message(s), température=%.2f)...",
            self.model,
            self.url,
            len(messages),
            self.temperature,
        )
        started = time.perf_counter()
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False,
                    "options": {"temperature": self.temperature},
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            content = resp.json()["message"]["content"]
        except requests.exceptions.Timeout:
            logger.error(
                "Timeout du LLM '%s' après %.1fs (limite : %s s) sur %s.",
                self.model,
                time.perf_counter() - started,
                self.timeout,
                self.url,
            )
            raise
        except requests.exceptions.ConnectionError as exc:
            logger.error("LLM injoignable sur %s : %s", self.url, exc)
            raise
        except requests.exceptions.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "?"
            logger.error("Le LLM '%s' a renvoyé une erreur HTTP %s.", self.model, status)
            raise
        except (KeyError, TypeError, ValueError) as exc:
            logger.error("Réponse illisible du LLM '%s' (%s).", self.model, exc)
            raise

        logger.info(
            "Réponse du LLM '%s' reçue en %.2fs (%d caractères).",
            self.model,
            time.perf_counter() - started,
            len(content),
        )
        logger.debug("Réponse brute du LLM : %.500s", content)
        return content
