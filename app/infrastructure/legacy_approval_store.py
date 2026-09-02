"""Adaptateur : file d'approbation legacy (core/approval_store.py) -> port.

Encapsule le store SQLite historique derrière ``ApprovalStorePort`` sans
aucune logique nouvelle. Conventions legacy conservées :
    - ``create`` renvoie un ``request_id`` (hex) — l'adaptateur renvoie la
      ligne complète (dict) pour coller au contrat du port ;
    - ``status`` : pending / approved / rejected ;
    - la même table trace les actions bloquées (reject) et en attente.
"""

from __future__ import annotations

from typing import Any

from app.domain.ports import ApprovalStorePort

try:
    from core.approval_store import get_approval_store as _get_store
except ImportError as _exc:  # sécurité : le module legacy est requis (fail-fast)
    raise ImportError(
        "core.approval_store introuvable : adaptateur d'approbation inutilisable."
    ) from _exc


class LegacyApprovalStoreAdapter:
    """Implémentation de ``ApprovalStorePort`` au-dessus du store legacy."""

    def create(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        """Enregistre une demande et renvoie la ligne créée (dict)."""
        request_id = _get_store().create(*args, **kwargs)
        record = _get_store().get(request_id) or {"request_id": request_id}
        return record

    def get(self, request_id: str) -> dict[str, Any] | None:
        return _get_store().get(request_id)

    def approve(self, request_id: str, decided_by: str | None = None) -> dict[str, Any] | None:
        return _get_store().approve(request_id, decided_by)

    def reject(self, request_id: str, decided_by: str | None = None) -> dict[str, Any] | None:
        return _get_store().reject(request_id, decided_by)

    def list(self, status: str | None = None) -> list[dict[str, Any]]:
        return _get_store().list(status)


def build_approval_store() -> ApprovalStorePort:
    """Instance par défaut (singleton legacy sous-jacent)."""
    return LegacyApprovalStoreAdapter()
