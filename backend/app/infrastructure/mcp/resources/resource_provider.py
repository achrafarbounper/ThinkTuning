# project/app/infrastructure/mcp/resources/resource_provider.py
"""Provider MCP des resources « thinktuning:// » — tâche 8 (S3, v1.0.0 Beta).

Implémente le port domaine ``MCPResourceRegistryPort`` (tâche 3) : chaque
resource ThinkTuning est une VUE LECTURE-SEULE résolue par délégation aux
tools internes — aucune règle réimplémentée (même principe que
``legacy_tool_provider``) :

    URI                                   → tool interne         → contenu
    thinktuning://jobs                    → job_list()           → JSON
    thinktuning://jobs/{job_id}           → job_get(job_id)      → JSON
    thinktuning://models                  → model_versions()     → JSON
    thinktuning://datasets/{path}/stats   → dataset_stats(path)  → JSON
    thinktuning://config                  → agent_config()       → JSON

SÉCURITÉ (checklist tâche 8) — défense en profondeur, fail-closed :

    1. URI : parsing strict par routes (regex ancrées, ordre déterministe) ;
       toute URI non reconnue → ``NotFoundError`` (pas d'oracle d'inventaire) ;
    2. segments : décodage percent-encoding en UNE passe (un ``%`` résiduel
       est rejeté → anti double-encodage), ``\\`` et caractères de contrôle
       interdits, segments ``''`` / ``'.'`` / ``'..'`` interdits, longueur
       plafonnée — la traversée de chemin est refusée AVANT toute I/O ;
    3. chemins : ``dataset_stats`` réapplique ``ia.tools.sandbox.safe_resolve``
       (aucun chemin ne sort de ``AGENT_SANDBOX_ROOT``) — porté PAR DÉLÉGATION
       (deuxième ligne de défense derrière la validation d'URI) ;
    4. SQL : ``job_list`` / ``job_get`` ouvrent ``experiments/jobs.db`` en
       ``mode=ro`` + ``PRAGMA query_only`` (``ia/tools/ml_tools.py``) — toute
       écriture est refusée par SQLite ; requêtes paramétrées (``?``) ;
    5. secrets : ``thinktuning://config`` masque les clés API
       (``has_*`` + ``*_masked``, convention du dashboard
       ``api/routes/agent.py::_settings_payload``) — JAMAIS en clair.

Résolution PARESSEUSE des tools legacy (premier appel) : la construction du
provider ne fait AUCUNE I/O ni import lourd (``fastapi``/``requests`` ne sont
importés que si ``thinktuning://config`` est lu) ; ``list_resources()`` est de
la métadonnée pure. Les tests injectent leurs propres callables.

Erreurs : URI inconnue, job/dataset introuvable, fichier absent, chemin hors
sandbox, format non supporté → ``NotFoundError`` (message actionable préservé,
fail-closed) ; ``RuntimeError`` et exceptions inattendues PROPAGENT (le serveur
MCP les traduit en ``Internal error``, aucune fuite de détail interne).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
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

# Type MIME unique de la surface v1.0.0 : les 5 resources sérialisent en JSON.
RESOURCE_MIME_TYPE = "application/json"

# Schéma URI propriétaire de la surface ThinkTuning (strictement préfixe).
RESOURCE_SCHEME = "thinktuning://"

# Plafonds post-décodage (anti-abus : une URI de plusieurs Ko ne doit jamais
# atteindre un tool ni saturer un message d'erreur).
MAX_JOB_ID_CHARS = 200
MAX_DATASET_PATH_CHARS = 500
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
    kind: str  # cible interne : jobs | job | models | dataset_stats | config
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
    "dataset_stats": (
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
        "thinktuning://jobs, thinktuning://jobs/{job_id}, thinktuning://models, "
        "thinktuning://datasets/{path}/stats, thinktuning://config."
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
    et la pile agentique — ``thinktuning://config`` est la seule resource qui
    en dépend, les autres lectures ne doivent pas payer ce coût.
    """

    def _resolve() -> Callable[..., dict]:
        from core.agent_cache import agent_config  # import paresseux (lourd)

        return agent_config

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
    """``MCPResourceRegistryPort`` — les 5 resources « thinktuning:// » (tâche 8).

    La liste (``list_resources``) est de la MÉTADONNÉE PURE : aucune I/O, aucun
    import de tool. La lecture (``read_resource``) résout l'URI par routes
    strictes puis DÉLÈGUE au tool interne correspondant — les garde-fous
    (``safe_resolve``, SQLite ``mode=ro`` + ``query_only``) sont portés par
    délégation, pas dupliqués.

    Args:
        job_list / job_get / model_versions / dataset_stats / agent_config:
            implémentations injectables (tests, déploiements spécifiques) ;
            ``None`` → résolution PARESSEUSE du tool legacy au premier appel
            (``ia.tools.tool_registry.TOOLS`` / ``core.agent_cache``).
    """

    def __init__(
        self,
        *,
        job_list: Callable[..., dict] | None = None,
        job_get: Callable[..., dict] | None = None,
        model_versions: Callable[..., dict] | None = None,
        dataset_stats: Callable[..., dict] | None = None,
        agent_config: Callable[..., dict] | None = None,
    ) -> None:
        self._resolvers: dict[str, Callable[[], Callable[..., dict]]] = {
            "jobs": (lambda: job_list) if job_list else _lazy_legacy_tool("job_list"),
            "job": (lambda: job_get) if job_get else _lazy_legacy_tool("job_get"),
            "models": (
                (lambda: model_versions)
                if model_versions
                else _lazy_legacy_tool("model_versions")
            ),
            "dataset_stats": (
                (lambda: dataset_stats)
                if dataset_stats
                else _lazy_legacy_tool("dataset_stats")
            ),
            "config": (lambda: agent_config) if agent_config else _lazy_agent_config(),
        }

    # --- Métadonnées (resources/list) — pur, sans I/O -----------------------

    def list_resources(self) -> list[MCPResource]:
        """Les 5 resources « thinktuning:// » (3 statiques + 2 paramétrées)."""
        return [route.resource for route in _ROUTES]

    def list_resource_templates(self) -> list[MCPResourceTemplate]:
        """Gabarits des resources paramétrées (préparation templates/list, S5).

        Hors contrat minimal du port (tâche 8) : prépare
        ``resources/templates/list`` (tâche 13) sans inventer d'entité —
        ``MCPResourceTemplate`` du domaine.
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
        métier client-réparable (job introuvable, dataset absent, chemin hors
        sandbox, format non supporté) — le message actionable du tool est
        préservé, aucun oracle d'inventaire n'est offert.
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
        func = self._resolvers[kind]()
        if kind == "config":
            # Secrets JAMAIS en clair sur la surface MCP (clés API masquées).
            return _sanitize_agent_config(func())
        return func(**params)


def build_legacy_resource_provider() -> LegacyResourceProvider:
    """Provider par défaut des 5 resources « thinktuning:// » (tâche 8).

    Construction SANS I/O ni import lourd : les tools internes sont résolus
    paresseusement au premier ``read_resource``.
    """
    return LegacyResourceProvider()
