# project/tests/test_legacy_approval_adapter.py
"""Tests de l'adaptateur file d'approbation legacy -> ApprovalStorePort."""

from app.domain.ports import ApprovalStorePort
from app.infrastructure.legacy_approval_store import (
    LegacyApprovalStoreAdapter,
    build_approval_store,
)


def test_adapter_satisfies_port() -> None:
    assert isinstance(build_approval_store(), ApprovalStorePort)


def test_adapter_delegates_and_wraps_create(monkeypatch) -> None:
    """``create`` doit renvoyer la ligne complète (dict), pas un simple id."""
    calls: list[tuple] = []

    class FakeLegacy:
        def create(self, *a, **k):
            calls.append((a, k))
            return "req-42"

        def get(self, request_id):
            return {"request_id": request_id, "tool": "echo", "status": "pending"}

        def approve(self, request_id, decided_by=None):
            return {"request_id": request_id, "status": "approved"}

        def reject(self, request_id, decided_by=None):
            return {"request_id": request_id, "status": "rejected"}

        def list(self, status=None):
            return []

    import app.infrastructure.legacy_approval_store as adapter_mod

    monkeypatch.setattr(adapter_mod, "_get_store", lambda: FakeLegacy())
    adapter = LegacyApprovalStoreAdapter()

    record = adapter.create("echo", {"x": 1}, "write", "approve", "raison", args_hash="h")
    assert record["request_id"] == "req-42"
    assert calls[0][1].get("args_hash") == "h"

    assert adapter.get("req-42")["tool"] == "echo"
    assert adapter.approve("req-1")["status"] == "approved"
    assert adapter.reject("req-2")["status"] == "rejected"
    assert adapter.list("pending") == []
