"""Tests du vérificateur de pins Docker (P2 lot 15 — supply-chain).

``backend/scripts/`` n'est pas un package : le module est chargé par chemin
via importlib. Seule la logique pure est testée ici (parsing des entrées,
normalisation Hub, détection d'écarts) — ``resolve_digest`` est mocké, aucun
appel réseau n'est effectué.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "check_docker_pins.py"
_spec = importlib.util.spec_from_file_location("check_docker_pins", _SCRIPT_PATH)
assert _spec is not None and _spec.loader is not None
check_docker_pins = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_docker_pins)


# --- parse_pin : deux styles de clés, fail-closed ---------------------------


def test_parse_pin_tag_embarque() -> None:
    repo, tag = check_docker_pins.parse_pin("python:3.13-slim", {"digest": "sha256:x"})
    assert (repo, tag) == ("python", "3.13-slim")


def test_parse_pin_champ_tag_explicite() -> None:
    entry = {"tag": "2026.9.5-a303e9c0c", "digest": "sha256:x"}
    repo, tag = check_docker_pins.parse_pin("searxng/searxng", entry)
    assert (repo, tag) == ("searxng/searxng", "2026.9.5-a303e9c0c")


def test_parse_pin_champ_et_embarque_concordants() -> None:
    repo, tag = check_docker_pins.parse_pin("python:3.13-slim", {"tag": "3.13-slim"})
    assert (repo, tag) == ("python", "3.13-slim")


def test_parse_pin_conflit_rejete() -> None:
    with pytest.raises(RuntimeError, match="tag embarqué"):
        check_docker_pins.parse_pin("python:3.13-slim", {"tag": "other"})


def test_parse_pin_sans_tag_rejete_pas_de_fallback_latest() -> None:
    with pytest.raises(RuntimeError, match="aucun tag précisé"):
        check_docker_pins.parse_pin("searxng/searxng", {"digest": "sha256:x"})


# --- _hub_repo : namespace des images officielles ---------------------------


def test_hub_repo_officiel_normalise_vers_library() -> None:
    assert check_docker_pins._hub_repo("python") == "library/python"
    assert check_docker_pins._hub_repo("searxng/searxng") == "searxng/searxng"


# --- resolve_digest : fail-closed sans réseau -------------------------------


def test_resolve_digest_ref_invalide_sans_tag() -> None:
    with pytest.raises(RuntimeError, match="référence image invalide"):
        check_docker_pins.resolve_digest("python")


# --- check_pins : écarts digest vs conformité (Hub mocké) -------------------


def test_check_pins_detecte_ecart_digest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_docker_pins,
        "resolve_digest",
        lambda _ref: {"digest": "sha256:remote", "tag": "t1"},
    )
    errors = check_docker_pins.check_pins(
        {"python": {"tag": "t1", "digest": "sha256:local"}}
    )
    assert errors and "sha256:local" in errors[0] and "sha256:remote" in errors[0]


def test_check_pins_conforme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        check_docker_pins,
        "resolve_digest",
        lambda _ref: {"digest": "sha256:ok", "tag": "t1"},
    )
    assert (
        check_docker_pins.check_pins({"python": {"tag": "t1", "digest": "sha256:ok"}})
        == []
    )


def test_check_pins_entree_invalide_listee_sans_lever(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Une entrée cassée est listée en écart sans interrompre les autres.
    monkeypatch.setattr(
        check_docker_pins,
        "resolve_digest",
        lambda _ref: {"digest": "sha256:ok", "tag": "t1"},
    )
    errors = check_docker_pins.check_pins(
        {
            "python": {"tag": "t1", "digest": "sha256:ok"},
            "node": {},  # pas de tag → entrée invalide
        }
    )
    assert len(errors) == 1 and "node" in errors[0]
