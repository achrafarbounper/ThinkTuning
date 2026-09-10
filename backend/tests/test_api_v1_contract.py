# project/tests/test_api_v1_contract.py
"""Verrou de contrat OpenAPI v1 (Phase 4 — découplage frontend/backend).

Le dashboard consomme aujourd'hui 4 endpoints v1 ; un client TS généré les
consommera demain. Toute modification des paths, méthodes, DTO ou posture
d'auth v1 DOIT passer par ce test d'abord : il casse ICI (décision consciente
de versioning : champ optionnel, /api/v2, ...) AVANT de casser un
consommateur — c'est ce qui rend la génération de client sûre le moment
venu (reportée volontairement à la stabilisation de la longue traîne).

L'export consommable par les générateurs : ``python export_openapi.py``
(script et verrou partagent la même source : ``app.openapi()`` — impossible
qu'ils divergent).

Ordre des propriétés INCLUS dans le verrou : un changement du DTO v1 (ordre,
ajout, retrait) est par définition un changement de contrat.
"""

import json
import os

os.environ.setdefault("API_KEY", "test-key")

from api import app  # noqa: E402
from api.schemas.prediction import MAX_TEXTS_PER_REQUEST  # noqa: E402

spec = app.openapi()

V1_PATHS = {
    "/api/v1/health": {"get"},
    "/api/v1/health/model-sanity": {"get"},
    "/api/v1/predict": {"post"},
    "/api/v1/predict/batch": {"post"},
    "/api/v1/predict/reload": {"post"},
    # Phase 3d — noyau training (le WS /api/v1/train/stream n'apparaît pas
    # dans le spec OpenAPI : les websockets ne sont pas des paths HTTP).
    "/api/v1/train": {"post"},
    "/api/v1/train/status/{job_id}": {"get"},
    "/api/v1/train/history/{job_id}": {"get"},
    "/api/v1/train/cancel/{job_id}": {"post"},
    "/api/v1/train/jobs": {"get"},
    "/api/v1/train/schedule": {"post"},
    "/api/v1/train/schedules": {"get"},
    "/api/v1/train/schedules/{schedule_id}": {"delete"},
    # Phase 3d-2 — entraînement d'intention (SCRUM-95)
    "/api/v1/train/intent": {"post"},
    "/api/v1/train/intent/status/{job_id}": {"get"},
    "/api/v1/train/intent/cancel/{job_id}": {"post"},
    "/api/v1/train/intent/jobs": {"get"},
    "/api/v1/train/intent/versions": {"get"},
    "/api/v1/train/intent/activate": {"post"},
    # Phase 3d-3 — catalogue de modèles + évaluation
    "/api/v1/models/details": {"get"},
    "/api/v1/models/active": {"get"},
    "/api/v1/models/{name}/activate": {"post"},
    "/api/v1/models/{name}": {"delete"},
    "/api/v1/evaluate/confusion": {"get"},
    # Phase 3d-4 — agent / sessions / chat IA (dashboard : AssistantPage,
    # FlowMapPage, modèle LLM du chat). Les websockets et flux SSE ne sont pas
    # des paths OpenAPI ; leur comportement est verrouillé par tests.
    "/api/v1/agent/settings": {"get", "put"},
    "/api/v1/agent/settings/test": {"post"},
    "/api/v1/agent/ask/core": {"post"},
    "/api/v1/agent/ask/core/stream": {"post"},
    "/api/v1/agent/multi/ask/stream": {"post"},
    "/api/v1/agent/approvals": {"get"},
    "/api/v1/agent/approvals/{request_id}/approve": {"post"},
    "/api/v1/agent/approvals/{request_id}/reject": {"post"},
    "/api/v1/agent/flow": {"get"},
    "/api/v1/agent/flow/{flow_id}": {"get"},
    "/api/v1/sessions": {"get", "post"},
    "/api/v1/sessions/{session_id}": {"delete"},
    "/api/v1/sessions/{session_id}/messages": {"get"},
    "/api/v1/chat/models": {"get"},
    "/api/v1/chat/ai": {"post"},
    # Phase 3d-5 — dernière tranche : monitoring, drift, explain, pipeline,
    # active learning, classifiers.
    "/api/v1/metrics": {"get"},
    "/api/v1/metrics/json": {"get"},
    # S4 tâche 12 — observabilité MCP (dashboard interne : call volume,
    # error rate, révocations) déléguée au handler legacy api.routes.mcp.
    "/api/v1/mcp/metrics": {"get"},
    "/api/v1/drift": {"post"},
    "/api/v1/explain": {"post"},
    "/api/v1/pipeline": {"post"},
    "/api/v1/pipeline/status/{job_id}": {"get"},
    "/api/v1/pipeline/cancel/{job_id}": {"post"},
    "/api/v1/pipeline/jobs": {"get"},
    "/api/v1/active_learning": {"post"},
    "/api/v1/annotate": {"post"},
    "/api/v1/annotate/list": {"get"},
    "/api/v1/annotate/merge": {"post"},
    "/api/v1/active_learning/cycle": {"post"},
    "/api/v1/active_learning/cycle/status/{job_id}": {"get"},
    "/api/v1/classifiers": {"get"},
    "/api/v1/classifiers/{name}": {"get"},
    "/api/v1/classifiers/{name}/predict": {"post"},
    "/api/v1/classifiers/{name}/reload": {"post"},
}


def test_v1_paths_and_methods_are_locked():
    """Les 4 endpoints v1 existent avec leurs méthodes — rien ne disparaît."""
    for path, methods in V1_PATHS.items():
        assert path in spec["paths"], f"path v1 disparu du contrat : {path}"
        assert methods <= set(spec["paths"][path]), f"méthode v1 modifiée : {path}"


def test_no_undeclared_v1_path():
    """Tout NOUVEAU path /api/v1/* doit être ajouté au verrou explicitement."""
    declared = {p for p in spec["paths"] if p.startswith("/api/v1")}
    undeclared = declared - set(V1_PATHS)
    assert declared == set(V1_PATHS), f"paths v1 non déclarés dans le verrou : {undeclared}"


def _schema(name: str) -> dict:
    schema = spec["components"]["schemas"].get(name)
    assert schema is not None, f"schéma v1 absent du contrat : {name}"
    return schema


def test_predict_dtos_are_locked():
    """Contrat POST /api/v1/predict : requête, réponse, item — ordre inclus.

    Noms courts (``PredictRequest``) : post-épuration legacy, la collision de
    noms qui faisait qualifier les schémas (``api__schemas__prediction__…``)
    a disparu — FastAPI requalifie uniquement en cas de doublon.
    """
    req = _schema("PredictRequest")
    assert list(req["properties"]) == ["texts", "model_name"]
    assert req["required"] == ["texts"]  # model_name optionnel : défaut = version active
    assert req["properties"]["texts"]["type"] == "array"
    assert req["properties"]["texts"]["maxItems"] == MAX_TEXTS_PER_REQUEST  # anti-DoS figé

    resp = _schema("PredictResponse")
    assert list(resp["properties"]) == ["results", "model_version"]
    assert resp["required"] == ["results"]

    item = _schema("PredictedText")
    assert list(item["properties"]) == ["text", "sentiment", "confidence", "model_version"]


def test_health_dtos_are_locked():
    """Contrats santé : /health, sanity (avec résultats par phrase), reload."""
    health = _schema("HealthResponse")
    assert list(health["properties"]) == [
        "status",
        "model_available",
        "active_jobs",
        "model_dir",
        "maintenance_mode",
    ]

    sanity = _schema("SanityVerdictResponse")
    assert list(sanity["properties"]) == [
        "verdict",
        "status",
        "detail",
        "min_confidence",
        "accuracy",
        "model",
        "results",
    ]

    case = _schema("SanityCaseResultOut")
    assert list(case["properties"]) == [
        "text",
        "lang",
        "expected",
        "predicted",
        "confidence",
        "correct",
    ]

    reload_resp = _schema("ReloadResponse")
    assert list(reload_resp["properties"]) == ["status", "sanity"]


def _header_names(path: str, method: str) -> set[str]:
    op = spec["paths"][path][method]
    return {p["name"] for p in op.get("parameters", []) if p["in"] == "header"}


def test_auth_posture_is_locked():
    """predict/reload exposent X-API-Key ; health reste public dans le spec.

    Nuance documentée : le spec marque le header ``required: false`` (valeur
    par défaut dans la signature de la dépendance) alors que le comportement
    réel est 401 sans clé. On verrouille la PRÉSENCE du mécanisme, pas cet
    artefact ; le passage à un vrai schéma Security (APIKeyHeader) corrigera
    la représentation pour les générateurs de clients — changement transverse
    à assumer séparément (touche aussi les routes legacy).
    """
    for path, method in (
        ("/api/v1/predict", "post"),
        ("/api/v1/predict/batch", "post"),
        ("/api/v1/predict/reload", "post"),
        # Phase 3d — noyau training : toute la surface est protégée (parité
        # avec le legacy /train/* qui porte require_api_key).
        ("/api/v1/train", "post"),
        ("/api/v1/train/status/{job_id}", "get"),
        ("/api/v1/train/history/{job_id}", "get"),
        ("/api/v1/train/cancel/{job_id}", "post"),
        ("/api/v1/train/jobs", "get"),
        ("/api/v1/train/schedule", "post"),
        ("/api/v1/train/schedules", "get"),
        ("/api/v1/train/schedules/{schedule_id}", "delete"),
        # Phase 3d-2 — entraînement d'intention : toute la surface est
        # protégée (parité avec le legacy /train/intent/*).
        ("/api/v1/train/intent", "post"),
        ("/api/v1/train/intent/status/{job_id}", "get"),
        ("/api/v1/train/intent/cancel/{job_id}", "post"),
        ("/api/v1/train/intent/jobs", "get"),
        ("/api/v1/train/intent/versions", "get"),
        ("/api/v1/train/intent/activate", "post"),
        # Phase 3d-3 — catalogue de modèles + évaluation : toute la surface
        # est protégée (parité avec le legacy /models/* et /evaluate/*).
        ("/api/v1/models/details", "get"),
        ("/api/v1/models/active", "get"),
        ("/api/v1/models/{name}/activate", "post"),
        ("/api/v1/models/{name}", "delete"),
        ("/api/v1/evaluate/confusion", "get"),
        # Phase 3d-4 — agent / sessions / chat : la surface agent, chat ET
        # sessions (P0 SEC F4 : lectures PII protégées) exige la clé.
        ("/api/v1/agent/settings", "get"),
        ("/api/v1/agent/settings", "put"),
        ("/api/v1/agent/settings/test", "post"),
        ("/api/v1/agent/ask/core", "post"),
        ("/api/v1/agent/ask/core/stream", "post"),
        ("/api/v1/agent/multi/ask/stream", "post"),
        ("/api/v1/agent/approvals", "get"),
        ("/api/v1/agent/approvals/{request_id}/approve", "post"),
        ("/api/v1/agent/approvals/{request_id}/reject", "post"),
        ("/api/v1/agent/flow", "get"),
        ("/api/v1/agent/flow/{flow_id}", "get"),
        ("/api/v1/sessions", "get"),
        ("/api/v1/sessions", "post"),
        ("/api/v1/sessions/{session_id}/messages", "get"),
        ("/api/v1/sessions/{session_id}", "delete"),
        ("/api/v1/metrics", "get"),
        ("/api/v1/metrics/json", "get"),
        ("/api/v1/chat/models", "get"),
        ("/api/v1/chat/ai", "post"),
        # Phase 3d-5 — la surface écrite est protégée (drift, explain,
        # pipeline, active learning, annotate, classifiers predict/reload)
        # + observabilité MCP (surface d'administration interne).
        # Les GET classifiers restent PUBLICS (parité legacy).
        ("/api/v1/drift", "post"),
        ("/api/v1/explain", "post"),
        ("/api/v1/mcp/metrics", "get"),
        ("/api/v1/pipeline", "post"),
        ("/api/v1/pipeline/status/{job_id}", "get"),
        ("/api/v1/pipeline/cancel/{job_id}", "post"),
        ("/api/v1/pipeline/jobs", "get"),
        ("/api/v1/active_learning", "post"),
        ("/api/v1/annotate", "post"),
        ("/api/v1/annotate/list", "get"),
        ("/api/v1/annotate/merge", "post"),
        ("/api/v1/active_learning/cycle", "post"),
        ("/api/v1/active_learning/cycle/status/{job_id}", "get"),
        ("/api/v1/classifiers/{name}/predict", "post"),
        ("/api/v1/classifiers/{name}/reload", "post"),
    ):
        assert "X-API-Key" in _header_names(path, method), f"auth absente du contrat : {path}"
    for path in (
        "/api/v1/health",
        "/api/v1/health/model-sanity",
        "/api/v1/classifiers",
        "/api/v1/classifiers/{name}",
    ):
        assert "X-API-Key" not in _header_names(path, "get"), f"endpoint doit rester public : {path}"  # noqa: E501


def test_export_script_produces_the_locked_spec(tmp_path):
    """Le script d'export (source des générateurs) reflète le même contrat."""
    import export_openapi  # noqa: E402  (racine du dépôt, via conftest)

    out = tmp_path / "openapi.json"
    assert export_openapi.main(["--out", str(out)]) == 0

    exported = json.loads(out.read_text(encoding="utf-8"))
    exported_v1 = {p for p in exported["paths"] if p.startswith("/api/v1")}
    assert exported_v1 == set(V1_PATHS)
