# project/tests/test_api_explain.py

"""Tests offline de l'endpoint POST /explain.

Aucun appel réseau : le runner OpenRouter est remplacé par un runner adossé à
un FakeLLM scripté (via `core.agent_cache._build_openrouter_runner`), et la
prédiction DistilBERT est remplacée par un FakePredictor (patché sur
`api._get_predictor`).

Couvert : succès (contrat JSON), contexte injecté dans le prompt, transmission
du modèle OpenRouter, auth requise, validation du corps.

Lance avec : pytest tests/test_api_explain.py -v
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import api  # noqa: E402
from core import agent_cache  # noqa: E402  (insère ia/ dans sys.path)
from api import app  # noqa: E402

HEADERS = {"X-API-Key": "test-key"}

client = TestClient(app)

EXPLANATION = (
    "Le texte emploie des termes très positifs comme « super » et « fantastique », "
    "ce qui explique la prédiction positive avec une confiance élevée."
)


class FakeLLM:
    """Remplace LLMClient (même pattern que tests/test_api_ai_chat.py).

    Réponses scriptées via `replies` (dépilées une par une) ; en l'absence de
    réponse scriptée, renvoie une explication par défaut. `calls` conserve les
    messages reçus pour pouvoir vérifier le prompt transmis.
    """

    def __init__(self):
        self.calls: list[list[dict]] = []
        self.replies: list[str] = []

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        if self.replies:
            return self.replies.pop(0)
        return EXPLANATION


class FakePredictor:
    """Remplace le prédicteur DistilBERT (même contrat que core.predictor_cache)."""

    def __init__(self, sentiment="positive", confidence=0.97):
        self.sentiment = sentiment
        self.confidence = confidence

    def predict(self, texts):
        return [{"text": t, "sentiment": self.sentiment, "confidence": self.confidence} for t in texts]


@pytest.fixture()
def openrouter_llm(monkeypatch):
    """Injecte un runner OpenRouter adossé à un FakeLLM (restauré après le test).

    Monkeypatche ``ask_agent_openrouter`` pour que POST /explain utilise le
    LLM factice sans aucun appel réseau, tout en enregistrant les modèles
    OpenRouter demandés (`request_models`).
    """
    llm = FakeLLM()
    request_models = []

    def fake_ask_openrouter(prompt, model=None):
        request_models.append(model)
        runner = agent_cache.AgentRunner(agent_cache.AgentCore(llm))
        return runner.ask_detailed(prompt).answer

    monkeypatch.setattr(agent_cache, "ask_agent_openrouter", fake_ask_openrouter)
    llm.request_models = request_models
    return llm


@pytest.fixture()
def fake_predictor(monkeypatch):
    """Patch api._get_predictor pour éviter tout chargement de DistilBERT."""
    predictor = FakePredictor()
    monkeypatch.setattr(api, "_get_predictor", lambda model=None: predictor)
    return predictor


# --- POST /explain ------------------------------------------------------------


def test_explain_success(openrouter_llm, fake_predictor):
    """Contrat de sortie : {sentiment, confidence, explanation}."""
    resp = client.post("/explain", json={"text": "ce produit est super"}, headers=HEADERS)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert set(body) == {"sentiment", "confidence", "explanation"}
    assert body["sentiment"] == "positive"
    assert body["confidence"] == 0.97
    assert body["explanation"]
    # L'agent IA (OpenRouter) a bien été sollicité (au moins un appel LLM).
    assert openrouter_llm.calls


def test_explain_forwards_context_to_agent(openrouter_llm, fake_predictor):
    """Le texte ET la prédiction DistilBERT sont injectés dans le prompt LLM."""
    client.post(
        "/explain", json={"text": "ce produit est super"}, headers=HEADERS
    )
    # Dernier message utilisateur reçu par le LLM = prompt construit.
    prompt = openrouter_llm.calls[-1][-1]["content"]
    assert "ce produit est super" in prompt
    assert "positive" in prompt
    assert "0.97" in prompt


def test_explain_forwards_model_to_openrouter(openrouter_llm, fake_predictor):
    """Le champ optionnel `model` est transmis comme modèle OpenRouter."""
    client.post(
        "/explain",
        json={"text": "bonjour", "model": "openrouter/free"},
        headers=HEADERS,
    )
    assert openrouter_llm.request_models == ["openrouter/free"]


def test_explain_defaults_to_openrouter_free(openrouter_llm, fake_predictor):
    """Sans `model`, le modèle OpenRouter par défaut est utilisé."""
    client.post("/explain", json={"text": "bonjour"}, headers=HEADERS)
    assert openrouter_llm.request_models == [None]


def test_explain_requires_api_key(openrouter_llm, fake_predictor):
    """Auth requise : sans en-tête X-API-Key -> 401."""
    resp = client.post("/explain", json={"text": "bonjour"})
    assert resp.status_code == 401


def test_explain_missing_text_validation(openrouter_llm, fake_predictor):
    """Validation du corps : absence de `text` -> 422."""
    resp = client.post("/explain", json={}, headers=HEADERS)
    assert resp.status_code == 422

    # texte vide également refusé par la contrainte min_length.
    resp = client.post("/explain", json={"text": ""}, headers=HEADERS)
    assert resp.status_code == 422