# project/app/infrastructure/security/vault.py

"""Vault de secrets (P2 durable, lot 13) — abstraction + adaptateurs.

Les secrets (clés API LLM, JWT_SECRET, STORE_ENCRYPTION_KEY, DSN) ne doivent
plus vivre uniquement dans l'environnement de process : backend ***vault***
(Doppler / Infisical / Azure Key Vault) et audit d'accès.

Port ``SecretSource`` (Protocol — pattern Ports & Adapters) :
    - ``EnvSecretSource``    : lecture ``os.getenv`` (défaut, zéro migration) ;
    - ``DopplerSecretSource``: API Doppler (token de service via l'env
      ``DOPPLER_TOKEN``, endpoint ``/v3/configs/config/secrets/download``) ;
    - ``InfisicalSecretSource`` : API Infisical (``INFISICAL_TOKEN`` +
      ``INFISICAL_URL``/``INFISICAL_PROJECT_ID``, endpoint
      ``/api/v3/secrets/raw/{name}``) ;
    - ``AzureKeyVaultSecretSource`` : Azure Key Vault (``AZURE_KEY_VAULT_URI``
      + ``AZURE_TENANT_ID``/``AZURE_CLIENT_ID``/``AZURE_CLIENT_SECRET``,
      flux OAuth2 client-credentials puis secret versionné) ;
    - ``ChainSecretSource``   : bascule entre plusieurs sources (fail-over).

Règles :
    1. ``get_secret`` AUDITE chaque accès (action ``secret_access``, nom du
       secret, source, succès/échec) — jamais la VALEUR (journal d'audit) ;
    2. active par ``VAULT_BACKEND=env|doppler|infisical|azure`` (défaut
       ``env`` : comportement inchangé, aucun réseau) ;
    3. fail-closed sur les backends HTTP : si la source est configurée mais
       injoignable/erreur, ``get_secret`` lève (pas de repli silencieux vers
       l'env — un secret MANQUANT ne doit pas être remplacé par un autre) ;
    4. les tokens d'accès aux APIs vault elles-mêmes restent dans l'env du
       process (bootstrap minimal, documenté dans docs/OPS_RUNBOOK.md).
"""

from __future__ import annotations

import logging
import os
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)

# Action d'audit normalisée (table agent_audit existante).
ACT_SECRET_ACCESS = "secret_access"

# Secrets dont l'accès est audité de façon prioritaire (lecture via l'API
# ``get_secret`` de ce module — les lectures directes os.getenv historiques
# ne changent pas de comportement).
SENSITIVE_SECRET_NAMES = frozenset(
    {
        "JWT_SECRET",
        "STORE_ENCRYPTION_KEY",
        "MODEL_SIGNING_KEY",
        "OPENROUTER_API_KEY",
        "HF_API_KEY",
        "HF_TOKEN",
        "DOPPLER_TOKEN",
        "INFISICAL_TOKEN",
        "AZURE_CLIENT_SECRET",
        "API_KEY",
        "API_KEY_READ",
        "SEARXNG_SECRET",
    }
)


@runtime_checkable
class SecretSource(Protocol):
    """Contrat d'une source de secrets (implémentée dans ce module)."""

    name: str

    def get_secret(self, name: str) -> str | None: ...


def _audit_secret_access(name: str, source: str, ok: bool) -> None:
    """Audit d'un accès secret (la valeur n'est JAMAIS journalisée)."""
    try:
        from core.audit_store import get_audit_store

        get_audit_store().log(
            action=ACT_SECRET_ACCESS,
            subject=source,
            detail={"secret": name, "ok": ok},
            actor=f"vault:{source}",
        )
    except Exception as exc:  # pragma: no cover - défensif
        logger.warning("Audit accès secret indisponible : %s", exc)


class EnvSecretSource:
    """Source par défaut : variable d'environnement (aucun réseau)."""

    name = "env"

    def get_secret(self, name: str) -> str | None:
        value = os.getenv(name) or None
        _audit_secret_access(name, self.name, ok=value is not None)
        return value


class _HttpVaultSource:
    """Base commune des backends HTTP (httpx, fail-closed)."""

    name = "http"

    def __init__(self) -> None:
        import httpx  # dépendance déjà présente (requirements.txt)

        self._httpx = httpx

    def _get(self, url: str, headers: dict[str, str], params: dict | None = None) -> dict:
        try:
            response = self._httpx.get(
                url, headers=headers, params=params, timeout=10.0, follow_redirects=True
            )
            response.raise_for_status()
            return response.json()
        except Exception as exc:
            raise RuntimeError(
                f"{self.name}: accès vault refusé ({url}) — fail-closed : {exc}"
            ) from exc


class DopplerSecretSource(_HttpVaultSource):
    """Doppler — télécharge la config courante (token de service en env).

    Prérequis : ``DOPPLER_TOKEN`` (service token) dans l'environnement du
    process ; ``DOPPLER_API_URL`` optionnel (défaut https://api.doppler.com).
    """

    name = "doppler"

    def get_secret(self, name: str) -> str | None:
        token = os.getenv("DOPPLER_TOKEN")
        if not token:
            _audit_secret_access(name, self.name, ok=False)
            raise RuntimeError("DOPPLER_TOKEN absent : source doppler non configurée")
        base = os.getenv("DOPPLER_API_URL", "https://api.doppler.com").rstrip("/")
        data = self._get(
            f"{base}/v3/configs/config/secrets/download",
            headers={"Authorization": f"Bearer {token}"},
        )
        value = None
        if isinstance(data, dict):
            value = data.get(name)
            if value is None:  # réponse wrapper « secrets »
                nested = data.get("secrets")
                if isinstance(nested, dict):
                    value = nested.get(name)
                    if isinstance(value, dict):
                        value = value.get("raw") or value.get("value")
        _audit_secret_access(name, self.name, ok=value is not None)
        return str(value) if value is not None else None


class InfisicalSecretSource(_HttpVaultSource):
    """Infisical — lecture d'un secret brut (token machine en env).

    Prérequis : ``INFISICAL_TOKEN``, ``INFISICAL_PROJECT_ID``,
    ``INFISICAL_ENVIRONMENT`` (défaut ``prod``) ; ``INFISICAL_URL`` optionnel
    (défaut https://app.infisical.com).
    """

    name = "infisical"

    def get_secret(self, name: str) -> str | None:
        token = os.getenv("INFISICAL_TOKEN")
        project_id = os.getenv("INFISICAL_PROJECT_ID")
        if not token or not project_id:
            _audit_secret_access(name, self.name, ok=False)
            raise RuntimeError(
                "INFISICAL_TOKEN / INFISICAL_PROJECT_ID absents : source infisical non configurée"
            )
        base = os.getenv("INFISICAL_URL", "https://app.infisical.com").rstrip("/")
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        params = {
            "environment": os.getenv("INFISICAL_ENVIRONMENT", "prod"),
            "secretName": name,
            "secretPath": os.getenv("INFISICAL_SECRET_PATH", "/"),
        }
        data = self._get(f"{base}/api/v3/secrets/raw/{name}", headers=headers, params=params)
        value = (data or {}).get("secret") if isinstance(data, dict) else None
        if isinstance(value, dict):
            value = value.get("secretValue") or value.get("value")
        _audit_secret_access(name, self.name, ok=value is not None)
        return str(value) if value is not None else None


class AzureKeyVaultSecretSource(_HttpVaultSource):
    """Azure Key Vault — secret versionné via OAuth2 client-credentials.

    Prérequis (env) : ``AZURE_KEY_VAULT_URI`` (ex. https://kv.vault.azure.net),
    ``AZURE_TENANT_ID``, ``AZURE_CLIENT_ID``, ``AZURE_CLIENT_SECRET``.
    """

    name = "azure_kv"

    def _access_token(self) -> str:
        client_secret = os.getenv("AZURE_CLIENT_SECRET")
        tenant = os.getenv("AZURE_TENANT_ID")
        client_id = os.getenv("AZURE_CLIENT_ID")
        if not all((tenant, client_id, client_secret)):
            raise RuntimeError("AZURE_TENANT_ID/CLIENT_ID/CLIENT_SECRET absents : azure_kv")
        try:
            response = self._httpx.post(
                f"https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token",
                data={
                    "grant_type": "client_credentials",
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "scope": "https://vault.azure.net/.default",
                },
                timeout=10.0,
            )
            response.raise_for_status()
            return str(response.json()["access_token"])
        except Exception as exc:
            raise RuntimeError(f"azure_kv : échec OAuth2 ({exc}) — fail-closed") from exc

    def get_secret(self, name: str) -> str | None:
        vault_uri = (os.getenv("AZURE_KEY_VAULT_URI") or "").rstrip("/")
        if not vault_uri:
            _audit_secret_access(name, self.name, ok=False)
            raise RuntimeError("AZURE_KEY_VAULT_URI absent : source azure_kv non configurée")
        token = self._access_token()
        version = os.getenv("AZURE_KEY_VAULT_VERSION") or ""
        url = f"{vault_uri}/secrets/{name}" + (f"/{version}" if version else "")
        try:
            data = self._get(
                url,
                headers={"Authorization": f"Bearer {token}"},
                params={"api-version": "7.4"},
            )
        except RuntimeError:
            # 404 = secret absent (pas une panne vault).
            _audit_secret_access(name, self.name, ok=False)
            return None
        value = (data or {}).get("value") if isinstance(data, dict) else None
        _audit_secret_access(name, self.name, ok=value is not None)
        return str(value) if value is not None else None


class ChainSecretSource:
    """Première source qui répond (fail-over ordonné).

    ``sources`` : liste de sources testées dans l'ordre. Si une source lève
    (panne), on tente la suivante ; si TOUTES échouent, on lève (fail-closed).
    """

    name = "chain"

    def __init__(self, sources: list[SecretSource]):
        self._sources = list(sources)

    def get_secret(self, name: str) -> str | None:
        last_error: Exception | None = None
        for source in self._sources:
            try:
                value = source.get_secret(name)
            except Exception as exc:  # noqa: BLE001 - fail-over volontaire
                logger.warning("vault %s indisponible pour %s : %s", source.name, name, exc)
                last_error = exc
                continue
            if value is not None:
                return value
        if last_error is not None:
            raise RuntimeError(
                f"aucune source vault n'a fourni {name} (dernière erreur : {last_error})"
            )
        _audit_secret_access(name, self.name, ok=False)
        return None


# ---------------------------------------------------------------------------
# Factory + helper
# ---------------------------------------------------------------------------

# Annotation explicite : sans elle, mypy joint les classes concrètes vers
# ``object`` (aucune base nominale commune — conformité STRUCTURELLE au
# Protocol) et refuse le retour ``SecretSource``.
_VAULT_BACKENDS: dict[str, type[SecretSource]] = {
    "env": EnvSecretSource,
    "doppler": DopplerSecretSource,
    "infisical": InfisicalSecretSource,
    "azure": AzureKeyVaultSecretSource,
    "azure_kv": AzureKeyVaultSecretSource,
}


def build_secret_source(backend: str | None = None) -> SecretSource:
    """Construit la source selon ``VAULT_BACKEND`` (défaut ``env``).

    ``backend`` accepte aussi un CSV de backends (ex. ``doppler,env`` :
    fail-over vers env — pratique en préprod).
    """
    raw = (backend or os.getenv("VAULT_BACKEND", "env")).strip().lower()
    backends = [b.strip() for b in raw.split(",") if b.strip()]
    if not backends:
        return EnvSecretSource()
    if len(backends) == 1:
        cls = _VAULT_BACKENDS.get(backends[0])
        if cls is None:
            raise ValueError(
                f"VAULT_BACKEND inconnu : {backends[0]!r} "
                f"(valides : {', '.join(sorted(_VAULT_BACKENDS))})"
            )
        return cls()
    return ChainSecretSource([_VAULT_BACKENDS[b]() for b in backends if b in _VAULT_BACKENDS])


def get_vault_secret(name: str) -> str | None:
    """Lit un secret via la source active (audit d'accès inclus)."""
    return build_secret_source().get_secret(name)


__all__ = [
    "ACT_SECRET_ACCESS",
    "AzureKeyVaultSecretSource",
    "ChainSecretSource",
    "DopplerSecretSource",
    "EnvSecretSource",
    "InfisicalSecretSource",
    "SecretSource",
    "SENSITIVE_SECRET_NAMES",
    "build_secret_source",
    "get_vault_secret",
]
