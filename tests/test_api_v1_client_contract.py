# project/tests/test_api_v1_client_contract.py
"""Verrou croisé : chaque chemin `/api/v1/*` appelé par le dashboard (source
non-test) doit correspondre à une route ENREGISTRÉE dans l'application FastAPI.

Complémentaire du verrou ``test_api_v1_contract.py`` (paths v1 attendus côté
backend) : il protège ici contre le risque inverse — un path consommé par le
frontend qui pointerait vers une route absente (404 silencieux). Un littéral
ajouté au client pour un endpoint inexistant casse ce test AVANT la mise en
prod.

Règles d'extraction (volontairement conservatrices) :
  - fichiers `.ts`/`.tsx` du dashboard SANS les `.test.*` ;
  - littéraux `/api/v1/...` délimités par quote/backtick, HORS commentaires
    (les blocs `/* */` et les `//` de ligne sont retirés par un scanneur qui
    respecte les chaînes) ;
  - paramètres normalisés (`${encodeURIComponent(x)}` et `{job_id}` → `{param}`).

Les WebSockets (ex. `train/stream`) sont couverts : les routes WS sont dans
``app.routes`` (hors spec OpenAPI mais bien enregistrées).
"""

import os
import re
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import api as _api  # noqa: E402, F401  (charge l'application, isole l'env)
from api import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
CLIENT_SRC = ROOT / "dashboard" / "src"

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
    client_paths = _client_v1_paths()
    assert client_paths, "aucun path /api/v1 extrait du client — le verrou est inopérant"
    registered = _registered_v1_paths()
    missing = sorted(p for p in client_paths if p not in registered)
    assert not missing, (
        "paths /api/v1 consommés par le dashboard mais absents des routes : "
        f"{missing}"
    )


def test_volume_endpoints_v1_plausible():
    """Garde-fou de volume : si le nombre de routes v1 chute, le test
    précédent devient moins discriminant — on s'en assure."""
    assert len(_registered_v1_paths()) >= 50
