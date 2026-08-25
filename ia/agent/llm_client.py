import requests


class LLMClient:
    def __init__(self, url, model, timeout=None):
        self.url = url
        self.model = model
        # Timeout en secondes pour requests.post ; None = comportement historique
        # (attente indéfinie), utilisé par ia/main.py.
        self.timeout = timeout

    def call(self, messages):
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
        return resp.json()["message"]["content"]
