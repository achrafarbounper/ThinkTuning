"""Tests E2E des routes classifieurs (Phase 5) : monitoring + prédiction.

Via TestClient (aucun modèle lourd), avec des classifieurs FACTICES
enregistrés dans le registre singleton, on couvre :
  - ``GET /classifiers`` : liste + synthèse de santé ;
  - ``GET /classifiers/{name}`` : instantané (info, métriques, health) ;
  - défense : un classifieur qui lève ne fait pas tomber le snapshot ;
  - ``POST /classifiers/{name}/predict`` : prédiction (ordre préservé),
    garde-fous d'entrée, 401 sans clé, 404 inconnu ;
  - ``POST /classifiers/{name}/reload`` : rechargement ;
  - le classifieur ``intent`` réel retombe sur les règles (aucun modèle
    entraîné → ``chat``/``action``) sans charger torch.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from core.classifier_registry import get_registry, reset_registry
from ia.agent.classifiers.base import BaseClassifier, PredictionResult

API_KEY = "test-key"
HEADERS = {"X-API-Key": API_KEY}


class _StubClassifier(BaseClassifier):
    """Classifieur factice : réponses fixes, trace les appels."""

    def __init__(self, name: str = "demo", fail_health: bool = False) -> None:
        self.name = name
        self.fail_health = fail_health
        self.reload_calls = 0
        self.predict_calls = 0

    def load_model(self) -> None:
        return None

    def reload(self) -> None:
        self.reload_calls += 1

    def predict(self, texts: list[str]) -> list[PredictionResult]:
        self.predict_calls += 1
        return [
            PredictionResult(text=text, label="positive", confidence=0.93)
            for text in texts
        ]

    def get_model_info(self) -> dict:
        return {"name": self.name, "engine": "stub", "labels": ["positive"]}

    def get_metrics(self) -> dict:
        return {"name": self.name, "predictions": self.predict_calls}

    def health_check(self) -> dict:
        if self.fail_health:
            raise RuntimeError("santé indisponible")
        return {"ok": True, "label": "positive"}


@pytest.fixture(autouse=True)
def _registre_propre():
    """Registre singleton vide avant/après chaque test."""
    reset_registry()
    yield
    reset_registry()


@pytest.fixture(scope="module")
def client():
    from api import app

    with TestClient(app) as c:
        yield c
class TestListClassifiers:
    def test_liste_et_synthese(self, client) -> None:
        get_registry().register(_StubClassifier("demo"))
        response = client.get("/classifiers")
        assert response.status_code == 200, response.text
        payload = response.json()
        names = [c["name"] for c in payload["classifiers"]]
        assert "demo" in names
        assert payload["summary"]["status"] == "ok"
        assert payload["summary"]["healthy"] == 1

    def test_synthese_degradee(self, client) -> None:
        get_registry().register(_StubClassifier("ok"))
        get_registry().register(_StubClassifier("hs", fail_health=True))
        payload = client.get("/classifiers").json()
        assert payload["summary"]["status"] == "degraded"
        by_name = {c["name"]: c for c in payload["classifiers"]}
        assert by_name["hs"]["health"]["error"]


class TestGetClassifier:
    def test_instantané_complet(self, client) -> None:
        get_registry().register(_StubClassifier("demo"))
        response = client.get("/classifiers/demo")
        assert response.status_code == 200
        snap = response.json()
        assert snap["info"]["engine"] == "stub"
        assert snap["metrics"]["predictions"] == 0
        assert snap["health"]["ok"] is True
        assert "snapshot_at_ms" in snap

    def test_inconnu_renvoie_404(self, client) -> None:
        assert client.get("/classifiers/inexistant").status_code == 404

    def test_fabrique_intent_cree_le_classifieur(self, client) -> None:
        # L'accès crée le classifieur intent (aucun modèle : repli règles).
        assert client.get("/classifiers/intent").status_code == 200


class TestPredictClassifier:
    def test_predict_ordre_preserve(self, client) -> None:
        get_registry().register(_StubClassifier("demo"))
        response = client.post(
            "/classifiers/demo/predict",
            json={"texts": ["un", "deux", "trois"]},
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert [r["text"] for r in results] == ["un", "deux", "trois"]
        assert all(r["label"] == "positive" for r in results)
        assert all(r["confidence"] == pytest.approx(0.93) for r in results)

    def test_sans_cle_api_401(self, client) -> None:
        get_registry().register(_StubClassifier("demo"))
        response = client.post(
            "/classifiers/demo/predict", json={"texts": ["un"]}
        )
        assert response.status_code == 401

    def test_textes_vides_422(self, client) -> None:
        get_registry().register(_StubClassifier("demo"))
        response = client.post(
            "/classifiers/demo/predict", json={"texts": []}, headers=HEADERS
        )
        assert response.status_code == 422

    def test_classifieur_inconnu_404(self, client) -> None:
        response = client.post(
            "/classifiers/inconnu/predict",
            json={"texts": ["un"]},
            headers=HEADERS,
        )
        assert response.status_code == 404

    def test_intent_reel_bascule_sur_regles(self, client) -> None:
        """Sans modèle entraîné, intent prédit action/chat via les règles."""
        response = client.post(
            "/classifiers/intent/predict",
            json={
                "texts": [
                    "Peux-tu lancer l'entraînement du modèle ?",
                    "Merci beaucoup pour ton aide",
                ]
            },
            headers=HEADERS,
        )
        assert response.status_code == 200, response.text
        results = response.json()["results"]
        assert [r["label"] for r in results] == ["action", "chat"]


class TestReloadClassifier:
    def test_reload_appelle_et_renvoie_info(self, client) -> None:
        stub = _StubClassifier("demo")
        get_registry().register(stub)

        response = client.post("/classifiers/demo/reload", headers=HEADERS)
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "reloaded"
        assert payload["model_info"]["engine"] == "stub"
        assert stub.reload_calls == 1

    def test_reload_sans_cle_401(self, client) -> None:
        get_registry().register(_StubClassifier("demo"))
        assert (
            client.post("/classifiers/demo/reload").status_code == 401
        )
