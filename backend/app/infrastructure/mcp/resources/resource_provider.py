# project/app/infrastructure/mcp/resources/resource_provider.py
"""Provider MCP des resources « thinktuning:// » — tâches 8 (S3) + 13 (S5, v1.1.0).

Implémente le port domaine ``MCPResourceRegistryPort`` (tâche 3) : chaque
resource ThinkTuning est une VUE LECTURE-SEULE résolue par délégation aux
tools internes — aucune règle réimplémentée (même principe que
``legacy_tool_provider``) :

    URI                                    → source interne            → contenu
    thinktuning://jobs                     → job_list()                → JSON
    thinktuning://jobs/{job_id}/logs       → job_get + job_logs        → JSON
    thinktuning://jobs/{job_id}            → job_get(job_id)           → JSON
    thinktuning://models                   → model_versions()          → JSON
    thinktuning://models/{version}/info    → scan + rapport d'entraînement → JSON
    thinktuning://datasets/{path}/stats    → dataset_stats(path)       → JSON
    thinktuning://datasets/{path}/preview  → head_file(path)           → JSON
    thinktuning://config                   → agent_config()            → JSON
    thinktuning://metrics/{job_id}         → job_get + train_metrics   → JSON
    thinktuning://health                   → run_health_check (v1)     → JSON

SÉCURITÉ (checklists tâches 8 et 13) — défense en profondeur, fail-closed :

    1. URI : parsing strict par routes (regex ancrées, ordre déterministe) ;
       toute URI non reconnue → ``NotFoundError`` (pas d'oracle d'inventaire) ;
    2. segments : décodage percent-encoding en UNE passe (un ``%`` résiduel
       est rejeté → anti double-encodage), ``\\`` et caractères de contrôle
       interdits, segments ``''`` / ``'.'`` / ``'..'`` interdits, longueur
       plafonnée — la traversée de chemin est refusée AVANT toute I/O ;
    3. chemins : ``dataset_stats`` / ``head_file`` réappliquent
       ``ia.tools.sandbox.safe_resolve`` et la lecture des métadonnées de
       modèle aussi (aucun chemin ne sort de ``AGENT_SANDBOX_ROOT``) — porté
       PAR DÉLÉGATION (deuxième ligne de défense derrière la validation d'URI) ;
    4. SQL : ``job_list`` / ``job_get`` ET la lecture des métriques ouvrent
       ``experiments/jobs.db`` en ``mode=ro`` + ``PRAGMA query_only``
       (``ia/tools/ml_tools.py``) — toute écriture est refusée par SQLite ;
       requêtes paramétrées (``?``) ; la resource metrics ne crée JAMAIS la
       base (contrairement au store applicatif qui fait ``_ensure_db``) ;
    5. secrets : ``thinktuning://config`` masque les clés API
       (``has_*`` + ``*_masked``, convention du dashboard
       ``api/routes/agent.py::_settings_payload``) — JAMAIS en clair ;
       ``thinktuning://datasets/{path}/preview`` n'ouvre que les formats
       dataset déclarés (``_DATASET_SUFFIXES``, même règle que
       ``dataset_stats``) — un ``.env`` est refusé AVANT toute lecture.

Résolution PARESSEUSE des tools legacy (premier appel) : la construction du
provider ne fait AUCUNE I/O ni import lourd (``fastapi``/``requests`` ne sont
importés que si ``thinktuning://config`` ou ``thinktuning://health`` est lu) ;
``list_resources()`` est de la métadonnée pure. Les tests injectent leurs
propres callables.

Erreurs : URI inconnue, job/version/dataset introuvable, fichier absent,
chemin hors sandbox, format non supporté → ``NotFoundError`` (message
actionnable préservé, fail-closed) ; ``RuntimeError`` et exceptions
inattendues PROPAGENT (le serveur MCP les traduit en ``Internal error``,
aucune fuite de détail interne).
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote

from app.domain.entities.mcp import (
    MCPPromptArgument,
    MCPResource,
    MCPResourceTemplate,
)
from app.domain.errors import NotFoundError
from app.domain.ports.mcp_ports import MCPResourceRegistryPort

logger = logging.getLogger("thinktuning.mcp.resources")

__all__ = [
    "LegacyResourceProvider",
    "RESOURCE_MIME_TYPE",
    "RESOURCE_SCHEME",
    "build_legacy_resource_provider",
]

# Type MIME unique de la surface « thinktuning:// » : les 10 resources
# sérialisent en JSON (tâche 8 : 5, tâche 13 : 5 supplémentaires).
RESOURCE_MIME_TYPE = "application/json"

# Schéma URI propriétaire de la surface ThinkTuning (strictement préfixe).
RESOURCE_SCHEME = "thinktuning://"

# Plafonds post-décodage (anti-abus : une URI de plusieurs Ko ne doit jamais
# atteindre un tool ni saturer un message d'erreur).
MAX_JOB_ID_CHARS = 200
MAX_DATASET_PATH_CHARS = 500
MAX_MODEL_VERSION_CHARS = 200
# Plafond de lignes de ``thinktuning://datasets/{path}/preview`` : la resource
# resserre ``head_file`` à un volume « aperçu » (le tool seul est plus généreux).
MAX_PREVIEW_LINES = 50
_MAX_URI_IN_ERROR_CHARS = 120

# Clés de ``agent_config()`` portant un SECRET : retirées de la vue MCP et
# remplacées par ``has_<key>`` (booléen) + ``<key>_masked`` (affichage).
_REDACTED_CONFIG_KEYS: frozenset[str] = frozenset({"openrouter_api_key", "hf_api_key"})


# ---------------------------------------------------------------------------
# Métadonnées listées (resources/list) + routes de résolution (resources/read)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Segment:
    """Spécification de validation d'un segment paramétré d'URI.

    Attributes:
        group:       nom du groupe capturé par la regex de la route ;
        label:       libellé humain (messages d'erreur actionnables) ;
        max_chars:   plafond de longueur APRES décodage percent-encoding ;
        allow_slash: ``False`` → un seul segment (``job_id``) ; ``True`` →
            chemin RELATIF multi-segments (``data/train.csv``).
    """

    group: str
    label: str
    max_chars: int
    allow_slash: bool


@dataclass(frozen=True, slots=True)
class _Route:
    """Route de résolution : métadonnée listée + motif ancré + tool interne.

    L'ordre du tuple ``_ROUTES`` est DÉTERMINISTE : la première route dont le
    motif correspond gagne (les motifs statiques sont plus spécifiques que
    les dynamiques et sont testés en premier).
    """

    resource: MCPResource
    pattern: re.Pattern[str]
    # cible interne : jobs | job | job_logs | models | model_info |
    #                 dataset_stats | dataset_preview | config |
    #                 job_metrics | health
    kind: str
    dynamic: bool  # True → gabarit (listé aussi comme MCPResourceTemplate)
    segments: tuple[_Segment, ...] = ()


_ROUTES: tuple[_Route, ...] = (
    _Route(
        resource=MCPResource(
            uri="thinktuning://jobs",
            name="jobs",
            description=(
                "Liste des jobs d'entraînement les plus récents "
                "(20 derniers, triés par mise à jour ; lecture seule)."
            ),
        ),
        pattern=re.compile(r"^thinktuning://jobs$"),
        kind="jobs",
        dynamic=False,
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://jobs/{job_id}/logs",
            name="job_logs",
            description=(
                "Logs d'un job d'entraînement (lignes capturées en mémoire par "
                "le thread du job — même source que le WebSocket /train/stream ; "
                "vides si le job date d'avant le démarrage de l'API)."
            ),
        ),
        # Placée AVANT « jobs/{job_id} » (ordre déterministe) : motif plus
        # spécifique (suffixe /logs) testé en premier.
        pattern=re.compile(r"^thinktuning://jobs/(?P<job_id>[^/]+)/logs$"),
        kind="job_logs",
        dynamic=True,
        segments=(
            _Segment(
                group="job_id",
                label="job_id",
                max_chars=MAX_JOB_ID_CHARS,
                allow_slash=False,
            ),
        ),
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://jobs/{job_id}",
            name="job",
            description=(
                "Payload COMPLET d'un job d'entraînement : hyperparamètres, "
                "statut, erreur, chemin du modèle (lecture seule)."
            ),
        ),
        pattern=re.compile(r"^thinktuning://jobs/(?P<job_id>[^/]+)$"),
        kind="job",
        dynamic=True,
        segments=(
            _Segment(
                group="job_id",
                label="job_id",
                max_chars=MAX_JOB_ID_CHARS,
                allow_slash=False,
            ),
        ),
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://models",
            name="models",
            description=(
                "Versions de modèles entraînés visibles dans la sandbox "
                "(la plus récente est active par défaut ; lecture seule)."
            ),
        ),
        pattern=re.compile(r"^thinktuning://models$"),
        kind="models",
        dynamic=False,
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://models/{version}/info",
            name="model_info",
            description=(
                "Métadonnées d'une version de modèle : artefacts (tailles), "
                "rapport d'entraînement (hyperparamètres, métriques finales), "
                "mappings de labels, drapeau actif (lecture seule)."
            ),
        ),
        pattern=re.compile(r"^thinktuning://models/(?P<version>[^/]+)/info$"),
        kind="model_info",
        dynamic=True,
        segments=(
            _Segment(
                group="version",
                label="version de modèle",
                max_chars=MAX_MODEL_VERSION_CHARS,
                allow_slash=False,
            ),
        ),
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://datasets/{path}/stats",
            name="dataset_stats",
            description=(
                "Profil d'un dataset CSV/TSV/JSONL sous la sandbox : lignes, "
                "colonnes, valeurs manquantes, distributions label/langue "
                "(chemin RELATIF à la sandbox, safe_resolve appliqué)."
            ),
        ),
        pattern=re.compile(r"^thinktuning://datasets/(?P<path>.+)/stats$"),
        kind="dataset_stats",
        dynamic=True,
        segments=(
            _Segment(
                group="path",
                label="chemin du dataset",
                max_chars=MAX_DATASET_PATH_CHARS,
                allow_slash=True,
            ),
        ),
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://datasets/{path}/preview",
            name="dataset_preview",
            description=(
                "Aperçu d'un dataset CSV/TSV/JSONL sous la sandbox : premières "
                "lignes (50 max, plafonné). Même règle de format que /stats — "
                "tout autre fichier (ex. .env) est refusé avant lecture."
            ),
        ),
        pattern=re.compile(r"^thinktuning://datasets/(?P<path>.+)/preview$"),
        kind="dataset_preview",
        dynamic=True,
        segments=(
            _Segment(
                group="path",
                label="chemin du dataset",
                max_chars=MAX_DATASET_PATH_CHARS,
                allow_slash=True,
            ),
        ),
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://config",
            name="config",
            description=(
                "Configuration courante de l'agent (provider, modèle, URLs, "
                "timeout) — clés API masquées, jamais exposées en clair."
            ),
        ),
        pattern=re.compile(r"^thinktuning://config$"),
        kind="config",
        dynamic=False,
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://metrics/{job_id}",
            name="job_metrics",
            description=(
                "Métriques d'entraînement par epoch d'un job (loss, F1 macro, "
                "accuracy — table train_metrics lue en lecture seule stricte)."
            ),
        ),
        pattern=re.compile(r"^thinktuning://metrics/(?P<job_id>[^/]+)$"),
        kind="job_metrics",
        dynamic=True,
        segments=(
            _Segment(
                group="job_id",
                label="job_id",
                max_chars=MAX_JOB_ID_CHARS,
                allow_slash=False,
            ),
        ),
    ),
    _Route(
        resource=MCPResource(
            uri="thinktuning://health",
            name="health",
            description=(
                "Santé du système : modèle disponible, jobs actifs, mode "
                "maintenance (use case v1, shape identique au /health legacy)."
            ),
        ),
        pattern=re.compile(r"^thinktuning://health$"),
        kind="health",
        dynamic=False,
    ),
)

# Gabarits MCP des routes paramétrées (préparation ``resources/templates/list``
# — hors contrat tâche 8, aligné sur l'entité ``MCPResourceTemplate`` du domaine).
_TEMPLATE_ARGUMENTS: dict[str, tuple[MCPPromptArgument, ...]] = {
    "job": (
        MCPPromptArgument(
            name="job_id",
            description="Identifiant du job (catalogué par thinktuning://jobs).",
            required=True,
        ),
    ),
    "job_logs": (
        MCPPromptArgument(
            name="job_id",
            description="Identifiant du job (catalogué par thinktuning://jobs).",
            required=True,
        ),
    ),
    "job_metrics": (
        MCPPromptArgument(
            name="job_id",
            description="Identifiant du job (catalogué par thinktuning://jobs).",
            required=True,
        ),
    ),
    "model_info": (
        MCPPromptArgument(
            name="version",
            description="Version de modèle (cataloguée par thinktuning://models).",
            required=True,
        ),
    ),
    "dataset_stats": (
        MCPPromptArgument(
            name="path",
            description="Chemin RELATIF du dataset sous la sandbox (data/train.csv).",
            required=True,
        ),
    ),
    "dataset_preview": (
        MCPPromptArgument(
            name="path",
            description="Chemin RELATIF du dataset sous la sandbox (data/train.csv).",
            required=True,
        ),
    ),
}


# ---------------------------------------------------------------------------
# Résolution d'URI — parsing strict + validation de segments (fail-closed)
# ---------------------------------------------------------------------------


def _short_uri(uri: str) -> str:
    """Tronque une URI pour les messages d'erreur (jamais plusieurs Ko)."""
    if len(uri) <= _MAX_URI_IN_ERROR_CHARS:
        return uri
    return uri[: _MAX_URI_IN_ERROR_CHARS - 1] + "…"


def _decode_segment(raw: str, *, segment: _Segment) -> str:
    """Décode et valide un segment d'URI (fail-closed, message actionable).

    Ordre volontaire :
        1. ``unquote`` UNE passe — un ``%`` résiduel est rejeté à l'étape 2
           (anti double-encodage : ``%252e%252e`` ne devient jamais ``..``) ;
        2. rejet ``%`` / ``\\`` / caractères de contrôle / null ;
        3. rejet des segments ``''`` / ``'.'`` / ``'..'`` (traversée) et, hors
           mode chemin, de tout séparateur ``/`` ;
        4. plafond de longueur.
    """
    decoded = unquote(raw)
    if "%" in decoded:
        raise NotFoundError(
            f"{segment.label} : encodage multiple interdit dans l'URI "
            f"('{_short_uri(raw)}')"
        )
    if "\\" in decoded or "\x00" in decoded or any(
        ord(c) < 32 or ord(c) == 127 for c in decoded
    ):
        raise NotFoundError(
            f"{segment.label} : séparateur backslash ou caractère de contrôle "
            f"interdit dans l'URI ('{_short_uri(raw)}')"
        )
    parts = decoded.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise NotFoundError(
            f"{segment.label} : segments vides, '.' ou '..' interdits dans "
            f"l'URI ('{_short_uri(raw)}')"
        )
    if not segment.allow_slash and len(parts) > 1:
        raise NotFoundError(
            f"{segment.label} : séparateur '/' interdit dans l'URI "
            f"('{_short_uri(raw)}')"
        )
    if len(decoded) > segment.max_chars:
        raise NotFoundError(
            f"{segment.label} : trop long ({len(decoded)} > "
            f"{segment.max_chars} caractères)"
        )
    return decoded


def _match_route(uri: str) -> tuple[_Route, dict[str, str]]:
    """Apparie une URI concrète à une route → (route, paramètres validés).

    Lève ``NotFoundError`` pour tout ce qui n'est pas une URI
    ``thinktuning://`` reconnue (schéma étranger inclus) — indiscernable
    d'une resource absente (aucun oracle d'inventaire).
    """
    if not isinstance(uri, str) or not uri.startswith(RESOURCE_SCHEME):
        raise NotFoundError(
            f"Resource inconnue : '{_short_uri(str(uri))}' "
            f"(schéma attendu : '{RESOURCE_SCHEME}')"
        )
    for route in _ROUTES:
        match = route.pattern.match(uri)
        if match is None:
            continue
        params = {
            segment.group: _decode_segment(match.group(segment.group), segment=segment)
            for segment in route.segments
        }
        return route, params
    raise NotFoundError(
        f"Resource inconnue : '{_short_uri(uri)}'. Resources exposées : "
        "thinktuning://jobs, thinktuning://jobs/{job_id}, "
        "thinktuning://jobs/{job_id}/logs, thinktuning://models, "
        "thinktuning://models/{version}/info, thinktuning://datasets/{path}/stats, "
        "thinktuning://datasets/{path}/preview, thinktuning://config, "
        "thinktuning://metrics/{job_id}, thinktuning://health."
    )


# ---------------------------------------------------------------------------
# Résolution paresseuse des tools internes (aucun import lourd à la construction)
# ---------------------------------------------------------------------------


def _lazy_legacy_tool(name: str) -> Callable[[], Callable[..., dict]]:
    """Résout un tool legacy ``ia.tools`` au PREMIER appel (zéro I/O avant)."""

    def _resolve() -> Callable[..., dict]:
        from ia.tools.tool_registry import TOOLS  # import paresseux (délégation)

        func = TOOLS.get(name)
        if func is None:
            raise RuntimeError(
                f"Tool legacy « {name} » introuvable dans ia.tools.tool_registry."
            )
        return func

    return _resolve


def _lazy_agent_config() -> Callable[[], Callable[..., dict]]:
    """Résout ``core.agent_cache.agent_config`` au premier appel.

    Import paresseux VOLONTAIRE : ``core.agent_cache`` tire fastapi/requests
    et la pile agentique — seules ``thinktuning://config`` et
    ``thinktuning://health`` paient ce coût, les autres lectures non.
    """

    def _resolve() -> Callable[..., dict]:
        from core.agent_cache import agent_config  # import paresseux (lourd)

        return agent_config

    return _resolve


def _lazy_job_logs() -> Callable[[], Callable[..., dict]]:
    """Résout ``core.job_logs.get_logs`` au premier appel (buffer mémoire).

    Même source que le WebSocket ``/train/stream`` : les lignes sont celles
    capturées par le thread du job. Elles sont PERDUES au redémarrage de
    l'API — un job connu peut donc légitimement n'avoir aucune ligne (ce
    n'est PAS une erreur, contrairement à un job inconnu).
    """

    def _resolve() -> Callable[..., dict]:
        from core.job_logs import get_logs  # import paresseux (léger, stdlib)

        def _fetch(job_id: str) -> dict:
            entries = get_logs(str(job_id))
            return {"job_id": str(job_id), "line_count": len(entries), "logs": entries}

        return _fetch

    return _resolve


def _lazy_job_metrics() -> Callable[[], Callable[..., dict]]:
    """Résout la lecture des métriques par epoch au premier appel.

    Miroir LECTURE SEULE de ``core/job_store.py::get_job_metrics`` (même
    SELECT paramétré, mêmes colonnes) sur la connexion ``mode=ro`` +
    ``PRAGMA query_only`` déléguée de ``ia/tools/ml_tools`` : la resource ne
    crée JAMAIS la base ni n'écrit (le store applicatif fait ``_ensure_db``).
    """

    def _resolve() -> Callable[..., dict]:
        from ia.tools.ml_tools import JOBS_DB_RELATIVE, _connect_readonly
        from ia.tools.sandbox import safe_resolve

        def _fetch(job_id: str) -> dict:
            db_path = safe_resolve(JOBS_DB_RELATIVE)
            if not db_path.is_file():
                raise FileNotFoundError(f"Aucune base de jobs : {db_path}")
            conn = _connect_readonly(db_path)
            try:
                try:
                    rows = conn.execute(
                        "SELECT job_id, epoch, loss, f1_macro, accuracy "
                        "FROM train_metrics WHERE job_id = ? ORDER BY epoch ASC",
                        (str(job_id),),
                    ).fetchall()
                except sqlite3.OperationalError as exc:
                    if "no such table" not in str(exc).lower():
                        raise
                    # Base antérieure à la table train_metrics (rétro-compat) :
                    # un job connu sans métriques est un état nominal.
                    rows = []
            finally:
                conn.close()
            return {
                "job_id": str(job_id),
                "epoch_count": len(rows),
                "metrics": [
                    {
                        "epoch": epoch,
                        "loss": loss,
                        "f1_macro": f1_macro,
                        "accuracy": accuracy,
                    }
                    for _, epoch, loss, f1_macro, accuracy in rows
                ],
            }

        return _fetch

    return _resolve


def _read_json_file(path: Path) -> dict | None:
    """Lit un fichier JSON de métadonnées (``None`` si absent, erreur lisible sinon)."""
    if not path.is_file():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(f"JSON illisible ({path.name}) : {exc}") from exc


def _read_model_version_info(version: str) -> dict:
    """Métadonnées d'une version de modèle (délégation scan + lecture sandbox).

    La PRÉSENCE et le drapeau ``active`` viennent du scan délégué
    (``model_versions`` — mêmes conventions que ``core/model_versioning``) ;
    les artefacts et le rapport d'entraînement sont lus sous la racine
    renvoyée par ce scan (déjà confinée par ``safe_resolve``), revalidée une
    seconde fois par ``safe_resolve(must_exist=True)`` (défense en profondeur).
    """
    from ia.tools.ml_tools import model_versions as _model_versions_tool
    from ia.tools.sandbox import safe_resolve

    listing = _model_versions_tool()
    entry = next(
        (item for item in listing.get("versions", []) if item.get("name") == version),
        None,
    )
    if entry is None:
        raise ValueError(
            f"Version de modèle inconnue : '{version}'. Utilisez "
            "thinktuning://models pour voir les versions disponibles."
        )
    version_dir = safe_resolve(
        Path(str(listing["model_root"])) / version, must_exist=True
    )
    artifacts: list[dict[str, Any]] = [
        {"name": child.name, "size_bytes": child.stat().st_size}
        for child in version_dir.iterdir()
        if child.is_file()
    ]
    artifacts.sort(key=lambda artifact: str(artifact["name"]))
    report = _read_json_file(version_dir / "training_report.json")
    payload: dict = {
        "version": version,
        "active": bool(entry.get("active")),
        "path": version_dir.as_posix(),
        "artifact_count": len(artifacts),
        "artifacts": artifacts,
        "training_report": report,
        "id2label": _read_json_file(version_dir / "id2label.json"),
    }
    if report is None:
        payload["message"] = (
            "Aucun training_report.json : version créée hors pipeline "
            "d'entraînement (poids/tokenizer seuls)."
        )
    return payload


def _lazy_system_health() -> Callable[[], Callable[..., dict]]:
    """Résout le use case de santé v1 au premier appel (adaptateurs legacy).

    Délégation EXACTE à ``app.application.health_usecase.run_health_check``
    avec les adaptateurs par défaut de la composition root : le payload est
    le ``HealthSnapshot`` du domaine (shape identique au ``/health`` legacy,
    contrat verrouillé par tests de non-régression) — zéro règle dupliquée.

    Import paresseux VOLONTAIRE : la chaîne tire fastapi (via l'état de
    maintenance legacy) et le scan des modèles — seule ``thinktuning://health``
    paie ce coût.
    """

    def _resolve() -> Callable[..., dict]:
        from dataclasses import asdict

        from app.application.health_usecase import run_health_check
        from app.infrastructure.ml.model_repository_adapter import (
            build_default_repository,
        )
        from app.infrastructure.system_status_adapter import (
            build_default_system_status,
        )

        def _snapshot() -> dict:
            snapshot = run_health_check(
                repository=build_default_repository(),
                status=build_default_system_status(),
            )
            return asdict(snapshot)

        return _snapshot

    return _resolve


def _mask_key(key: str) -> str:
    """Masque une clé API pour l'affichage (« sk-or-v1 » → « sk-or-…abcd »).

    Miroir de ``api/routes/agent.py::_mask_key`` : la convention est
    réimplémentée ici car ``app/*`` n'importe jamais ``api/*`` (dépendances
    hexagonales pointant vers l'intérieur).
    """
    key = key or ""
    if not key:
        return ""
    if len(key) <= 8:
        return "*" * len(key)
    return f"{key[:6]}…{key[-4:]}"


def _sanitize_agent_config(config: dict) -> dict:
    """Retire les SECRETS de la config agent (clés API jamais exposées via MCP).

    Convention identique au dashboard (``has_<key>`` + ``<key>_masked``) :
    la valeur brute est retirée, seule sa présence et son masque d'affichage
    transitent sur la surface MCP.
    """
    sanitized = dict(config)
    for key in sorted(_REDACTED_CONFIG_KEYS):
        value = str(sanitized.pop(key, "") or "")
        sanitized[f"has_{key}"] = bool(value.strip())
        sanitized[f"{key}_masked"] = _mask_key(value)
    return sanitized


def _payload_to_json(payload: Any) -> str:
    """Sérialise le retour d'un tool interne en JSON (convention MCP texte).

    Mêmes options que ``legacy_tool_provider._result_to_text`` : lisible,
    non échappé, tolérant aux valeurs non JSON (``default=str``).
    """
    return json.dumps(payload, ensure_ascii=False, indent=2, default=str)


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------


class LegacyResourceProvider(MCPResourceRegistryPort):
    """``MCPResourceRegistryPort`` — les 10 resources « thinktuning:// » (tâches 8 + 13).

    La liste (``list_resources``) est de la MÉTADONNÉE PURE : aucune I/O, aucun
    import de tool. La lecture (``read_resource``) résout l'URI par routes
    strictes puis DÉLÈGUE au tool interne correspondant — les garde-fous
    (``safe_resolve``, SQLite ``mode=ro`` + ``query_only``) sont portés par
    délégation, pas dupliqués.

    Args:
        job_list / job_get / job_logs / model_versions / model_info /
        dataset_stats / dataset_preview / agent_config / job_metrics /
        system_health: implémentations injectables (tests, déploiements
            spécifiques) ; ``None`` → résolution PARESSEUSE au premier appel
            (``ia.tools.tool_registry.TOOLS``, ``core.job_logs``,
            ``core.agent_cache``, use case santé v1 + adaptateurs legacy).
            ``model_info`` et ``system_health`` remplacent ENTIÈREMENT la
            composition par défaut de leur resource (signature ``version``
            et ``()`` respectivement).
    """

    def __init__(
        self,
        *,
        job_list: Callable[..., dict] | None = None,
        job_get: Callable[..., dict] | None = None,
        job_logs: Callable[..., dict] | None = None,
        model_versions: Callable[..., dict] | None = None,
        model_info: Callable[..., dict] | None = None,
        dataset_stats: Callable[..., dict] | None = None,
        dataset_preview: Callable[..., dict] | None = None,
        agent_config: Callable[..., dict] | None = None,
        job_metrics: Callable[..., dict] | None = None,
        system_health: Callable[..., dict] | None = None,
    ) -> None:
        self._resolvers: dict[str, Callable[[], Callable[..., dict]]] = {
            "jobs": (lambda: job_list) if job_list else _lazy_legacy_tool("job_list"),
            "job": (lambda: job_get) if job_get else _lazy_legacy_tool("job_get"),
            "job_logs": (lambda: job_logs) if job_logs else _lazy_job_logs(),
            "models": (
                (lambda: model_versions)
                if model_versions
                else _lazy_legacy_tool("model_versions")
            ),
            "model_info": (
                (lambda: model_info)
                if model_info
                else (lambda: _read_model_version_info)
            ),
            "dataset_stats": (
                (lambda: dataset_stats)
                if dataset_stats
                else _lazy_legacy_tool("dataset_stats")
            ),
            "dataset_preview": (
                (lambda: dataset_preview)
                if dataset_preview
                else _lazy_legacy_tool("head_file")
            ),
            "config": (lambda: agent_config) if agent_config else _lazy_agent_config(),
            "job_metrics": (lambda: job_metrics) if job_metrics else _lazy_job_metrics(),
            "health": (
                (lambda: system_health) if system_health else _lazy_system_health()
            ),
        }

    # --- Métadonnées (resources/list) — pur, sans I/O -----------------------

    def list_resources(self) -> list[MCPResource]:
        """Les 10 resources « thinktuning:// » (4 statiques + 6 paramétrées)."""
        return [route.resource for route in _ROUTES]

    def list_resource_templates(self) -> list[MCPResourceTemplate]:
        """Gabarits des resources paramétrées (``resources/templates/list``).

        Tâche 13 : 6 gabarits avec arguments typés (``job_id``, ``version``,
        ``path``) — ``MCPResourceTemplate`` du domaine, hors contrat minimal
        du port (tâche 8).
        """
        return [
            MCPResourceTemplate(
                uri_template=route.resource.uri,
                name=route.resource.name,
                description=route.resource.description,
                mime_type=route.resource.mime_type,
                arguments=_TEMPLATE_ARGUMENTS.get(route.kind, ()),
            )
            for route in _ROUTES
            if route.dynamic
        ]

    # --- Lecture (resources/read) — résolution URI → tool interne -----------

    def read_resource(self, uri: str) -> str:
        """Résout une URI « thinktuning:// » en contenu JSON (lecture seule).

        Lève ``NotFoundError`` pour une URI inconnue ET pour toute erreur
        métier client-réparable (job/version de modèle introuvable, dataset
        absent, chemin hors sandbox, format non supporté) — le message
        actionable du tool est préservé, aucun oracle d'inventaire n'est offert.
        """
        route, params = _match_route(uri)
        try:
            payload = self._execute(route.kind, params)
        except NotFoundError:
            raise
        except (ValueError, OSError) as exc:
            # Erreur MÉTIER (ValueError : job introuvable, format non supporté,
            # fichier trop volumineux ; OSError : fichier/base absent,
            # PermissionError = chemin hors sandbox) → 404 fail-closed : le
            # client peut corriger l'URI, le transport reste sain.
            logger.info("MCP resource %s indisponible : %s", uri, exc)
            raise NotFoundError(
                f"Resource « {_short_uri(uri)} » indisponible : {exc}"
            ) from exc
        return _payload_to_json(payload)

    def _execute(self, kind: str, params: dict[str, str]) -> Any:
        """Délègue au tool interne (résolution paresseuse au premier appel)."""
        if kind == "config":
            # Secrets JAMAIS en clair sur la surface MCP (clés API masquées).
            return _sanitize_agent_config(self._resolvers[kind]()())
        if kind in ("job_logs", "job_metrics"):
            # Existence d'abord (délégation job_get) : un job inconnu est une
            # erreur métier client-réparable (ValueError/FileNotFoundError →
            # 404) levée AVANT de toucher aux logs/métriques ; un job connu
            # sans logs/métriques reste un état nominal (payload vide).
            self._resolvers["job"]()(**params)
            return self._resolvers[kind]()(**params)
        if kind == "dataset_preview":
            # Même règle de format que dataset_stats (constante déléguée) :
            # un aperçu n'ouvre que les datasets déclarés — un .env est refusé
            # AVANT toute lecture (fail-closed, message actionable).
            from ia.tools.ml_tools import _DATASET_SUFFIXES

            suffix = PurePosixPath(params["path"]).suffix.lower()
            if suffix not in _DATASET_SUFFIXES:
                raise ValueError(
                    f"Format non supporté : '{suffix or 'aucun'}'. "
                    f"Formats : {sorted(_DATASET_SUFFIXES)} (CSV/TSV/JSONL)."
                )
            return self._resolvers[kind]()(
                path=params["path"], max_lines=MAX_PREVIEW_LINES
            )
        return self._resolvers[kind]()(**params)


def build_legacy_resource_provider() -> LegacyResourceProvider:
    """Provider par défaut des 10 resources « thinktuning:// » (tâches 8 + 13).

    Construction SANS I/O ni import lourd : les tools internes sont résolus
    paresseusement au premier ``read_resource``.
    """
    return LegacyResourceProvider()
