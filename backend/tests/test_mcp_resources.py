# project/tests/test_mcp_resources.py
"""Tests d'acceptation — Tâche 8 : 5 Resources « thinktuning:// » (S3, v1.0.0 Beta).

Tâche 13 (S5, v1.1.0) : la surface est étendue à 10 (4 statiques +
6 paramétrées). Les assertions de LISTE reflètent la surface complète ;
les tests de LECTURE restants couvrent les 5 resources d'origine
(non-régression), les 5 nouvelles étant couvertes exhaustivement par
test_mcp_resources_10.py.

Checklist (docs/mcp/IMPLEMENTATION_PLAN.md, tâche 8) :

    - ``LegacyResourceProvider`` expose les 5 resources (3 statiques +
      2 paramétrées) via ``list_resources()`` → ``resources/list`` ;
    - entité ``MCPResource`` : ``uri``, ``name``, ``description``, ``mimeType`` ;
    - ``read_resource(uri)`` résout l'URI → appelle le tool interne
      (``job_list`` / ``job_get`` / ``model_versions`` / ``dataset_stats`` /
      ``agent_config``) → JSON ;
    - sécurité : validation stricte des segments d'URI (anti-traversée,
      anti double-encodage, anti backslash), ``safe_resolve`` porté par
      délégation pour les chemins, SQLite ``mode=ro`` + ``PRAGMA query_only``
      pour le SQL (jobs.db), clés API de ``agent_config`` JAMAIS en clair ;
    - serveur : ``resources/list`` retourne les 5, ``resources/read`` résout
      une URI (contenu ``{uri, mimeType?, text}``) et traduit ``NotFoundError``
      en erreur JSON-RPC ``Invalid params`` (jamais un crash).

Deux niveaux : UNITAIRES (callables fakes injectés — aucune I/O) et
INTÉGRATION (tools legacy RÉELS dans une sandbox temporaire
``AGENT_SANDBOX_ROOT`` : jobs.db en lecture seule, dataset CSV, évasion de
chemin bloquée). Aucun appel réseau ; ``thinktuning://config`` n'est JAMAIS
lu avec l'implémentation réelle (``core.agent_cache`` écrirait une base — les
fakes couvrent la route et le masquage).
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from app.domain.entities.mcp import MCPResource, MCPResourceTemplate, MCPScopeRole, MCPVersion
from app.domain.errors import NotFoundError
from app.domain.ports.mcp_ports import MCPResourceRegistryPort
from app.infrastructure.mcp.mcp_server import InMemoryToolProvider, MCPServer
from app.infrastructure.mcp.mcp_server_factory import build_mcp_server
from app.infrastructure.mcp.protocol import ErrorCode
from app.infrastructure.mcp.resources.resource_provider import (
    LegacyResourceProvider,
    build_legacy_resource_provider,
)

# Les 5 URI de la checklist tâche 8 (ordre alphabétique pour comparaison d'ensemble).
# Tâche 13 (S5, v1.1.0) : la surface est étendue à 10 — ces 5 URI d'origine
# restent un SOUS-ENSEMBLE garanti (non-régression), les 5 nouvelles sont
# couvertes exhaustivement par tests/test_mcp_resources_10.py.
EXPECTED_URIS = frozenset(
    {
        "thinktuning://jobs",
        "thinktuning://jobs/{job_id}",
        "thinktuning://models",
        "thinktuning://datasets/{path}/stats",
        "thinktuning://config",
    }
)

# Surface complète après extension tâche 13 (4 statiques + 6 paramétrées).
EXPECTED_URIS_10 = EXPECTED_URIS | frozenset(
    {
        "thinktuning://jobs/{job_id}/logs",
        "thinktuning://models/{version}/info",
        "thinktuning://datasets/{path}/preview",
        "thinktuning://metrics/{job_id}",
        "thinktuning://health",
    }
)


# ---------------------------------------------------------------------------
# Fakes des tools internes (aucune I/O — injection dans le provider)
# ---------------------------------------------------------------------------


def _fake_job_list(**_kwargs) -> dict:
    return {
        "db_path": "experiments/jobs.db",
        "job_count": 1,
        "truncated": False,
        "jobs": [{"job_id": "j-1", "status": "completed", "step": 3}],
    }


def _fake_job_get(job_id: str) -> dict:
    if job_id == "j-1":
        return {"job_id": "j-1", "status": "completed", "step": 3, "error": None}
    raise ValueError(
        f"Job introuvable : '{job_id}'. Utilisez job_list pour voir les jobs existants."
    )


def _fake_model_versions() -> dict:
    return {
        "model_root": "experiments/models",
        "version_count": 1,
        "versions": [{"name": "20260908T000000Z", "active": True}],
    }


def _fake_dataset_stats(path: str, sample_rows: int = 2000) -> dict:
    return {"path": path, "format": ".csv", "row_count": 2, "sample_rows_used": sample_rows}


def _fake_agent_config() -> dict:
    return {
        "provider": "openrouter",
        "model": "vendor/model-x",
        "openrouter_url": "https://openrouter.ai/api/v1/chat/completions",
        "openrouter_api_key": "sk-or-v1-abcdef0123456789",
        "hf_api_key": "",
        "timeout": 30.0,
    }


@pytest.fixture
def provider() -> LegacyResourceProvider:
    """Provider avec tools internes FAKES (aucune I/O, aucun import lourd)."""
    return LegacyResourceProvider(
        job_list=_fake_job_list,
        job_get=_fake_job_get,
        model_versions=_fake_model_versions,
        dataset_stats=_fake_dataset_stats,
        agent_config=_fake_agent_config,
    )


def _rpc(server: MCPServer, request_id: int, method: str, params: dict | None = None) -> dict:
    """Helper JSON-RPC : exécute une requête sur le serveur et parse la réponse."""
    payload = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        payload["params"] = params
    return json.loads(server.handle_text(json.dumps(payload)))


# ============================================================
# Entité MCPResource — immutabilité + projection MCP
# ============================================================


def test_mcp_resource_is_immutable() -> None:
    """MCPResource est immuable (frozen, slots) — value object du domaine."""
    resource = MCPResource(uri="thinktuning://jobs", name="jobs")
    with pytest.raises((AttributeError, TypeError, PermissionError)):
        resource.name = "changed"  # type: ignore[misc]


def test_mcp_resource_to_dict_projection() -> None:
    """to_dict projette exactement les clés MCP (uri, name, description, mimeType)."""
    resource = MCPResource(
        uri="thinktuning://jobs",
        name="jobs",
        description="Liste des jobs d'entraînement.",
        mime_type="application/json",
    )
    d = resource.to_dict()
    assert set(d.keys()) == {"uri", "name", "description", "mimeType"}
    assert d["uri"] == "thinktuning://jobs"
    assert d["mimeType"] == "application/json"


# ============================================================
# ListResources — les 5 resources (contrat port + JSON-RPC)
# ============================================================


def test_provider_implements_resource_registry_port(provider) -> None:
    """LegacyResourceProvider implémente MCPResourceRegistryPort — fail-fast."""
    assert isinstance(provider, MCPResourceRegistryPort)


def test_list_resources_returns_exactly_the_5(provider) -> None:
    """``ListResources`` → les 10 resources (les 5 de la tâche 8 + 5 tâche 13).

    Non-régression tâche 8 : les 5 URI d'origine restent présentes ; la
    couverture exhaustive des 5 nouvelles vit dans test_mcp_resources_10.py.
    """
    resources = provider.list_resources()
    assert len(resources) == 10
    assert all(isinstance(r, MCPResource) for r in resources)
    assert {r.uri for r in resources} == EXPECTED_URIS_10
    assert EXPECTED_URIS <= {r.uri for r in resources}
    for resource in resources:
        assert resource.name, resource.uri
        assert resource.description, resource.uri
        assert resource.mime_type == "application/json", resource.uri


def test_list_resource_templates_covers_dynamic_uris(provider) -> None:
    """Les 6 URIs paramétrées sont aussi exposées comme gabarits MCP (S5)."""
    templates = provider.list_resource_templates()
    assert {t.uri_template for t in templates} == {
        "thinktuning://jobs/{job_id}",
        "thinktuning://jobs/{job_id}/logs",
        "thinktuning://models/{version}/info",
        "thinktuning://datasets/{path}/stats",
        "thinktuning://datasets/{path}/preview",
        "thinktuning://metrics/{job_id}",
    }
    assert all(isinstance(t, MCPResourceTemplate) for t in templates)
    job_template = next(t for t in templates if t.uri_template == "thinktuning://jobs/{job_id}")
    assert job_template.arguments[0].name == "job_id"
    assert job_template.arguments[0].required is True


def test_list_resources_is_pure_metadata(provider) -> None:
    """La liste ne déclenche AUCUNE résolution de tool (métadonnée pure)."""
    # Les résolveurs paresseux ne sont JAMAIS appelés : aucune I/O possible.
    assert provider.list_resources() == provider.list_resources()


def test_server_resources_list_returns_5(provider) -> None:
    """``resources/list`` JSON-RPC → les 10 resources projetées (uri + name)."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=provider
    )
    reply = _rpc(server, 1, "resources/list")
    resources = reply["result"]["resources"]
    assert len(resources) == 10
    assert {r["uri"] for r in resources} == EXPECTED_URIS_10
    assert all({"uri", "name", "description", "mimeType"} <= set(r) for r in resources)


def test_factory_wires_resources_by_default() -> None:
    """``build_mcp_server()`` branche le registre des 10 resources (tâches 8+13)."""
    server = build_mcp_server()
    reply = _rpc(server, 2, "resources/list")
    assert {r["uri"] for r in reply["result"]["resources"]} == EXPECTED_URIS_10


def test_initialize_advertises_resources_capability(provider) -> None:
    """initialize annonce la capability ``resources`` quand un registre est branché."""
    with_provider = _rpc(
        build_mcp_server(tool_provider=InMemoryToolProvider([]), resource_provider=provider),
        3,
        "initialize",
    )
    assert with_provider["result"]["capabilities"]["resources"] == {
        "subscribe": False,
        "listChanged": False,
    }
    # Un provider VIDE expose la capability (surface supportée, catalogue nul)…
    empty = _rpc(
        build_mcp_server(tool_provider=InMemoryToolProvider([]), resource_provider=_EMPTY_PROVIDER),
        4,
        "initialize",
    )
    assert empty["result"]["capabilities"]["resources"] == {
        "subscribe": False,
        "listChanged": False,
    }
    # …alors qu'un serveur SANS registre (constructions sur mesure) ne l'annonce pas.
    bare = _rpc(
        MCPServer(
            version=MCPVersion(major=0, minor=1, patch=0),
            scope=MCPScopeRole.READ_ONLY,
            tool_provider=InMemoryToolProvider([]),
            resource_provider=None,
        ),
        5,
        "initialize",
    )
    assert "resources" not in bare["result"]["capabilities"]


class _EmptyProvider:
    """Provider SANS resource (simulation d'une surface vide explicite)."""

    def list_resources(self) -> list[MCPResource]:
        return []

    def read_resource(self, uri: str) -> str:
        raise NotFoundError(f"Resource not found : {uri}")


_EMPTY_PROVIDER = _EmptyProvider()


# ============================================================
# ReadResource — routes statiques et paramétrées (délégation tools internes)
# ============================================================


def test_read_resource_jobs_static(provider) -> None:
    """``thinktuning://jobs`` → ``job_list()`` → JSON parsable."""
    payload = json.loads(provider.read_resource("thinktuning://jobs"))
    assert payload["job_count"] == 1
    assert payload["jobs"][0]["job_id"] == "j-1"


def test_read_resource_job_dynamic(provider) -> None:
    """``thinktuning://jobs/{job_id}`` → ``job_get(job_id)`` → payload complet."""
    payload = json.loads(provider.read_resource("thinktuning://jobs/j-1"))
    assert payload == {"job_id": "j-1", "status": "completed", "step": 3, "error": None}


def test_read_resource_models_static(provider) -> None:
    """``thinktuning://models`` → ``model_versions()`` → JSON."""
    payload = json.loads(provider.read_resource("thinktuning://models"))
    assert payload["version_count"] == 1
    assert payload["versions"][0]["active"] is True


def test_read_resource_dataset_stats_multi_segment(provider) -> None:
    """``thinktuning://datasets/{path}/stats`` : chemin RELATIF multi-segments.

    Tout ce qui est entre ``datasets/`` et le ``/stats`` final est le chemin :
    ``data/train.csv`` → ``dataset_stats("data/train.csv")``.
    """
    payload = json.loads(
        provider.read_resource("thinktuning://datasets/data/train.csv/stats")
    )
    assert payload["path"] == "data/train.csv"
    assert payload["row_count"] == 2


def test_read_resource_config_masks_api_keys(provider) -> None:
    """``thinktuning://config`` → JSON SANS clé API en clair (masquage dashboard)."""
    raw = provider.read_resource("thinktuning://config")
    payload = json.loads(raw)
    assert "openrouter_api_key" not in payload
    assert "hf_api_key" not in payload
    # La valeur brute NE FUIT JAMAIS dans le contenu sérialisé.
    assert "sk-or-v1-abcdef0123456789" not in raw
    assert payload["has_openrouter_api_key"] is True
    assert payload["openrouter_api_key_masked"] == "sk-or-…6789"
    assert payload["has_hf_api_key"] is False
    assert payload["hf_api_key_masked"] == ""
    # Les champs non secrets restent intacts.
    assert payload["provider"] == "openrouter"
    assert payload["model"] == "vendor/model-x"


def test_read_resource_unknown_uri_raises_not_found(provider) -> None:
    """URI inconnue / schéma étranger / segment surnuméraire → NotFoundError."""
    for uri in (
        "thinktuning://nope",
        "thinktuning://jobs/a/b",  # la route job_id refuse le '/'
        "http://jobs",
        "thinktuning://datasets/train.csv",  # sans le suffixe /stats
    ):
        with pytest.raises(NotFoundError):
            provider.read_resource(uri)


def test_read_resource_missing_job_maps_to_not_found(provider) -> None:
    """Job introuvable (erreur métier du tool) → NotFoundError actionable."""
    with pytest.raises(NotFoundError, match="Job introuvable"):
        provider.read_resource("thinktuning://jobs/ghost")


# ============================================================
# Serveur — resources/read JSON-RPC de bout en bout
# ============================================================


def test_server_resources_read_roundtrip(provider) -> None:
    """``resources/read`` → ``contents[0]`` (uri + mimeType + texte JSON)."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=provider
    )
    reply = _rpc(
        server,
        5,
        "resources/read",
        {"uri": "thinktuning://jobs"},
    )
    contents = reply["result"]["contents"]
    assert len(contents) == 1
    assert contents[0]["uri"] == "thinktuning://jobs"
    assert contents[0]["mimeType"] == "application/json"  # resource statique
    assert json.loads(contents[0]["text"])["job_count"] == 1


def test_server_resources_read_unknown_uri_is_invalid_params(provider) -> None:
    """URI inconnue → erreur JSON-RPC ``Invalid params`` (jamais un crash)."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=provider
    )
    reply = _rpc(server, 6, "resources/read", {"uri": "thinktuning://nope"})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS
    assert "Resource inconnue" in reply["error"]["message"]


def test_server_resources_read_missing_uri_param(provider) -> None:
    """``resources/read`` sans ``uri`` → Invalid params."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=provider
    )
    reply = _rpc(server, 7, "resources/read", {})
    assert reply["error"]["code"] == ErrorCode.INVALID_PARAMS


def test_server_without_provider_keeps_v010_surface() -> None:
    """Sans registre : ``resources/list`` vide + ``resources/read`` → Invalid params."""
    server = build_mcp_server(
        tool_provider=InMemoryToolProvider([]), resource_provider=_EMPTY_PROVIDER
    )
    listing = _rpc(server, 8, "resources/list")
    assert listing["result"]["resources"] == []
    read = _rpc(server, 9, "resources/read", {"uri": "thinktuning://jobs"})
    assert read["error"]["code"] == ErrorCode.INVALID_PARAMS


# ============================================================
# Sécurité URI — traversée, double-encodage, séparateurs, longueur
# ============================================================


@pytest.mark.parametrize(
    "uri",
    [
        "thinktuning://datasets/../secret.csv/stats",  # traversée relative
        "thinktuning://datasets/data/../../secret.csv/stats",  # traversée imbriquée
        "thinktuning://datasets/%2e%2e/secret.csv/stats",  # traversée encodée UNE fois
        "thinktuning://datasets/%252e%252e/x.csv/stats",  # double-encodage (% résiduel)
        "thinktuning://datasets/data%5Csecret.csv/stats",  # backslash encodé
        "thinktuning://datasets/data\\secret.csv/stats",  # backslash brut
        "thinktuning://datasets/./train.csv/stats",  # segment '.'
        "thinktuning://datasets//train.csv/stats",  # segment vide (chemin absolu)
        "thinktuning://datasets/%00train.csv/stats",  # null byte encodé
        "thinktuning://datasets/data%0Atrain.csv/stats",  # contrôle encodé
    ],
)
def test_read_resource_rejects_malicious_dataset_uris(provider, uri: str) -> None:
    """Aucune URI malveillante n'atteint le tool : NotFoundError AVANT délégation."""
    with pytest.raises(NotFoundError):
        provider.read_resource(uri)


def test_read_resource_rejects_oversized_job_id(provider) -> None:
    """Un job_id géant est refusé AVANT toute délégation (plafond 200 chars)."""
    with pytest.raises(NotFoundError, match="trop long"):
        provider.read_resource("thinktuning://jobs/" + "a" * 500)


# ============================================================
# Intégration — tools legacy RÉELS dans une sandbox temporaire
# (AGENT_SANDBOX_ROOT → tmp_path : safe_resolve + SQLite query_only vérifiés)
# ============================================================


@pytest.fixture
def sandbox_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox ISOLÉE : ``safe_resolve`` confine tous les chemins à tmp_path."""
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    return tmp_path


@pytest.fixture
def legacy_provider() -> LegacyResourceProvider:
    """Provider PAR DÉFAUT (tools legacy résolus paresseusement au 1er appel)."""
    return build_legacy_resource_provider()


def _seed_jobs_db(sandbox_root: Path, job_id: str = "job-123") -> None:
    """Crée experiments/jobs.db (schéma minimal lu par job_list/job_get)."""
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
        conn.commit()
    finally:
        conn.close()


def test_real_jobs_db_list_and_get(sandbox_root, legacy_provider) -> None:
    """jobs.db réelle : ``thinktuning://jobs`` + ``thinktuning://jobs/{id}`` en JSON."""
    _seed_jobs_db(sandbox_root)
    listing = json.loads(legacy_provider.read_resource("thinktuning://jobs"))
    assert listing["job_count"] == 1
    assert listing["jobs"][0]["job_id"] == "job-123"
    assert listing["jobs"][0]["status"] == "completed"

    job = json.loads(legacy_provider.read_resource("thinktuning://jobs/job-123"))
    assert job["status"] == "completed"
    assert job["model_path"] == "experiments/models/20260908T000000Z"


def test_real_jobs_db_unknown_job_is_not_found(sandbox_root, legacy_provider) -> None:
    """Job absent de la base réelle → NotFoundError avec le message du tool."""
    _seed_jobs_db(sandbox_root)
    with pytest.raises(NotFoundError, match="Job introuvable"):
        legacy_provider.read_resource("thinktuning://jobs/ghost")


def test_real_jobs_db_connection_is_query_only(sandbox_root) -> None:
    """« query_only pour SQL » : la connexion des tools refuse toute écriture.

    Vérifie la garantie portée par délégation : ``mode=ro`` + ``PRAGMA
    query_only = ON`` (``ia/tools/ml_tools._connect_readonly``) → toute
    écriture lève ``sqlite3.OperationalError``.
    """
    from ia.tools.ml_tools import _connect_readonly
    from ia.tools.sandbox import safe_resolve

    _seed_jobs_db(sandbox_root)
    conn = _connect_readonly(safe_resolve(Path("experiments") / "jobs.db"))
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO jobs VALUES ('evil', '{}', 0)")
    finally:
        conn.close()


def test_real_dataset_stats_roundtrip(sandbox_root, legacy_provider) -> None:
    """Dataset CSV réel : profil complet via ``thinktuning://datasets/.../stats``."""
    pytest.importorskip("pandas")
    data_dir = sandbox_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "train.csv").write_text(
        "text,label,lang_code\nbonjour,positive,fr\ntriste,negative,en\n",
        encoding="utf-8",
    )
    stats = json.loads(
        legacy_provider.read_resource("thinktuning://datasets/data/train.csv/stats")
    )
    assert stats["row_count"] == 2
    assert stats["label_counts"] == {"positive": 1, "negative": 1}
    assert stats["lang_code_counts"] == {"fr": 1, "en": 1}


def test_real_dataset_missing_is_not_found(sandbox_root, legacy_provider) -> None:
    """Dataset absent → ``safe_resolve(must_exist=True)`` → NotFoundError."""
    with pytest.raises(NotFoundError, match="Introuvable"):
        legacy_provider.read_resource("thinktuning://datasets/data/ghost.csv/stats")


def test_real_dataset_escape_is_blocked_by_safe_resolve(
    sandbox_root, legacy_provider
) -> None:
    """Deuxième ligne de défense : un chemin ABSOLU hors sandbox est refusé.

    L'URI passe la validation de segments (chemin absolu multi-segments) mais
    ``safe_resolve`` (délégation ``dataset_stats``) refuse l'évasion →
    ``PermissionError`` → ``NotFoundError`` (fail-closed, pas d'oracle).
    """
    outside = sandbox_root.parent / "outside-secret.csv"
    outside.write_text("text,label\nx,positive\n", encoding="utf-8")
    try:
        uri = f"thinktuning://datasets/{outside.as_posix()}/stats"
        with pytest.raises(NotFoundError):
            legacy_provider.read_resource(uri)
    finally:
        outside.unlink()


def test_real_models_resource_lists_sandbox_versions(
    sandbox_root, legacy_provider
) -> None:
    """``thinktuning://models`` scanne la sandbox réelle (conventions versioning)."""
    version_dir = sandbox_root / "experiments" / "models" / "20260908T000000Z"
    version_dir.mkdir(parents=True, exist_ok=True)
    (version_dir / "model.safetensors").write_bytes(b"weights")
    payload = json.loads(legacy_provider.read_resource("thinktuning://models"))
    assert payload["version_count"] == 1
    assert payload["versions"][0]["name"] == "20260908T000000Z"
    assert payload["versions"][0]["active"] is True
