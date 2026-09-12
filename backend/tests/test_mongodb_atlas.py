# project/tests/test_mongodb_atlas.py
"""Garde-fous de connexion MongoDB Atlas pour le déploiement Render (SCRUM-139).

Couvre les diagnostics ajoutés dans ``persistence/mongodb.py`` :

- ``_normalize_atlas_uri`` : toute URI ``mongodb://…*.mongodb.net`` est réécrite
  en ``mongodb+srv://`` (TLS + SNI + SRV obligatoires sur Atlas) ; les URI hors
  Atlas (localhost, mongomock, self-managed) sont laissées intactes ;
- ``MongoClientProvider`` : un échec de handshake/TLS au boot (ex. IP Access
  List Atlas restrictive — signature ``TLSV1_ALERT_INTERNAL_ERROR``) lève
  ``AtlasConnectionError`` avec un message d'action explicite plutôt qu'un
  ``ServerSelectionTimeoutError`` brut ;
- ``_safe_host`` : le log de démarrage n'expose jamais le mot de passe.

Aucun réseau requis : ``pymongo.MongoClient`` est substitué par un faux qui
lève immédiatement (cohérent avec ``MONGODB_MOCK`` côté suite).

Lance : pytest tests/test_mongodb_atlas.py -v
"""

from __future__ import annotations

import pytest
from pymongo.errors import ServerSelectionTimeoutError

from app.infrastructure.persistence import mongodb as m

URI_PLAIN_ATLAS = (
    "mongodb://ac-wcxrowl-shard-00-00.rxffie2.mongodb.net:27017/thinktuning"
)
URI_SRV_ATLAS = (
    "mongodb+srv://ac-wcxrowl-shard-00-00.rxffie2.mongodb.net:27017/thinktuning"
)


# --- Normalisation SRV -------------------------------------------------------


def test_normalize_atlas_uri_rewrites_plain_scheme_to_srv():
    assert m._normalize_atlas_uri(URI_PLAIN_ATLAS) == URI_SRV_ATLAS


def test_normalize_atlas_uri_keeps_userinfo_and_query():
    raw = (
        "mongodb://user:p%40ss@ac-wcxrowl-shard-00-00.rxffie2.mongodb.net/"
        "?retryWrites=true&w=majority"
    )
    normalized = m._normalize_atlas_uri(raw)
    assert normalized.startswith("mongodb+srv://user:p%40ss@")
    assert "?retryWrites=true&w=majority" in normalized
    assert not normalized.startswith("mongodb://user")


def test_normalize_atlas_uri_keeps_non_atlas_uris_untouched():
    assert m._normalize_atlas_uri("mongodb://localhost:27017/thinktuning") == (
        "mongodb://localhost:27017/thinktuning"
    )
    assert m._normalize_atlas_uri(URI_SRV_ATLAS) == URI_SRV_ATLAS


def test_mongo_config_normalizes_atlas_uri(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)
    cfg = m.MongoConfig(uri=URI_PLAIN_ATLAS)
    assert cfg.uri == URI_SRV_ATLAS


# --- Erreur de démarrage actionnable -----------------------------------------


def test_provider_tls_failure_raises_actionable_error(monkeypatch):
    """Handshake TLS coupé par Atlas (IP non listée) → AtlasConnectionError."""

    def boom(*_args, **_kwargs):
        raise ServerSelectionTimeoutError(
            "SSL handshake failed: [SSL: TLSV1_ALERT_INTERNAL_ERROR]"
        )

    monkeypatch.setattr("pymongo.MongoClient", boom)
    monkeypatch.delenv("MONGODB_URI", raising=False)
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)
    with pytest.raises(m.AtlasConnectionError) as excinfo:
        m.MongoClientProvider(config=m.MongoConfig(uri=URI_SRV_ATLAS))
    assert "0.0.0.0/0" in str(excinfo.value)  # aide IP Access List
    assert "mongodb+srv://" in str(excinfo.value)  # format d'URI attendu
    assert isinstance(excinfo.value.__cause__, ServerSelectionTimeoutError)


def test_safe_host_never_leaks_credentials():
    uri = (
        "mongodb+srv://user:s3cret%40pass@ac-x.rxffie2.mongodb.net:27017/thinktuning"
    )
    host = m._safe_host(uri)
    assert "s3cret" not in host and "user@" not in host
    assert "ac-x.rxffie2.mongodb.net" in host
