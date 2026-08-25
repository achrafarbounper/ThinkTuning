import os

import requests

# Température basse par défaut : réduit drastiquement les sorties hors-format
# (prose autour du JSON) et les hallucinations de contenu. Surchargeable via
# la variable d'environnement AGENT_LLM_TEMPERATURE.
DEFAULT_TEMPERATURE = float(os.getenv("AGENT_LLM_TEMPERATURE", "0.2"))


class LLMClient:
    def __init__(self, url, model, timeout=None, temperature=None):
        self.url = url
        self.model = model
        # Timeout en secondes pour requests.post ; None = comportement historique
        # (attente indéfinie), utilisé par ia/main.py.
        self.timeout = timeout
        self.temperature = DEFAULT_TEMPERATURE if temperature is None else float(temperature)

    def call(self, messages):
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
        return resp.json()["message"]["content"]
