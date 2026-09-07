"""Tests CORS de surface API.

Contexte production : une origine NON autorisée reçoit
  - sur une simple requête GET : une réponse 200 SANS en-tête
    ``Access-Control-Allow-Origin`` (le navigateur bloque alors côté client,
    ex. « No 'Access-Control-Allow-Origin' header is present » observé depuis
    le frontend Vercel) ;
  - sur un preflight OPTIONS : un 400 « Disallowed CORS origin » émis par le
    CORSMiddleware de Starlette.

On ne teste PAS l'instance ``app`` d'``api.main`` : sa configuration CORS est
figée à l'import (variables d'environnement du process), dépendante de l'ordre
de collecte pytest. On teste donc :
  1. les helpers de lecture d'environnement (fonctions pures, re-lues à chaque
     appel) ;
  2. le comportement du middleware câblé EXACTEMENT comme en production
     (mêmes paramètres, CORS le plus externe) sur une app synthétique.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from api.main import _cors_allow_origin_regex, _cors_allowed_origins

# Origines de référence (production + previews Vercel).
# Regex préfixée par le nom de projet Vercel « think-tuning-ai » : elle couvre
# les previews (think-tuning-ai-<hash>.vercel.app, think-tuning-ai-<hash>-<slug
# d'équipe>.vercel.app, think-tuning-ai-git-<branche>-… ) et exclut les autres
# projets Vercel. La production nue reste dans CORS_ALLOWED_ORIGINS (la regex
# exige un suffixe après « think-tuning-ai- »).
VERCEL_ORIGIN = "https://think-tuning-ai.vercel.app"
VERCEL_PREVIEW = "https://think-tuning-ai-abc123.vercel.app"
VERCEL_PREVIEW_TEAM = "https://think-tuning-ai-abc123-snowy-two-23.vercel.app"
VERCEL_REGEX = r"^https://think-tuning-ai-[a-z0-9-]+\.vercel\.app$"


def _make_client(origins: list[str], regex: str | None = None) -> TestClient:
    """App synthétique câblée comme api/main.py : CORS = seul middleware."""
    app = FastAPI()

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "ok"}

    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_origin_regex=regex,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    return TestClient(app)


# --- Helpers d'environnement ---------------------------------------------------


def test_cors_allowed_origins_defauts_locaux_si_var_absente(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans CORS_ALLOWED_ORIGINS : uniquement les defaults localhost (bug prod)."""
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)
    origins = _cors_allowed_origins()
    assert origins == [
        "http://localhost",
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]
    assert VERCEL_ORIGIN not in origins


def test_cors_allowed_origins_parse_csv_avec_origine_vercel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CSV : espaces tolérés, origine Vercel préservée telle quelle."""
    monkeypatch.setenv(
        "CORS_ALLOWED_ORIGINS",
        f" http://localhost:5173 , {VERCEL_ORIGIN} ",
    )
    assert _cors_allowed_origins() == ["http://localhost:5173", VERCEL_ORIGIN]


def test_cors_allow_origin_regex_desactivee_par_defaut(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans la variable (ou vide/espaces) : aucune regex — comportement inchangé."""
    monkeypatch.delenv("CORS_ALLOW_ORIGIN_REGEX", raising=False)
    assert _cors_allow_origin_regex() is None
    monkeypatch.setenv("CORS_ALLOW_ORIGIN_REGEX", "   ")
    assert _cors_allow_origin_regex() is None


def test_cors_allow_origin_regex_lue_si_definie(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valeur lue et débarrassée des espaces parasites."""
    monkeypatch.setenv("CORS_ALLOW_ORIGIN_REGEX", f"  {VERCEL_REGEX}  ")
    assert _cors_allow_origin_regex() == VERCEL_REGEX


# --- Comportement HTTP (câblage identique à la production) ---------------------


def test_origine_autorisee_recoit_access_control_allow_origin() -> None:
    """Origine explicite autorisée : ACAO écho de l'Origin (cas sain)."""
    client = _make_client([VERCEL_ORIGIN])
    response = client.get("/ping", headers={"Origin": VERCEL_ORIGIN})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == VERCEL_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"


def test_origine_non_autorisee_reponse_sans_acao() -> None:
    """REPRODUCTION DU BUG PROD : origine absente -> 200 mais AUCUN header ACAO.

    Signature exacte de l'erreur navigateur
    « No 'Access-Control-Allow-Origin' header is present » : la requête réussit
    côté serveur (200) mais le navigateur bloque la réponse.
    """
    client = _make_client(["http://localhost:5173"])  # défauts localhost uniquement
    response = client.get("/ping", headers={"Origin": VERCEL_ORIGIN})
    assert response.status_code == 200
    assert "access-control-allow-origin" not in response.headers


def test_preflight_origine_non_autorisee_rejete_400() -> None:
    """Preflight d'une origine absente : 400 « Disallowed CORS origin »."""
    client = _make_client(["http://localhost:5173"])
    response = client.options(
        "/ping",
        headers={
            "Origin": VERCEL_ORIGIN,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert response.status_code == 400
    assert "Disallowed CORS origin" in response.text


def test_regex_active_les_previews_vercel() -> None:
    """Avec CORS_ALLOW_ORIGIN_REGEX : les sous-domaines variables passent.

    Config réelle (render.yaml) : liste explicite (prod) + regex (previews).
    La regex préfixée par le nom de projet ne couvre PAS l'origine de
    production nue (« think-tuning-ai » sans suffixe) : celle-ci doit rester
    dans CORS_ALLOWED_ORIGINS — d'où la liste passée au client ci-dessous.
    """
    client = _make_client([VERCEL_ORIGIN], regex=VERCEL_REGEX)
    response = client.get("/ping", headers={"Origin": VERCEL_PREVIEW})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == VERCEL_PREVIEW

    # Les previews avec slug d'équipe passent aussi (autre format d'URL).
    response = client.get("/ping", headers={"Origin": VERCEL_PREVIEW_TEAM})
    assert response.headers["access-control-allow-origin"] == VERCEL_PREVIEW_TEAM

    # La production reste autorisée via la liste explicite (pas la regex).
    response = client.get("/ping", headers={"Origin": VERCEL_ORIGIN})
    assert response.headers["access-control-allow-origin"] == VERCEL_ORIGIN


def test_regex_ne_couvre_pas_la_prod_nue() -> None:
    """La regex seule (liste vide) ne couvre pas l'origine de prod nue (attendu)."""
    client = _make_client([], regex=VERCEL_REGEX)
    response = client.get("/ping", headers={"Origin": VERCEL_ORIGIN})
    assert "access-control-allow-origin" not in response.headers


def test_regex_absente_les_previews_sont_bloquees() -> None:
    """Sans regex : une preview Vercel (sous-domaine variable) est bloquée."""
    client = _make_client([VERCEL_ORIGIN], regex=None)
    response = client.get("/ping", headers={"Origin": VERCEL_PREVIEW})
    assert "access-control-allow-origin" not in response.headers


def test_origine_etrangere_reste_bloquee_meme_avec_regex() -> None:
    """La regex ne cale pas la porte : un autre projet Vercel tiers est rejeté."""
    client = _make_client([], regex=VERCEL_REGEX)
    response = client.get(
        "/ping", headers={"Origin": "https://autre-projet-tiers.vercel.app"}
    )
    assert "access-control-allow-origin" not in response.headers

