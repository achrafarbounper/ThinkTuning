# project/tests/test_api_v1_client_contract.py
"""Verrous croisés client ↔ backend pour la surface `/api/v1`.

1. ``test_tous_les_paths_client_sont_enregistres`` — chaque chemin `/api/v1/*`
   appelé par le dashboard (source non-test) doit correspondre à une route
   ENREGISTRÉE dans l'application FastAPI (protège contre le 404 silencieux).
2. ``test_aucune_route_hors_v1_montee`` (post-strangler) — l'application ne
   monte PLUS aucune route hors `/api/v1` : l'épuration a retiré les 16
   ``include_router`` legacy, et toute réintroduction casse ce test AVANT la
   mise en prod.
3. ``test_aucun_appel_reseau_hors_v1`` (verrou inversé) — tout appel réseau du
   dashboard (request/fetch/WebSocket/EventSource) cible `/api/v1/*` : un
   endpoint non versionné ajouté par erreur côté client est interdit.

Règles d'extraction (volontairement conservatrices) :
  - fichiers `.ts`/`.tsx` du dashboard SANS les `.test.*` ;
  - littéraux `/api/...` délimités par quote/backtick, HORS commentaires
    (les blocs `/* */` et les `//` de ligne sont retirés par un scanneur qui
    respecte les chaînes) ;
  - paramètres normalisés (`${encodeURIComponent(x)}` et `{job_id}` → `{param}`).

Les WebSockets (ex. `train/stream`) sont couverts : les routes WS sont dans
``app.openapi()`` complété par l'allowlist explicite de l'unique WS (hors spec
OpenAPI mais bien enregistré).
"""

import os
import re
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import api as _api  # noqa: E402, F401  (charge l'application, isole l'env)
from api import app  # noqa: E402

# Racine Python (backend/). Le frontend (ex-dashboard/) vit un niveau
# au-dessus, à la racine du dépôt : ../frontend/src.
ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT.parent / "frontend" / "src"

_PATH_RE = r'/api/v1/[^\s\'\"\`<>]*'


def _strip_comments(text: str) -> str:
    """Retire les blocs `/* */` et les `//` de commentaire en ignorant les
    chaînes (quotes singles/doubles/backticks) — évite de corrompre les
    littéraux qui contiendraient `//` (statique, sans regex globale)."""
    out: list[str] = []
    i, n = 0, len(text)
    in_str = None
    while i < n:
        ch = text[i]
        if in_str is not None:
            out.append(ch)
            if ch == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 1
            elif ch == in_str:
                in_str = None
            i += 1
            continue
        if ch in "\"'`":
            in_str = ch
            out.append(ch)
            i += 1
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "*":
            end = text.find("*/", i + 2)
            if end == -1:
                break
            i = end + 2
            continue
        if ch == "/" and i + 1 < n and text[i + 1] == "/":
            end = text.find("\n", i + 2)
            i = n if end == -1 else end
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _normalize_path(path: str) -> str:
    """Normalise les paramètres en `{param}` (forme des routes FastAPI).

    Gère les constructions du client : query string (`?token=`), ternaires
    d'URL WebSocket (`${qs ? `?${qs}` : ""}` → coupe à l'espace) et templates
    imbriqués laissés orphelins (dangling `${`)."""
    path = path.split("?", 1)[0]
    path = path.split(" ", 1)[0]
    path = re.sub(r"\$\{[^}]*\}", "{param}", path)
    path = re.sub(r"\{[^}]*\}", "{param}", path)
    path = re.sub(r"\$\{[^}]*$", "", path)  # `${qs` orphelin d'un template imbriqué
    return path


def _client_v1_paths() -> set[str]:
    paths: set[str] = set()
    for file in CLIENT_SRC.rglob("*"):
        if file.suffix not in (".ts", ".tsx") or ".test." in file.name:
            continue
        text = _strip_comments(file.read_text(encoding="utf-8"))
        for match in re.finditer(_PATH_RE, text, flags=re.VERBOSE):
            paths.add(_normalize_path(match.group(0)))
    return paths


def _registered_v1_paths() -> set[str]:
    """Chemins v1 EFFECTIFS : les routes HTTP viennent de `app.openapi()`
    (FastAPI y résout les préfixes `include_router`), l'unique WebSocket hors
    spec est ajoutée explicitement (à compléter si un nouveau WS est ajouté)."""
    spec = app.openapi()
    paths = set(spec["paths"].keys())
    paths.add("/api/v1/train/stream/{job_id}")  # WebSocket de suivi training
    return {
        _normalize_path(path)
        for path in paths
        if path.startswith("/api/v1")
    }


def test_tous_les_paths_client_sont_enregistres():
    """Chaque `/api/v1/*` du client résout vers une route v1 enregistrée.

    Résolution par regex segments (comme le router) : un path littéral concret
    (ex. `/api/v1/classifiers/intent/reload`) est accepté s'il matche une route
    paramétrée (`/api/v1/classifiers/{name}/reload`) — pas seulement par
    égalité stricte avec les templates du spec.
    """
    client_paths = _client_v1_paths()
    assert client_paths, "aucun path /api/v1 extrait du client — le verrou est inopérant"
    patterns = [_legacy_path_to_regex(p) for p in sorted(_registered_v1_paths())]
    missing = sorted(
        p for p in client_paths
        if not any(pattern.fullmatch(p) for pattern in patterns)
    )
    assert not missing, (
        "paths /api/v1 consommés par le dashboard mais absents des routes : "
        f"{missing}"
    )


def test_volume_endpoints_v1_plausible():
    """Garde-fou de volume : si le nombre de routes v1 chute, le test
    précédent devient moins discriminant — on s'en assure."""
    assert len(_registered_v1_paths()) >= 50


# -- Verrou inversé (post-strangler) : la surface réseau est 100 % v1 ----------

_LEGACY_PARAM_SEGMENT = r"(?:\$\{[^}]*\}|[^/'\"`\s]+)"

# Premier argument CHAÎNE d'un appel réseau — c'est ce que le client consomme.
# Prose UI, clés de parsing Prometheus (ex. ``new Set(['/metrics', ...])``) et
# clés de correspondance métrique (ex. ``findLatency(..., '/predict')``) ne
# sont donc PAS comptées : le verrou vise les appels HTTP, pas le texte.
_NETWORK_CALL_RE = re.compile(r"([A-Za-z_$][\w$]*)\s*\(\s*([`'\"])([^`'\"]*)\2")


def _is_network_callee(name: str) -> bool:
    """Noms connus de transport dans ce codebase (clientCore et services)."""
    return "request" in name or "fetch" in name or name in ("WebSocket", "EventSource")


def _strip_scheme_host(arg: str) -> str:
    match = re.match(r"^(?:https?|wss?):\/\/[^/]+(/.*)?$", arg)
    return match.group(1) or "/" if match else arg


def _network_call_args(text: str) -> list[str]:
    """Chemins normalisés de TOUS les appels réseau à argument littéral.

    Post-épuration, l'ancien filtre « arg legacy » (contenant /api/v1 exclu)
    est levé : le verrou inversé doit voir CHAQUE appel pour pouvoir interdire
    tout chemin hors /api/v1.
    """
    args: list[str] = []
    for match in _NETWORK_CALL_RE.finditer(text):
        callee, arg = match.group(1), match.group(3)
        if not _is_network_callee(callee):
            continue
        arg = _normalize_path(_strip_scheme_host(arg))
        if arg.startswith("/"):
            args.append(arg)
    return args


def _legacy_path_to_regex(path: str) -> re.Pattern:
    """Transforme un path enregistré (`{param}` = segment joker) en regex
    compatible avec le client : un segment paramétré accepte aussi bien un
    template `${...}` qu'une valeur concrète."""
    segments = [
        _LEGACY_PARAM_SEGMENT if seg == "{param}" else re.escape(seg)
        for seg in path.strip("/").split("/")
    ]
    return re.compile("/" + "/".join(segments))


def test_aucune_route_hors_v1_montee():
    """POST-STRANGLER (côté backend) : l'application ne monte PLUS AUCUNE
    route hors `/api/v1`.

    L'épuration a retiré les 16 ``include_router`` legacy de ``api/main.py`` :
    toute réintroduction d'un routeur non versionné casse ce test AVANT la
    mise en prod (les fichiers legacy restants ne sont plus montés).
    """
    outside = sorted(
        path for path in app.openapi()["paths"] if not path.startswith("/api/v1")
    )
    assert not outside, (
        "routes hors /api/v1 encore montées (épuration incomplète) :\n"
        + "\n".join(outside[:20])
    )


def test_aucun_appel_reseau_hors_v1():
    """POST-STRANGLER (côté client) : tout appel réseau du dashboard cible
    `/api/v1/*`.

    Verrou inversé de l'ancien « aucun endpoint legacy consommé » : la surface
    legacy n'existe plus, on interdit donc TOUT chemin réseau non v1 — y
    compris un futur endpoint non versionné ajouté par erreur côté client.
    """
    offenders: list[str] = []
    for file in CLIENT_SRC.rglob("*"):
        if file.suffix not in (".ts", ".tsx") or ".test." in file.name:
            continue
        text = _strip_comments(file.read_text(encoding="utf-8"))
        for arg in _network_call_args(text):
            if not arg.startswith("/api/v1/"):
                offenders.append(f"{file.relative_to(CLIENT_SRC)}: {arg}")
    assert not offenders, (
        "appels réseau hors /api/v1 détectés :\n" + "\n".join(offenders[:20])
    )
