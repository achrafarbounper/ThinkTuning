# project/tests/test_openapi_export.py
"""Verrou de fraîcheur de ``openapi.json`` — source de la génération client TS.

Le spec commité à la racine est la source unique du client TypeScript
(`npm run generate:api-types` dans ``frontend/``). Il doit donc refléter
EXACTEMENT l'application montée :

1. ``test_openapi_json_est_frais`` — régénère le spec en mémoire et compare
   au fichier commité ; toute dérive de contrat non commitée casse la CI
   AVANT que quelqu'un génère un client périmé.
2. ``test_openapi_json_ne_contient_que_la_v1`` — post-strangler, l'export ne
   contient AUCUN path hors ``/api/v1`` et le volume reste plausible
   (garde-fou contre un export tronqué).
3. ``test_export_openapi_script_produit_le_meme_fichier`` — le script CLI
   ``export_openapi.py`` (exécuté in-process) écrit exactement le fichier
   commité (même sérialisation, même ordre de clés).
"""

import json
import os
from pathlib import Path

os.environ.setdefault("API_KEY", "test-key")

import api as _api  # noqa: E402, F401  (charge l'application, isole l'env)
from api import app  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
EXPORTED = ROOT / "openapi.json"


def _dump(spec: dict) -> str:
    """Sérialisation canonique — doit rester alignée sur export_openapi.py."""
    return json.dumps(spec, indent=2, ensure_ascii=False) + "\n"


def test_openapi_json_est_frais():
    """Le spec commité est identique au spec de l'app montée (zéro dérive)."""
    assert EXPORTED.exists(), (
        "openapi.json absent — exécutez `python export_openapi.py` et "
        "commitez le spec avec le changement de contrat"
    )
    committed = EXPORTED.read_text(encoding="utf-8")
    fresh = _dump(app.openapi())
    assert committed == fresh, (
        "openapi.json périmé : le spec monté a dérivé du fichier commité. "
        "Relancez `python export_openapi.py` et committez openapi.json "
        "AVEC le changement de contrat (sinon le client TS généré sera faux)."
    )


def test_openapi_json_ne_contient_que_la_v1():
    """Post-strangler : zéro path hors /api/v1, volume plausible (≥ 50)."""
    spec = json.loads(EXPORTED.read_text(encoding="utf-8"))
    paths = list(spec["paths"])
    outside = sorted(p for p in paths if not p.startswith("/api/v1"))
    assert not outside, (
        "openapi.json contient des paths hors /api/v1 (surface legacy "
        f"réapparue ?) : {outside[:10]}"
    )
    assert len(paths) >= 50, (
        f"openapi.json tronqué ? {len(paths)} paths seulement (attendu ≥ 50)"
    )


def test_export_openapi_script_produit_le_meme_fichier(tmp_path):
    """Le script CLI écrit exactement le fichier commité (même sérialisation)."""
    import export_openapi  # module racine (sys.path assuré par conftest)

    out = tmp_path / "openapi.json"
    rc = export_openapi.main(["--out", str(out)])
    assert rc == 0
    assert out.read_text(encoding="utf-8") == EXPORTED.read_text(encoding="utf-8"), (
        "export_openapi.py produit une sérialisation différente du fichier "
        "commité — alignez _dump()/le script puis régénérez openapi.json"
    )
