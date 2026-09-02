# project/tests/test_model_cleanup.py
"""Nettoyage du répertoire experiments/models via DELETE /models/{name}.

Le sanity check comportemental décide de la suppression :
  - version défaillante (untrained / fallback_base_model / illisible) -> 200 ;
  - version active -> 409 ;
  - version saine -> 422 ;
  - version inconnue -> 404.
"""

import json
import os

import pytest
from fastapi.testclient import TestClient

import api  # noqa: F401
from api import app

# Éviter le chargement du modèle réel (lent) : patch global du predictor.
from tests.test_model_sanity import AlternatingPredictor, GoodPredictor, StubPredictor

client = TestClient(app)
AUTH = {"X-API-Key": "test-key"}


@pytest.fixture(autouse=True)
def _skip_real_predictor(monkeypatch):
    """Neutralise le chargement d'un vrai Predictor (torch) : on fournit
    toujours un stub via get_predictor."""
    import api.routes.models as mod

    monkeypatch.setattr(mod, "get_predictor", lambda name=None: StubPredictor())
    return mod


def _make_version(root, name, files=("model.safetensors", "config.json", "tokenizer_config.json")):
    """Crée une version structurellement VALIDE (config.json parsable, tokenizer
    + poids présents) : nécessaire pour atteindre le sanity check dans le DELETE."""
    version_dir = os.path.join(root, name)
    os.makedirs(version_dir, exist_ok=True)
    for fname in files:
        fpath = os.path.join(version_dir, fname)
        if fname == "config.json":
            with open(fpath, "w", encoding="utf-8") as fh:
                json.dump({"architectures": ["DistilBertForSequenceClassification"]}, fh)
        else:
            with open(fpath, "wb") as fh:
                fh.write(b"fake-content")
    return version_dir


def test_delete_broken_model_succeeds(monkeypatch, tmp_path):
    """Version défaillante (labels faux) -> 200 + dossier supprimé."""
    root = str(tmp_path / "models")
    version_dir = _make_version(root, "20260101T000000Z")

    monkeypatch.setattr("api.routes.models.MODEL_ROOT", root)
    monkeypatch.setattr(
        "api.routes.models.get_predictor", lambda name=None: AlternatingPredictor()
    )
    monkeypatch.setattr("api.routes.models.is_active", lambda name: False)

    response = client.delete("/models/20260101T000000Z", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["verdict"] == "fallback_base_model"
    assert not os.path.exists(version_dir)


def test_delete_active_model_refused(monkeypatch, tmp_path):
    """La version active n'est jamais supprimable -> 409."""
    root = str(tmp_path / "models")
    version_dir = _make_version(root, "20260101T000000Z")

    monkeypatch.setattr("api.routes.models.MODEL_ROOT", root)
    monkeypatch.setattr("api.routes.models.is_active", lambda name: True)

    response = client.delete("/models/20260101T000000Z", headers={"X-API-Key": "test-key"})

    assert response.status_code == 409, response.text
    assert os.path.exists(version_dir)


def test_delete_healthy_model_refused(monkeypatch, tmp_path):
    """Un modèle sain (sanity ok) ne peut pas être supprimé -> 422."""
    root = str(tmp_path / "models")
    version_dir = _make_version(root, "20260101T000000Z")

    monkeypatch.setattr("api.routes.models.MODEL_ROOT", root)
    monkeypatch.setattr(
        "api.routes.models.get_predictor", lambda name=None: GoodPredictor()
    )
    monkeypatch.setattr("api.routes.models.is_active", lambda name: False)

    response = client.delete("/models/20260101T000000Z", headers={"X-API-Key": "test-key"})

    assert response.status_code == 422, response.text
    assert os.path.exists(version_dir)


def test_delete_unknown_version_returns_404(monkeypatch, tmp_path):
    monkeypatch.setattr("api.routes.models.MODEL_ROOT", str(tmp_path / "models"))
    monkeypatch.setattr("api.routes.models.is_active", lambda name: False)

    response = client.delete("/models/20990101T000000Z", headers={"X-API-Key": "test-key"})

    assert response.status_code == 404, response.text


def test_delete_invalid_name_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr("api.routes.models.MODEL_ROOT", str(tmp_path / "models"))

    # Nom commençant par ".." -> échoue au regex (422), mais pas de 404
    response = client.delete("/models/..bad", headers={"X-API-Key": "test-key"})
    assert response.status_code == 422, response.text

    # Nom vide / caractère spécial -> 422
    response = client.delete("/models/bad/name", headers={"X-API-Key": "test-key"})
    assert response.status_code == 404, response.text  # FastAPI route matching


def test_delete_broken_directory_allowed(monkeypatch, tmp_path):
    """Un dossier illisible (modèle non chargeable) -> verdict model_unavailable
    et suppression autorisée (cas d'usage principal du nettoyage)."""
    root = str(tmp_path / "models")
    version_dir = _make_version(root, "20260101T000000Z", files=("config.json",))

    from fastapi import HTTPException
    from starlette.status import HTTP_503_SERVICE_UNAVAILABLE

    def fake_get_predictor(name=None):
        raise HTTPException(
            status_code=HTTP_503_SERVICE_UNAVAILABLE, detail="Aucun modèle disponible"
        )

    monkeypatch.setattr("api.routes.models.MODEL_ROOT", root)
    monkeypatch.setattr("api.routes.models.get_predictor", fake_get_predictor)
    monkeypatch.setattr("api.routes.models.is_active", lambda name: False)

    response = client.delete("/models/20260101T000000Z", headers={"X-API-Key": "test-key"})

    assert response.status_code == 200, response.text
    assert response.json()["verdict"] == "model_unavailable"
    assert not os.path.exists(version_dir)


def test_delete_incomplete_version_no_model_load(monkeypatch, tmp_path):
    """Une version incomplète (pas de tokenizer / config vide / poids absents)
    est supprimée immédiatement : SANS get_predictor(), SANS sanity check,
    SANS 500 — verdict « model_unavailable »."""
    import api.routes.models as mod

    root = str(tmp_path / "models")
    monkeypatch.setattr("api.routes.models.MODEL_ROOT", root)
    monkeypatch.setattr("api.routes.models.is_active", lambda name: False)

    # Garde-fou : tout chargement de modèle ou sanity check est un échec de test.
    def _forbidden(*args, **kwargs):
        raise AssertionError("get_predictor / run_model_sanity ne doivent pas être appelés")

    monkeypatch.setattr(mod, "get_predictor", _forbidden)
    monkeypatch.setattr("api.routes.models.run_model_sanity", _forbidden)

    # Pas de tokenizer
    d1 = _make_version(root, "20260101T000001Z", files=("model.safetensors", "config.json"))
    # Config vide ({})
    d2 = os.path.join(root, "20260101T000002Z")
    os.makedirs(d2, exist_ok=True)
    for fname, content in (
        ("tokenizer_config.json", b"fake"),
        ("model.safetensors", b"fake"),
    ):
        with open(os.path.join(d2, fname), "wb") as fh:
            fh.write(content)
    with open(os.path.join(d2, "config.json"), "w", encoding="utf-8") as fh:
        json.dump({}, fh)
    # Aucun fichier de poids
    d3 = _make_version(root, "20260101T000003Z", files=("config.json", "tokenizer_config.json"))

    for name, version_dir in (
        ("20260101T000001Z", d1),
        ("20260101T000002Z", d2),
        ("20260101T000003Z", d3),
    ):
        response = client.delete(f"/models/{name}", headers={"X-API-Key": "test-key"})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["deleted"] is True
        assert body["verdict"] == "model_unavailable"
        assert body["detail"]
        assert not os.path.exists(version_dir)
