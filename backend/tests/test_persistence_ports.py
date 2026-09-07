# project/tests/test_persistence_ports.py
"""Contrat de la couche de persistance strangler (roadmap ⚙️#6).

Vérifie que TOUTES les implémentations (SQLite legacy wrappée, fakes mémoire)
satisfont les ports typés du domaine, avec le MÊME comportement :

    1. isinstance runtime_checkable + signatures (introspection) ;
    2. roundtrips comportementaux identiques, paramétrés sur les
       implémentations : cycle de vie de run (start → events → finish →
       list/filtres) et gate d'approbation (create → approve/reject).

Lance : pytest tests/test_persistence_ports.py -v
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest

from app.domain.ports import ApprovalStorePort, RunStorePort
from app.infrastructure.persistence.memory import MemoryApprovalStore, MemoryRunStore
from app.infrastructure.persistence.sqlite import SqliteApprovalStore, SqliteRunStore

COMPLETED = "completed"
ERROR = "error"
AWAITING = "awaiting_approval"
REJECTED_STATUS = "rejected"


# --- Fabriques (SQLite temporaire / mémoire) -----------------------------------


def _sqlite_run_store(tmp_path):
    return SqliteRunStore(str(tmp_path / "runs.db"))


def _memory_run_store(_tmp_path=None):
    return MemoryRunStore()


def _sqlite_approval_store(tmp_path):
    return SqliteApprovalStore(str(tmp_path / "approvals.db"))


def _memory_approval_store(_tmp_path=None):
    return MemoryApprovalStore()


# --- Conformité structurelle ----------------------------------------------------


def test_memory_fakes_satisfy_typed_ports():
    assert isinstance(MemoryRunStore(), RunStorePort)
    assert isinstance(MemoryApprovalStore(), ApprovalStorePort)


def test_sqlite_wrappers_are_legacy_subclasses():
    from core.approval_store import ApprovalStore as LegacyApproval
    from core.run_store import RunStore as LegacyRun

    assert issubclass(SqliteRunStore, LegacyRun)
    assert issubclass(SqliteApprovalStore, LegacyApproval)


def test_sqlite_wrappers_satisfy_typed_ports(tmp_path):
    assert isinstance(_sqlite_run_store(tmp_path), RunStorePort)
    assert isinstance(_sqlite_approval_store(tmp_path), ApprovalStorePort)


def test_port_signatures_are_implemented_by_both_backends(tmp_path):
    """Le port n'exige que des arguments que CHAQUE backend accepte."""
    impls = {
        RunStorePort: [_sqlite_run_store(tmp_path), MemoryRunStore()],
        ApprovalStorePort: [_sqlite_approval_store(tmp_path), MemoryApprovalStore()],
    }
    for port, stores in impls.items():
        for name in dir(port):
            if name.startswith("_"):
                continue
            port_method = getattr(port, name, None)
            if not callable(port_method):
                continue
            port_params = set(inspect.signature(port_method).parameters)
            for store in stores:
                # Introspection par la CLASSE (non bound) : cohérent avec le
                # port, lui aussi introspecté sur sa classe (le `self` est
                # retiré automatiquement des méthodes liées d'une instance).
                impl_params = set(
                    inspect.signature(getattr(type(store), name)).parameters
                )
                assert port_params <= impl_params, (
                    f"{type(store).__name__}.{name} : {port_params - impl_params}"
                )


# --- Comportement RunStore (paramétré sur les backends) -------------------------


@pytest.mark.parametrize("factory", [_sqlite_run_store, _memory_run_store])
def test_run_lifecycle_roundtrip(factory, tmp_path):
    store = factory(tmp_path)
    row = store.start_run("calcule 1+1", model="m1", source="test")
    run_id = row["id"]
    assert row["status"] == "running"

    store.append_tool_event(run_id, {"event": "tool_start", "tool": "add"})
    store.append_tool_event(run_id, {"event": "tool_result", "tool": "add"})

    finished = store.finish_run(run_id, COMPLETED, answer_summary="2")
    assert finished["status"] == COMPLETED
    assert finished["answer_summary"] == "2"

    fetched = store.get(run_id)
    assert fetched["prompt"] == "calcule 1+1"

    rows = store.list(limit=10)
    assert rows[0]["id"] == run_id                 # les plus récents d'abord
    # Contrat legacy : limit <= 0 = non borné (pas de clause LIMIT SQL).
    assert store.list(limit=0) != []

    # Aucun run n'a un statut error (le test ne crée que des completed) —
    # indépendant de la clôture d'un éventuel run « running » résiduel.
    assert [r["status"] for r in store.list(limit=10)] == [COMPLETED]

    with pytest.raises(ValueError):
        store.finish_run(run_id, "statut-inexistant")


@pytest.mark.parametrize("factory", [_sqlite_run_store, _memory_run_store])
def test_run_list_filters_by_tool(factory, tmp_path):
    store = factory(tmp_path)
    r1 = store.start_run("p1")
    store.append_tool_event(r1["id"], {"event": "tool_start", "tool": "add"})
    store.finish_run(r1["id"], COMPLETED)
    r2 = store.start_run("p2")
    store.finish_run(r2["id"], COMPLETED)

    hits = store.list(limit=10, tool="add")
    assert [r["id"] for r in hits] == [r1["id"]]


@pytest.mark.parametrize("factory", [_sqlite_run_store, _memory_run_store])
def test_finish_unknown_run_returns_none(factory, tmp_path):
    store = factory(tmp_path)
    assert store.finish_run("inconnu", COMPLETED) is None
    assert store.get("inconnu") is None


# --- Comportement ApprovalStore (paramétré sur les backends) --------------------


@pytest.mark.parametrize("factory", [_sqlite_approval_store, _memory_approval_store])
def test_approval_roundtrip_and_state_machine(factory, tmp_path):
    store = factory(tmp_path)
    record = store.create(
        "write_file", {"path": "a.txt"}, "write", "approve",
        "Policy", prompt="p", args_hash="deadbeef",
    )
    request_id = record["request_id"]
    assert record["status"] == "pending"
    assert record["args_hash"] == "deadbeef"

    approved = store.approve(request_id, decided_by="humain")
    assert approved["status"] == "approved"
    assert approved["decided_by"] == "humain"

    # Déjà décidée : renvoyée INCHANGÉE (la route mappe en 409).
    again = store.approve(request_id)
    assert again["status"] == "approved"

    assert store.list(status="approved")[0]["request_id"] == request_id
    assert store.list(status="pending") == []


@pytest.mark.parametrize("factory", [_sqlite_approval_store, _memory_approval_store])
def test_approval_reject_blocks_execution(factory, tmp_path):
    store = factory(tmp_path)
    record = store.create("run_command", {"cmd": "x"}, "execute", "approve", "Policy")
    assert store.reject(record["request_id"])["status"] == REJECTED_STATUS


@pytest.mark.parametrize("factory", [_sqlite_approval_store, _memory_approval_store])
def test_approval_unknown_id_returns_none(factory, tmp_path):
    store = factory(tmp_path)
    assert store.get("inconnu") is None
    assert store.approve("inconnu") is None
    assert store.reject("inconnu") is None


# --- Fabrique de coexistence ------------------------------------------------------


def test_default_factories_delegate_to_legacy_singletons():
    """Les fabriques par défaut renvoient les singletons legacy (même base) —
    coexistence stricte tant que le swap d'implémentation n'a pas lieu."""
    from app.infrastructure.persistence import sqlite as persistence
    from core.approval_store import get_approval_store as legacy_approvals
    from core.run_store import get_run_store as legacy_runs

    assert persistence.default_run_store() is legacy_runs()
    assert persistence.default_approval_store() is legacy_approvals()


# --- Intégration use-case + persistance mémoire (zéro réseau / SQLite) --------
# Preuve du découplage : le use-case de production (run_ask_core) s'exécute
# entièrement en mémoire — mêmes fakes que les tests de persistance, aucun
# disque, aucune base, aucun appel LLM.


def test_run_ask_core_with_memory_stores():
    from app.agent.core import RunStatus
    from app.application.ask_usecase import run_ask_core

    run_store = MemoryRunStore()
    approval_store = MemoryApprovalStore()
    run_calls: list[SimpleNamespace] = []

    class FakeCore:
        def run(self, intent, history):
            run_calls.append(intent)
            return SimpleNamespace(
                status=RunStatus.COMPLETED,
                answer="réponse finale",
                actions=[],
                rounds_used=1,
                tool_calls_used=1,
                awaiting_action=None,
            )

    outcome = run_ask_core(
        prompt="bonjour",
        session_id=None,
        resume_request_id=None,
        model="m1",
        run_store=run_store,
        approval_store=approval_store,
        build_core=lambda **kw: FakeCore(),
        load_history=lambda *a, **k: [],
        persist_exchange=lambda *a, **k: None,
        audit_log=lambda *a, **k: None,
    )

    assert outcome.api_status == "completed"
    assert outcome.answer == "réponse finale"
    # Le run a été persisté dans le store MÉMOIRE et clôturé en completed.
    runs = run_store.list(limit=10)
    assert len(runs) == 1
    assert runs[0]["status"] == COMPLETED
    assert runs[0]["source"] == "ask_core"
    assert len(run_calls) == 1                       # le noyau a bien été invoqué


def test_run_ask_core_pending_approval_with_memory_stores():
    """Chemin d'approbation : le use-case persiste la demande via le store
    mémoire (convention `request_id`), sans ouvrir la base legacy."""
    from app.agent.core import RunStatus
    from app.application.ask_usecase import run_ask_core
    from app.domain.entities.plan import ActionCategory

    run_store = MemoryRunStore()
    approval_store = MemoryApprovalStore()

    class FakeAction:
        tool = "write_file"
        args = {"path": "a.txt"}
        category = ActionCategory.WRITE

        def fingerprint(self) -> str:
            return "abc123"

    class FakeCore:
        def run(self, intent, history):
            return SimpleNamespace(
                status=RunStatus.PENDING_APPROVAL,
                answer="",
                actions=[],
                rounds_used=1,
                tool_calls_used=0,
                awaiting_action=FakeAction(),
            )

    outcome = run_ask_core(
        prompt="écris un fichier",
        session_id="s1",
        resume_request_id=None,
        model="m1",
        run_store=run_store,
        approval_store=approval_store,
        build_core=lambda **kw: FakeCore(),
        load_history=lambda *a, **k: [],
        persist_exchange=lambda *a, **k: None,
        audit_log=lambda *a, **k: None,
    )

    assert outcome.api_status == "awaiting_approval"
    assert outcome.approval is not None
    # La demande existe dans le store mémoire, attend la validation humaine.
    pending = approval_store.list(status="pending")
    assert len(pending) == 1
    assert pending[0]["tool"] == "write_file"
    assert pending[0]["args_hash"] == "abc123"
    # Le run est clôturé en awaiting_approval.
    assert run_store.list(limit=10)[0]["status"] == AWAITING


