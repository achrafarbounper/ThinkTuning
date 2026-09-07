# project/tests/test_api_v1_models.py
"""Tests des endpoints « modèles » v1 (Phase 3d-3).

Deux niveaux :
    - routes : ports substitués via ``app.dependency_overrides`` — AUCUNE
      infrastructure, statuts + enveloppe domaine {error:{code,message}} ;
    - adaptateur : handlers legacy enveloppés avec monkeypatchs ciblés
      (``api.routes.models.*``) pour vérifier la conversion HTTPException ->
      erreurs de domaine (422/404/409) sans toucher au disque réel.
"""

import os

os.environ.setdefault("API_KEY", "test-key")

import pytest
from fastapi.testclient import TestClient

import api  # noqa: F401
from api import app
from api.dependencies.composition import get_model_versioning_port
from app.domain.errors import ConflictError, NotFoundError, ValidationError
from app.infrastructure.ml.model_versioning_adapter import ModuleModelVersioningAdapter
from core.models import ModelVersion

client = TestClient(app)

AUTH = {"X-API-Key": "test-key"}


# ---------------------------------------------------------------------------
# Fake du port (aucune infrastructure)
# ---------------------------------------------------------------------------
class FakeVersioning:
    def __init__(self) -> None:
        self.versions: list[ModelVersion] = []
        self.pointer: dict = {"activated": False}
        self.activate_result: dict = {}
        self.activate_error: Exception | None = None
        self.delete_result: dict = {}
        self.delete_error: Exception | None = None
        self.activated: list[str] = []
        self.deleted: list[str] = []

    def list_details(self) -> list[ModelVersion]:
        return list(self.versions)

    def active_pointer(self) -> dict:
        return dict(self.pointer)

    def activate(self, name: str) -> dict:
        if self.activate_error is not None:
            raise self.activate_error
        self.activated.append(name)
        return self.activate_result

    def delete(self, name: str) -> dict:
        if self.delete_error is not None:
            raise self.delete_error
        self.deleted.append(name)
        return self.delete_result


@pytest.fixture
def fake_versioning():
    fake = FakeVersioning()
    app.dependency_overrides[get_model_versioning_port] = lambda: fake
    yield fake
    app.dependency_overrides.pop(get_model_versioning_port, None)


# ---------------------------------------------------------------------------
# GET /api/v1/models/details et /api/v1/models/active
# ---------------------------------------------------------------------------
def test_v1_models_details_200(fake_versioning):
    fake_versioning.versions = [
        ModelVersion(name="20260905", path="/x/20260905", created_at=1.0, active=True),
        ModelVersion(name="20260901", path="/x/20260901", created_at=0.5, active=False),
    ]

    response = client.get("/api/v1/models/details", headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert [v["name"] for v in body] == ["20260905", "20260901"]
    assert body[0]["active"] is True


def test_v1_models_details_empty_200(fake_versioning):
    """Aucun modèle entraîné : 200 + [] (parité legacy, pas de 500)."""
    response = client.get("/api/v1/models/details", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_v1_models_details_requires_api_key():
    response = client.get("/api/v1/models/details")

    assert response.status_code == 401, response.text


def test_v1_models_active_pointer(fake_versioning):
    fake_versioning.pointer = {"activated": True, "version": "20260905"}

    response = client.get("/api/v1/models/active", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {"activated": True, "version": "20260905"}


def test_v1_models_active_none(fake_versioning):
    response = client.get("/api/v1/models/active", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {"activated": False}


# ---------------------------------------------------------------------------
# POST /api/v1/models/{name}/activate
# ---------------------------------------------------------------------------
def test_v1_models_activate_200(fake_versioning):
    fake_versioning.activate_result = {"activated": True, "version": "20260905"}

    response = client.post("/api/v1/models/20260905/activate", headers=AUTH)

    assert response.status_code == 200, response.text
    assert response.json() == {"activated": True, "version": "20260905"}
    assert fake_versioning.activated == ["20260905"]


def test_v1_models_activate_invalid_artifacts_422(fake_versioning):
    fake_versioning.activate_error = ValidationError(
        "Tête de classification non entraînée (std <= 0.03)"
    )

    response = client.post("/api/v1/models/bad/activate", headers=AUTH)

    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == "validation_error"


def test_v1_models_activate_unknown_404(fake_versioning):
    fake_versioning.activate_error = NotFoundError("Version de modèle inconnue : nope")

    response = client.post("/api/v1/models/nope/activate", headers=AUTH)

    assert response.status_code == 404, response.text
    assert response.json()["error"]["code"] == "not_found"


def test_v1_models_activate_requires_api_key():
    response = client.post("/api/v1/models/x/activate")

    assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# DELETE /api/v1/models/{name}
# ---------------------------------------------------------------------------
def test_v1_models_delete_200(fake_versioning):
    fake_versioning.delete_result = {
        "deleted": True,
        "name": "bad",
        "verdict": "model_unavailable",
        "detail": "aucun fichier de poids non vide",
        "accuracy": None,
    }

    response = client.delete("/api/v1/models/bad", headers=AUTH)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["deleted"] is True
    assert body["verdict"] == "model_unavailable"
    assert fake_versioning.deleted == ["bad"]


def test_v1_models_delete_active_409(fake_versioning):
    """Version active : 409 enveloppe domaine (code conflict)."""
    fake_versioning.delete_error = ConflictError(
        "La version bad est le modèle actif : désactivez-la avant suppression."
    )

    response = client.delete("/api/v1/models/bad", headers=AUTH)

    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "conflict"


def test_v1_models_delete_healthy_422(fake_versioning):
    fake_versioning.delete_error = ValidationError(
        "La version bad est saine (sanity check ok) : suppression refusée."
    )

    response = client.delete("/api/v1/models/bad", headers=AUTH)

    assert response.status_code == 422, response.text


def test_v1_models_delete_unknown_404(fake_versioning):
    fake_versioning.delete_error = NotFoundError("Version de modèle inconnue : nope.")

    response = client.delete("/api/v1/models/nope", headers=AUTH)

    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# Adaptateur réel : conversion HTTPException legacy -> erreurs de domaine
# ---------------------------------------------------------------------------
def test_adapter_activate_invalid_valueerror_422(monkeypatch):
    """validate_model_version ValueError => HTTPException 422 => ValidationError."""
    import api.routes.models as models_module

    def _invalid(path: str) -> None:
        raise ValueError("config.json illisible")

    monkeypatch.setattr(models_module, "validate_model_version", _invalid)
    adapter = ModuleModelVersioningAdapter()

    with pytest.raises(ValidationError):
        adapter.activate("bad")


def test_adapter_delete_active_conflict_409(monkeypatch, tmp_path):
    """Version active => HTTPException 409 => ConflictError."""
    import api.routes.models as models_module

    monkeypatch.setattr(models_module, "MODEL_ROOT", str(tmp_path))
    monkeypatch.setattr(models_module, "is_active", lambda name: True)
    (tmp_path / "bad").mkdir()
    adapter = ModuleModelVersioningAdapter()

    with pytest.raises(ConflictError):
        adapter.delete("bad")


def test_adapter_delete_unknown_notfound_404(monkeypatch, tmp_path):
    """Version inconnue => HTTPException 404 => NotFoundError."""
    import api.routes.models as models_module

    monkeypatch.setattr(models_module, "MODEL_ROOT", str(tmp_path))
    adapter = ModuleModelVersioningAdapter()

    with pytest.raises(NotFoundError):
        adapter.delete("inconnu")
