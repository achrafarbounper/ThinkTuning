# project/tests/test_mcp_resources_10.py
"""Tests d'acceptation — Tâche 13 : 10 Resources « thinktuning:// » (S5, v1.1.0).

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tâche 13) :

    - 5 resources supplémentaires au-dessus des 5 de la tâche 8 :
        thinktuning://jobs/{job_id}/logs   → logs d'un job (existence vérifiée
          par délégation job_get → 404 fail-closed ; lignes déléguées à
          ``core.job_logs``, même source mémoire que le WebSocket
          /train/stream — vides pour un job antérieur au démarrage) ;
        thinktuning://models/{version}/info → métadonnées d'un modèle (scan
          délégué ``model_versions`` pour présence + drapeau actif, artefacts
          et ``training_report.json``/``id2label.json`` lus sous la racine
          sandbox revalidée ``safe_resolve``) ;
        thinktuning://datasets/{path}/preview → aperçu d'un dataset (délégation
          ``head_file`` plafonnée ; même règle de format que ``dataset_stats``
          — un ``.env`` est refusé AVANT toute lecture) ;
        thinktuning://metrics/{job_id} → métriques par epoch (existence par
          ``job_get``, puis SELECT miroir de ``core/job_store.py`` sur une
          connexion ``mode=ro`` + ``PRAGMA query_only`` — jamais de création
          de base, jamais d'écriture) ;
        thinktuning://health → santé du système (délégation EXACTE au use case
          hexagonal ``run_health_check`` + adaptateurs legacy par défaut —
          shape ``HealthSnapshot``, identique au /health legacy) ;
    - ``resource_provider.py`` résout les URI dynamiques (regex ancrées +
      validation de segments : traversée, double-encodage, backslash, octets
      nul/contrôle, longueur — refusés AVANT toute I/O) ;
    - ``list_resources()`` → 10, ``list_resource_templates()`` → 6 gabarits ;
    - serveur : ``resources/list`` (10) + ``resources/read`` de bout en bout.

Deux niveaux : UNITAIRES (callables fakes injectés — aucune I/O) et
INTÉGRATION (sources legacy RÉELLES dans une sandbox temporaire
``AGENT_SANDBOX_ROOT`` : jobs.db + train_metrics en lecture seule, logs
capturés en mémoire, version de modèle sur disque, dataset CSV, évasion de
chemin bloquée). Le test ``thinktuning://health`` réel redirige le CWD vers
la sandbox (les adaptateurs legacy lisent ``experiments/`` relativement au
process) : aucun fichier du dépôt n'est créé, aucun appel réseau n'est émis.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import pytest

from app.domain.entities.mcp import MCPResource, MCPResourceTemplate
from app.domain.errors import NotFoundError
from app.domain.ports.mcp_ports import MCPResourceRegistryPort
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPServer
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.protocol import ErrorCode
from app.infrastructure.mcp.resources.resource_provider import (
    MAX_PREVIEW_LINES,
    LegacyResourceProvider,
    build_legacy_resource_provider,
)

# Les 10 URI de la surface thinktuning:// (tâche 8 + extension tâche 13),
# ordre alphabétique pour comparaison d'ensemble.
EXPECTED_URIS = frozenset(
    {
        "thinktuning://config",
        "thinktuning://datasets/{path}/preview",
        "thinktuning://datasets/{path}/stats",
        "thinktuning://health",
        "thinktuning://jobs",
        "thinktuning://jobs/{job_id}",
        "thinktuning://jobs/{job_id}/logs",
        "thinktuning://metrics/{job_id}",
        "thinktuning://models",
        "thinktuning://models/{version}/info",
    }
)

# Les 6 gabarits (routes paramétrées) — les 4 autres sont statiques.
EXPECTED_TEMPLATES = frozenset(
    {
        "thinktuning://datasets/{path}/preview",
        "thinktuning://datasets/{path}/stats",
        "thinktuning://jobs/{job_id}",
        "thinktuning://jobs/{job_id}/logs",
        "thinktuning://metrics/{job_id}",
        "thinktuning://models/{version}/info",
    }
)


# ---------------------------------------------------------------------------
# Fakes des sources internes (aucune I/O — injection dans le provider)
# ---------------------------------------------------------------------------


def _fake_job_get(job_id: str) -> dict:
    if job_id == "j-1":
        return {"job_id": "j-1", "status": "completed", "step": 3, "error": None}
    raise ValueError(
        f"Job introuvable : '{job_id}'. Utilisez job_list pour voir les jobs existants."
    )


def _fake_job_logs(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "line_count": 2,
        "logs": [
            {"seq": 1, "ts": 1.0, "level": "INFO", "step": "training", "message": "epoch 1"},
            {"seq": 2, "ts": 2.0, "level": "INFO", "step": "training", "message": "epoch 2"},
        ],
    }


def _fake_model_versions() -> dict:
    return {
        "model_root": "experiments/models",
        "version_count": 1,
        "versions": [{"name": "20260908T000000Z", "active": True}],
    }


def _fake_model_info(version: str) -> dict:
    if version != "20260908T000000Z":
        raise ValueError(
            f"Version de modèle inconnue : '{version}'. Utilisez "
            "thinktuning://models pour voir les versions disponibles."
        )
    return {
        "version": version,
        "active": True,
        "path": "experiments/models/20260908T000000Z",
        "artifact_count": 2,
        "artifacts": [
            {"name": "model.safetensors", "size_bytes": 7},
            {"name": "training_report.json", "size_bytes": 42},
        ],
        "training_report": {"job_id": "j-1", "metrics": {"f1_macro": 0.9}},
        "id2label": {"0": "negative", "1": "positive"},
    }


def _fake_dataset_stats(path: str, sample_rows: int = 2000) -> dict:
    return {"path": path, "format": ".csv", "row_count": 2, "sample_rows_used": sample_rows}


def _fake_dataset_preview(path: str, max_lines: int = MAX_PREVIEW_LINES) -> dict:
    return {
        "path": path,
        "max_lines": max_lines,
        "returned_lines": 2,
        "truncated": False,
        "lines": ["text,label", "bonjour,positive"],
    }


def _fake_agent_config() -> dict:
    return {"provider": "openrouter", "openrouter_api_key": "sk-or-v1-abcdef0123456789"}


def _fake_job_metrics(job_id: str) -> dict:
    return {
        "job_id": job_id,
        "epoch_count": 2,
        "metrics": [
            {"epoch": 1, "loss": 0.7, "f1_macro": 0.6, "accuracy": 0.7},
            {"epoch": 2, "loss": 0.4, "f1_macro": 0.8, "accuracy": 0.85},
        ],
    }


def _fake_system_health() -> dict:
    return {
        "status": "ok",
        "model_available": True,
        "active_jobs": 1,
        "model_dir": "experiments/models/20260908T000000Z",
        "maintenance_mode": False,
    }


@pytest.fixture
def provider() -> LegacyResourceProvider:
    """Provider avec sources internes FAKES (aucune I/O, aucun import lourd)."""
    return LegacyResourceProvider(
        job_get=_fake_job_get,
        job_logs=_fake_job_logs,
        model_versions=_fake_model_versions,
        model_info=_fake_model_info,
        dataset_stats=_fake_dataset_stats,
        dataset_preview=_fake_dataset_preview,
        agent_config=_fake_agent_config,
        job_metrics=_fake_job_metrics,
        system_health=_fake_system_health,
    )


def _rpc(server: MCPServer, request_id: int, method: str, params: dict | None = None) -> dict:
    """Helper JSON-RPC : exécute une requête sur le serveur et parse la réponse."""
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return json.loads(server.handle_text(json.dumps(payload)))


# ============================================================
# Catalogue — les 10 resources (contrat port + gabarits MCP)
# ============================================================


def test_provider_implements_resource_registry_port(provider) -> None:
    """LegacyResourceProvider implémente MCPResourceRegistryPort — fail-fast."""
    assert isinstance(provider, MCPResourceRegistryPort)


def test_list_resources_returns_exactly_the_10(provider) -> None:
    """``ListResources`` → les 10 resources de la checklist tâche 13."""
    resources = provider.list_resources()
    assert len(resources) == 10
    assert all(isinstance(r, MCPResource) for r in resources)
    assert {r.uri for r in resources} == EXPECTED_URIS
    for resource in resources:
        assert resource.name, resource.uri
        assert resource.description, resource.uri
        assert resource.mime_type == "application/json", resource.uri


def test_list_resources_is_pure_metadata(provider) -> None:
    """La liste ne déclenche AUCUNE résolution de source (métadonnée pure)."""
    assert provider.list_resources() == provider.list_resources()


def test_list_resource_templates_covers_the_6_dynamic_uris(provider) -> None:
    """Les 6 routes paramétrées sont exposées comme gabarits MCP typés."""
    templates = provider.list_resource_templates()
    assert {t.uri_template for t in templates} == EXPECTED_TEMPLATES
    assert all(isinstance(t, MCPResourceTemplate) for t in templates)
    by_uri = {t.uri_template: t for t in templates}
    for uri in (
        "thinktuning://jobs/{job_id}/logs",
        "thinktuning://metrics/{job_id}",
    ):
        assert by_uri[uri].arguments[0].name == "job_id", uri
        assert by_uri[uri].arguments[0].required is True, uri
    assert by_uri["thinktuning://models/{version}/info"].arguments[0].name == "version"
    assert by_uri["thinktuning://datasets/{path}/preview"].arguments[0].name == "path"


# ============================================================
# ReadResource — les 5 nouvelles resources (délégation fakes)
# ============================================================


def test_read_resource_job_logs_delegates_after_existence_check(provider) -> None:
    """``jobs/{id}/logs`` → job_get (existence) PUIS logs → JSON parsable."""
    payload = json.loads(provider.read_resource("thinktuning://jobs/j-1/logs"))
    assert payload["job_id"] == "j-1"
    assert payload["line_count"] == 2
    assert payload["logs"][0]["message"] == "epoch 1"
    assert payload["logs"][1]["seq"] == 2


def test_read_resource_job_logs_unknown_job_is_not_found(provider) -> None:
    """Job inconnu → l'existence passe par job_get (message actionable préservé)."""
    with pytest.raises(NotFoundError, match="Job introuvable"):
        provider.read_resource("thinktuning://jobs/ghost/logs")


def test_read_resource_model_info(provider) -> None:
    """``models/{version}/info`` → scan délégué + rapport + artefacts en JSON."""
    payload = json.loads(
        provider.read_resource("thinktuning://models/20260908T000000Z/info")
    )
    assert payload["version"] == "20260908T000000Z"
    assert payload["active"] is True
    assert payload["training_report"]["job_id"] == "j-1"
    assert payload["id2label"] == {"0": "negative", "1": "positive"}
    assert {a["name"] for a in payload["artifacts"]} == {
        "model.safetensors",
        "training_report.json",
    }
    assert payload["artifact_count"] == 2


def test_read_resource_model_info_unknown_version_is_not_found(provider) -> None:
    """Version absente du scan délégué → NotFoundError actionable."""
    with pytest.raises(NotFoundError, match="Version de modèle inconnue"):
        provider.read_resource("thinktuning://models/ghost/info")


def test_read_resource_dataset_preview_caps_max_lines(provider) -> None:
    """``datasets/{path}/preview`` → head_file délégué, plafonné à 50 lignes."""
    payload = json.loads(
        provider.read_resource("thinktuning://datasets/data/train.csv/preview")
    )
    assert payload["path"] == "data/train.csv"
    assert payload["max_lines"] == MAX_PREVIEW_LINES
    assert payload["lines"][0] == "text,label"
    assert payload["truncated"] is False


@pytest.mark.parametrize(
    "uri",
    [
        "thinktuning://datasets/notes.txt/preview",
        "thinktuning://datasets/secrets/credentials/preview",  # sans suffixe
        "thinktuning://datasets/.env/preview",  # secret : refusé AVANT lecture
    ],
)
def test_read_resource_dataset_preview_rejects_non_dataset_formats(
    provider, uri: str
) -> None:
    """Même règle de format que dataset_stats : fail-closed, message actionable."""
    with pytest.raises(NotFoundError, match="Format non supporté"):
        provider.read_resource(uri)


def test_read_resource_metrics(provider) -> None:
    """``metrics/{job_id}`` → SELECT lecture seule → métriques par epoch."""
    payload = json.loads(provider.read_resource("thinktuning://metrics/j-1"))
    assert payload["job_id"] == "j-1"
    assert payload["epoch_count"] == 2
    assert [m["epoch"] for m in payload["metrics"]] == [1, 2]
    assert payload["metrics"][1]["f1_macro"] == 0.8
    assert payload["metrics"][0]["loss"] == 0.7


def test_read_resource_metrics_unknown_job_is_not_found(provider) -> None:
    """Job inconnu → existence vérifiée par délégation job_get → 404."""
    with pytest.raises(NotFoundError, match="Job introuvable"):
        provider.read_resource("thinktuning://metrics/ghost")


def test_read_resource_health_shape_is_locked(provider) -> None:
    """``health`` → HealthSnapshot (shape /health legacy, contrat verrouillé)."""
    payload = json.loads(provider.read_resource("thinktuning://health"))
    assert set(payload.keys()) == {
        "status",
        "model_available",
        "active_jobs",
        "model_dir",
        "maintenance_mode",
    }
    assert payload["status"] == "ok"
    assert payload["model_available"] is True
    assert payload["active_jobs"] == 1


# ============================================================
# Sécurité URI — traversée, double-encodage, séparateurs, longueur
# (segments des NOUVELLES routes dynamiques)
# ============================================================


@pytest.mark.parametrize(
    "uri",
    [
        "thinktuning://jobs/..%2Fsecret/logs",  # traversée encodée UNE fois
        "thinktuning://jobs/%252e%252e/x/logs",  # double-encodage (% résiduel)
        "thinktuning://jobs/data%5Csecret/logs",  # backslash encodé
        "thinktuning://jobs/j%00x/logs",  # null byte encodé
        "thinktuning://jobs/j%0Ax/logs",  # contrôle encodé
        "thinktuning://models/..%2Factive.json/info",  # traversée encodée
        "thinktuning://models/%252e%252e/x/info",  # double-encodage
        "thinktuning://models/data%5Csecret/info",  # backslash encodé
        "thinktuning://metrics/..",  # traversée relative
        "thinktuning://metrics/..%2Fjobs.db",  # traversée encodée
        "thinktuning://metrics/%00x",  # null byte encodé
        "thinktuning://metrics/./x",  # segment '.'
        "thinktuning://datasets/data%0Asecret.csv/preview",  # contrôle encodé
        "thinktuning://datasets//train.csv/preview",  # segment vide
        "thinktuning://datasets/./train.csv/preview",  # segment '.'
        "thinktuning://datasets/data/../../secret.csv/preview",  # traversée
    ],
)
def test_read_resource_rejects_malicious_dynamic_uris(provider, uri: str) -> None:
    """Aucune URI malveillante n'atteint la source : NotFoundError AVANT I/O."""
    with pytest.raises(NotFoundError):
        provider.read_resource(uri)


def test_read_resource_rejects_oversized_dynamic_segments(provider) -> None:
    """Segments géants refusés AVANT toute délégation (plafond 200/500 chars)."""
    with pytest.raises(NotFoundError, match="trop long"):
        provider.read_resource("thinktuning://metrics/" + "a" * 500)
    with pytest.raises(NotFoundError, match="trop long"):
        provider.read_resource("thinktuning://models/" + "a" * 500 + "/info")
    with pytest.raises(NotFoundError, match="trop long"):
        provider.read_resource("thinktuning://datasets/" + "a" * 600 + "/preview")


def test_read_resource_unknown_uri_lists_the_10(provider) -> None:
    """URI inconnue → NotFoundError dont le message liste les 10 resources."""
    with pytest.raises(NotFoundError) as excinfo:
        provider.read_resource("thinktuning://nope")
    message = str(excinfo.value)
    for expected in (
        "thinktuning://jobs/{job_id}/logs",
        "thinktuning://models/{version}/info",
        "thinktuning://datasets/{path}/preview",
        "thinktuning://metrics/{job_id}",
        "thinktuning://health",
    ):
        assert expected in message


# ============================================================
# Serveur — resources/list (10) + resources/read de bout en bout
# ============================================================


def test_server_resources_list_returns_10(provider) -> None:
    """``resources/list`` JSON-RPC → les 10 resources projetées."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=provider
    )
    reply = _rpc(server, 1, "resources/list")
    resources = reply["result"]["resources"]
    assert len(resources) == 10
    assert {r["uri"] for r in resources} == EXPECTED_URIS
    assert all({"uri", "name", "description", "mimeType"} <= set(r) for r in resources)


def test_factory_wires_the_10_resources_by_default() -> None:
    """``build_mcp_server()`` branche le registre des 10 resources (tâche 13)."""
    server = build_mcp_server()
    reply = _rpc(server, 2, "resources/list")
    uris = {r["uri"] for r in reply["result"]["resources"]}
    assert len(uris) == 10
    assert uris == EXPECTED_URIS


def test_server_resources_read_roundtrip_new_uris(provider) -> None:
    """``resources/read`` → ``contents[0]`` (uri + text ; mimeType si statique)."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=provider
    )
    # URI PARAMÉTRÉE : la spec MCP rend mimeType optionnel — le serveur ne le
    # reprend du catalogue que pour une URI statique (convention bootstrap).
    reply = _rpc(server, 3, "resources/read", {"uri": "thinktuning://metrics/j-1"})
    contents = reply["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "thinktuning://metrics/j-1"
    assert "mimeType" not in contents[0]
    assert json.loads(contents[0]["text"])["epoch_count"] == 2

    # URI STATIQUE : mimeType = application/json.
    reply = _rpc(server, 4, "resources/read", {"uri": "thinktuning://health"})
    contents = reply["result"]["contents"]
    assert contents[0]["mimeType"] == "application/json"
    assert json.loads(contents[0]["text"])["status"] == "ok"


def test_server_resources_read_unknown_uri_is_invalid_params(provider) -> None:
    """URI inconnue (segment surnuméraire) → erreur JSON-RPC ``Invalid params``."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=provider
    )
    reply = _rpc(server, 5, "resources/read", {"uri": "thinktuning://jobs/j-1/logs/x"})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_server_resources_read_missing_job_is_invalid_params(provider) -> None:
    """Erreur métier du tool (404 domaine) → ``Invalid params``, jamais un crash."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=provider
    )
    reply = _rpc(server, 6, "resources/read", {"uri": "thinktuning://metrics/ghost"})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "Job introuvable" in reply["error"]["message"]


# ============================================================
# Intégration — sources legacy RÉELLES dans une sandbox temporaire
# (AGENT_SANDBOX_ROOT → tmp_path : safe_resolve + SQLite query_only vérifiés)
# ============================================================


@pytest.fixture
def sandbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox ISOLÉE : ``safe_resolve`` confine tous les chemins à tmp_path."""
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def legacy_provider() -> LegacyResourceProvider:
    """Provider PAR DÉFAUT (sources legacy résolues paresseusement au 1er appel)."""
    return build_legacy_resource_provider()


def _seed_jobs_db(sandbox_root: Path, job_id: str = "job-123") -> None:
    """Crée experiments/jobs.db (tables jobs + train_metrics des tools legacy)."""
    db_dir = sandbox_root / "experiments"
    db_dir.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_dir / "jobs.db")
    try:
        conn.execute(
            "CREATE TABLE jobs ("
            "job_id TEXT PRIMARY KEY, payload TEXT NOT NULL, updated_at REAL NOT NULL)"
        )
        payload = {
            "job_id": job_id,
            "status": "completed",
            "step": 3,
            "error": None,
            "model_path": "experiments/models/20260908T000000Z",
        }
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)", (job_id, json.dumps(payload), time.time())
        )
        # Table train_metrics (schéma du PersistentJobStore, SCRUM-73).
        conn.execute(
            "CREATE TABLE train_metrics ("
            "job_id TEXT NOT NULL, epoch INTEGER NOT NULL, loss REAL, "
            "f1_macro REAL, accuracy REAL, updated_at REAL NOT NULL, "
            "PRIMARY KEY (job_id, epoch))"
        )
        conn.executemany(
            "INSERT INTO train_metrics VALUES (?, ?, ?, ?, ?, ?)",
            [
                (job_id, 1, 0.7, 0.55, 0.62, time.time()),
                (job_id, 2, 0.41, 0.81, 0.86, time.time()),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _make_model_version(sandbox_root: Path, name: str = "20260908T000000Z") -> Path:
    """Version de modèle réaliste : poids + rapport d'entraînement + mappings."""
    version_dir = sandbox_root / "experiments" / "models" / name
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "model.safetensors").write_bytes(b"weights")
    (version_dir / "training_report.json").write_text(
        json.dumps(
            {
                "timestamp": name,
                "job_id": "job-123",
                "hyperparameters": {"epochs": 3},
                "metrics": {"f1_macro": 0.81},
            }
        ),
        encoding="utf-8",
    )
    (version_dir / "id2label.json").write_text(
        json.dumps({"0": "negative", "1": "positive"}), encoding="utf-8"
    )
    return version_dir


def test_real_metrics_roundtrip(sandbox_root, legacy_provider) -> None:
    """Métriques réelles : SELECT lecture seule sur la table train_metrics."""
    _seed_jobs_db(sandbox_root)
    payload = json.loads(legacy_provider.read_resource("thinktuning://metrics/job-123"))
    assert payload["job_id"] == "job-123"
    assert payload["epoch_count"] == 2
    assert [m["epoch"] for m in payload["metrics"]] == [1, 2]
    assert payload["metrics"][1]["accuracy"] == 0.86
    assert payload["metrics"][1]["f1_macro"] == 0.81


def test_real_metrics_unknown_job_is_not_found(sandbox_root, legacy_provider) -> None:
    """Job absent de la base réelle → NotFoundError avec le message de job_get."""
    _seed_jobs_db(sandbox_root)
    with pytest.raises(NotFoundError, match="Job introuvable"):
        legacy_provider.read_resource("thinktuning://metrics/ghost")


def test_real_metrics_missing_db_is_not_found(sandbox_root, legacy_provider) -> None:
    """Base absente → FileNotFoundError délégué → NotFoundError actionable."""
    with pytest.raises(NotFoundError, match="Aucune base de jobs"):
        legacy_provider.read_resource("thinktuning://metrics/job-123")


def test_real_metrics_connection_is_query_only(sandbox_root) -> None:
    """Garantie portée par délégation : mode=ro + PRAGMA query_only.

    La connexion utilisée par la resource metrics est celle des tools
    (``ia/tools/ml_tools._connect_readonly``) : toute écriture est refusée
    par SQLite (``sqlite3.OperationalError``).
    """
    from ia.tools.ml_tools import _connect_readonly
    from ia.tools.sandbox import safe_resolve

    _seed_jobs_db(sandbox_root)
    conn = _connect_readonly(safe_resolve(Path("experiments") / "jobs.db"))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO train_metrics VALUES ('evil', 1, 0, 0, 0, 0)")
    finally:
        conn.close()


def test_real_job_logs_roundtrip(sandbox_root, legacy_provider) -> None:
    """Logs réels : buffer mémoire de ``core.job_logs`` (source /train/stream)."""
    from core import job_logs

    _seed_jobs_db(sandbox_root)
    job_logs.attach_job_logging("job-123")
    # pytest force WARNING sur le logger racine : sans ce niveau explicite, le
    # record INFO est filtré AVANT d'atteindre le JobLogHandler (root).
    capture_logger = logging.getLogger("test.mcp.resources_10")
    capture_logger.setLevel(logging.INFO)
    try:
        capture_logger.info("Epoch 1 terminée (f1=0.55)")
        payload = json.loads(
            legacy_provider.read_resource("thinktuning://jobs/job-123/logs")
        )
    finally:
        job_logs.detach_job_logging()
        job_logs.reset_job_logs("job-123")
    assert payload["job_id"] == "job-123"
    assert payload["line_count"] == 1
    assert "Epoch 1 terminée" in payload["logs"][0]["message"]
    assert payload["logs"][0]["level"] == "INFO"


def test_real_job_logs_known_job_without_capture_is_empty(
    sandbox_root, legacy_provider
) -> None:
    """Job connu (base réelle) mais logs perdus (restart) → 0 ligne, PAS 404."""
    from core import job_logs

    _seed_jobs_db(sandbox_root)
    payload = json.loads(
        legacy_provider.read_resource("thinktuning://jobs/job-123/logs")
    )
    assert payload["line_count"] == 0
    assert payload["logs"] == []
    job_logs.reset_job_logs("job-123")  # no-op défensif (isolation)


def test_real_model_info_roundtrip(sandbox_root, legacy_provider) -> None:
    """Métadonnées réelles : scan délégué + rapport lu sous la sandbox."""
    _make_model_version(sandbox_root)
    payload = json.loads(
        legacy_provider.read_resource("thinktuning://models/20260908T000000Z/info")
    )
    assert payload["version"] == "20260908T000000Z"
    assert payload["active"] is True  # la plus récente (unique) est active
    assert payload["training_report"]["job_id"] == "job-123"
    assert payload["training_report"]["metrics"]["f1_macro"] == 0.81
    assert payload["id2label"] == {"0": "negative", "1": "positive"}
    assert {a["name"] for a in payload["artifacts"]} == {
        "id2label.json",
        "model.safetensors",
        "training_report.json",
    }
    assert payload["artifacts"][0]["name"] == "id2label.json"  # tri par nom


def test_real_model_info_without_report_still_answers(
    sandbox_root, legacy_provider
) -> None:
    """Version créée hors pipeline (poids seuls) → rapport None + message.

    Une version PLUS RÉCENTE et valide est créée d'abord : la version
    tokenizer-only (ancienne) n'est donc PAS active (drapeau du scan délégué).
    """
    _make_model_version(sandbox_root)  # 20260908… : la plus récente → active
    version_dir = sandbox_root / "experiments" / "models" / "20260101T000000Z"
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "model.safetensors").write_bytes(b"weights")
    payload = json.loads(
        legacy_provider.read_resource("thinktuning://models/20260101T000000Z/info")
    )
    assert payload["training_report"] is None
    assert "training_report" in payload["message"]
    assert payload["active"] is False  # une version plus récente existe


def test_real_model_info_unknown_version_is_not_found(
    sandbox_root, legacy_provider
) -> None:
    """Version absente du scan → ValueError délégué → NotFoundError."""
    with pytest.raises(NotFoundError, match="Version de modèle inconnue"):
        legacy_provider.read_resource("thinktuning://models/ghost/info")


def test_real_dataset_preview_roundtrip(sandbox_root, legacy_provider) -> None:
    """Dataset CSV réel : aperçu head_file (plafond 50 lignes appliqué)."""
    data_dir = sandbox_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "train.csv").write_text(
        "text,label,lang_code\nbonjour,positive,fr\ntriste,negative,en\n",
        encoding="utf-8",
    )
    payload = json.loads(
        legacy_provider.read_resource("thinktuning://datasets/data/train.csv/preview")
    )
    assert payload["returned_lines"] == 3
    assert payload["truncated"] is False
    assert payload["max_lines"] == MAX_PREVIEW_LINES
    assert payload["lines"][0] == "text,label,lang_code"
    assert payload["lines"][1] == "bonjour,positive,fr"


def test_real_dataset_preview_missing_is_not_found(sandbox_root, legacy_provider) -> None:
    """Dataset absent → ``safe_resolve(must_exist=True)`` → NotFoundError."""
    with pytest.raises(NotFoundError, match="Introuvable"):
        legacy_provider.read_resource("thinktuning://datasets/data/ghost.csv/preview")


def test_real_dataset_preview_rejects_env_file(sandbox_root, legacy_provider) -> None:
    """Sécurité : un fichier non-dataset (ex. .env) est refusé AVANT lecture."""
    (sandbox_root / ".env").write_text("API_KEY=supersecret\n", encoding="utf-8")
    with pytest.raises(NotFoundError, match="Format non supporté"):
        legacy_provider.read_resource("thinktuning://datasets/.env/preview")


def test_real_dataset_preview_escape_is_blocked(sandbox_root, legacy_provider) -> None:
    """Deuxième ligne de défense : un chemin ABSOLU hors sandbox est refusé.

    L'URI passe la validation de segments (chemin multi-segments sans ``..``)
    mais ``safe_resolve`` (délégation ``head_file``) refuse l'évasion →
    ``PermissionError`` → ``NotFoundError`` (fail-closed, pas d'oracle).
    """
    outside = sandbox_root.parent / "outside-secret.csv"
    outside.write_text("text,label\nx,positive\n", encoding="utf-8")
    try:
        uri = f"thinktuning://datasets/{outside.as_posix()}/preview"
        with pytest.raises(NotFoundError):
            legacy_provider.read_resource(uri)
    finally:
        outside.unlink()


def test_real_health_roundtrip(
    sandbox_root, legacy_provider, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Santé réelle : use case v1 + adaptateurs legacy (CWD redirigé vers la
    sandbox : les adaptateurs lisent ``experiments/`` relativement au process,
    aucun fichier du dépôt n'est créé)."""
    monkeypatch.chdir(sandbox_root)
    # Isolation du store global : le singleton ``core.job_store`` est chargé
    # en mémoire à l'import depuis ``backend/experiments/jobs.db`` (état
    # développeur local, ex. 12 RUNNING) et ne suit PAS le chdir. Sans cette
    # isolation, ``active_jobs`` dépend de la machine qui lance le test
    # (flake d'ordre : passe seul, casse en suite complète). On simule la
    # « base vierge » attendue (même pattern que test_api_v1_health).
    monkeypatch.setattr("core.job_store.get_job_store", lambda: {})
    _make_model_version(sandbox_root)
    payload = json.loads(legacy_provider.read_resource("thinktuning://health"))
    assert payload["status"] == "ok"
    assert payload["model_available"] is True
    assert payload["model_dir"].endswith("20260908T000000Z")
    assert payload["active_jobs"] == 0  # base vierge (aucun job RUNNING)
    assert payload["maintenance_mode"] is False
