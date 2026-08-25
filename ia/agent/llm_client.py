"""Client HTTP minimaliste vers l'endpoint chat Ollama, avec logs structurés.

Les événements sont publiés sur le logger « thinktuning.agent » (même canal
que AgentCore) : requête/durée/statut en INFO, contenus complets en DEBUG,
erreurs réseau en ERROR. Le niveau se règle via AGENT_LOG_LEVEL.
"""

import logging
import os
import time

import requests

logger = logging.getLogger("thinktuning.agent")
logger.setLevel(os.getenv("AGENT_LOG_LEVEL", "INFO").upper())

# Température appliquée quand l'appelant n'en fournit pas explicitement
# (0.8 = défaut historique du serveur Ollama).
DEFAULT_TEMPERATURE = 0.8


class LLMClient:
    def __init__(self, url, model, timeout=None, temperature=None):
        self.url = url
        self.model = model
        # Timeout en secondes pour requests.post ; None = comportement historique
        # (attente indéfinie), utilisé par ia/main.py.
        self.timeout = timeout
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else float(temperature)

    def call(self, messages):
        started = time.perf_counter()
        logger.info(
            "llm_request url=%s model=%s messages=%d timeout=%s",
            self.url,
            self.model,
            len(messages),
            self.timeout,
        )
        try:
            resp = requests.post(
                self.url,
                json={
                    "model": self.model,
                    "messages": messages,
                    "stream": False
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error(
                "llm_timeout url=%s model=%s elapsed_ms=%.0f timeout=%s",
                self.url,
                self.model,
                (time.perf_counter() - started) * 1000,
                self.timeout,
            )
            raise
        except requests.exceptions.HTTPError:
            status = resp.status_code if resp is not None else "?"
            logger.error("llm_http_error url=%s status=%s", self.url, status)
            raise
        except requests.exceptions.RequestException:
            logger.exception("llm_connection_error url=%s", self.url)
            raise

        content = resp.json()["message"]["content"]
        logger.info(
            "llm_response status=%d elapsed_ms=%.0f content_chars=%d",
            resp.status_code,
            (time.perf_counter() - started) * 1000,
            len(content),
        )
        logger.debug("llm_response_content=%s", content)
        return content
