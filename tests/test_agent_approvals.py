# project/tests/test_agent_approvals.py

"""Tests offline du système de décision auto_approve / approve / reject.

Couvre le moteur de décision (ia/agent/approvals.py), le store SQLite
(core/approval_store.py), le gate intégré à AgentCore et les endpoints
/api/agent/approvals*. Aucun réseau : le LLM est un client scripté.
Lance avec : pytest tests/test_agent_approvals.py -v
"""

import hashlib
import json
import os
import sys
from datetime import datetime

os.environ.setdefault("API_KEY", "test-key")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from core import agent_cache  # noqa: E402  (insère ia/ dans sys.path)
from core.approval_store import (  # noqa: E402
    APPROVED,
    PENDING,
    REJECTED,
    ApprovalStore,
    reset_approval_store,
)
from api import app  # noqa: E402

# Le moteur vit dans « ia/agent/approvals.py » : on garantit la racine « ia/ »
# dans sys.path (comme le fait core.agent_cache au runtime) avant l'import.
_IA_ROOT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "ia")
if _IA_ROOT not in sys.path:
    sys.path.insert(0, _IA_ROOT)
import agent.approvals as approvals_module  # noqa: E402

classify = approvals_module.classify          # moteur de décision
Decision = approvals_module.Decision          # enum auto/approve/reject


AgentCore = agent_cache.AgentCore
AgentRunner = agent_cache.AgentRunner

HEADERS = {"X-API-Key": "test-key"}
client = TestClient(app)


class ScriptedLLM:
    """Client LLM fictif : réponses dépilées, puis repli prudent.

    Sans réponse scriptée restante : conclut en texte si le dernier message
    utilisateur signale qu'un outil a tourné, sinon échoue franchement.
    """

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def call(self, messages):
        self.calls.append([dict(m) for m in messages])
        if self.replies:
            return self.replies.pop(0)
        last = messages[-1]["content"]
        if last.startswith(("Dernier résultat", "Nombre maximum")):
            return "L'action demandée est terminée."
        raise AssertionError(f"Appel LLM non scripté : {last[:120]!r}")


@pytest.fixture
def sandbox_root(tmp_path, monkeypatch):
    """Racine de sandbox isolée pour chaque test (relue à chaque appel)."""
    root = tmp_path / "sandbox"
    root.mkdir()
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(root))
    return root


def _make_agent(llm, store):
    return AgentCore(llm, approval_store=store, enable_logging=False)


# --- Moteur de décision -----------------------------------------------------------------

class TestClassify:
    def test_read_only_tools_are_auto_approved(self):
        for tool, args in [("read_file", {"path": "a.txt"}),
                           ("list_dir", {"path": "."}),
                           ("calc", {"expression": "1+1"}),
                           ("git_status", {})]:
            decision = classify(tool, args)
            assert decision.decision == Decision.AUTO_APPROVE, tool

    def test_mutating_tools_require_human_approval(self):
        for tool, args in [("write_file", {"path": "a.txt", "content": "x"}),
                           ("remove_path", {"path": "tmp.txt"}),
                           ("run_python", {"code": "print(1)"})]:
            decision = classify(tool, args)
            assert decision.decision == Decision.APPROVE, tool

    def test_hard_rule_blocks_mutation_under_git(self):
        """Règle dure : TOUTE mutation sous .git est rejetée, sans exécution."""
        decision = classify("remove_path", {"path": "repo/.git/config"})
        assert decision.decision == Decision.REJECT
        assert ".git" in decision.reason

    def test_escape_outside_sandbox_is_rejected(self, sandbox_root):
        decision = classify("write_file", {"path": "../evil.txt", "content": "x"})
        assert decision.decision == Decision.REJECT
        assert "hors sandbox" in decision.reason.lower()

    def test_run_command_fine_grained_classification(self):
        git_read = classify("run_command", {"command": ["git", "status"]})
        pip_install = classify("run_command",
                               {"command": ["pip", "install", "flask"]})
        shell = classify("run_command",
                         {"command": ["powershell.exe", "-Command", "dir"]})
        assert git_read.decision == Decision.AUTO_APPROVE
        assert pip_install.decision == Decision.APPROVE
        assert shell.decision == Decision.REJECT

    def test_sql_queries_split_on_readonly_flag(self):
        ro = classify("sqlite_query",
                      {"db_path": "experiments/jobs.db", "query": "SELECT 1"})
        rw = classify("postgres_query",
                      {"query": "DELETE FROM t", "readonly": False})
        assert ro.decision == Decision.AUTO_APPROVE
        assert rw.decision == Decision.APPROVE

    def test_unknown_tool_fails_closed(self):
        """Tout outil sans politique explicite exige l'humain (échec fermé)."""
        assert classify("outil_inconnu", {}).decision == Decision.APPROVE

    def test_policy_json_is_structured_and_timestamped(self, sandbox_root):
        decision = classify("write_file", {"path": "a.txt", "content": "x" * 900})
        data = decision.to_dict()
        # Clés stables garantissant traçabilité et cohérence.
        assert set(data) == {
            "tool", "args", "decision", "category", "reason",
            "args_hash", "timestamp",
        }
        # Horodatage ISO UTC sérialisable et triable.
        assert data["timestamp"].endswith("Z")
        parsed = datetime.fromisoformat(data["timestamp"].replace("Z", "+00:00"))
        assert parsed.tzinfo is not None
        # Empreinte SHA-256 déterministe des arguments canoniques.
        payload = json.dumps(decision.args, sort_keys=True, default=str,
                             ensure_ascii=False)
        expected_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
        assert data["args_hash"] == expected_hash


# --- Store SQLite -----------------------------------------------------------------------

class TestApprovalStore:
    def test_create_get_roundtrip(self, tmp_path):
        store = ApprovalStore(str(tmp_path / "a.db"))
        request_id = store.create(
            tool="write_file", args={"path": "a.txt"},
            category="write", decision="approve",
            reason="validation humaine", prompt="écris a",
            args_hash="deadbeef", status=PENDING,
        )
        row = store.get(request_id)
        assert row["status"] == PENDING
        assert row["tool"] == "write_file"
        assert row["args"] == {"path": "a.txt"}       # args désérialisés
        assert row["created_at"].endswith("Z")
        assert row["request_id"] == request_id

    def test_get_unknown_returns_none(self, tmp_path):
        assert ApprovalStore(str(tmp_path / "a.db")).get("nope") is None

    def test_approve_then_decided_fields_filled(self, tmp_path):
        store = ApprovalStore(str(tmp_path / "a.db"))
        rid = store.create(tool="add", args={"a": 1}, category="read",
                           decision="approve", reason="")
        row = store.approve(rid, decided_by="alice")
        assert row["status"] == APPROVED
        assert row["decided_by"] == "alice"
        assert row["decided_at"].endswith("Z")

    def test_reject_marks_row_and_keeps_trace(self, tmp_path):
        store = ApprovalStore(str(tmp_path / "a.db"))
        rid = store.create(tool="run_python", args={}, category="exec",
                           decision="reject", reason="code arbitraire")
        store.reject(rid, decided_by="bob")
        assert store.get(rid)["status"] == REJECTED
        statuses = {r["status"] for r in store.list()}
        assert REJECTED in statuses                    # journal conservé

    def test_rows_persist_across_instances(self, tmp_path):
        path = str(tmp_path / "a.db")
        rid = ApprovalStore(path).create(tool="t", args={}, category="read",
                                         decision="approve", reason="")
        assert ApprovalStore(path).get(rid) is not None

    def test_list_filters_by_status(self, tmp_path):
        store = ApprovalStore(str(tmp_path / "a.db"))
        store.create(tool="a", args={}, category="read", decision="approve",
                     reason="")
        rid2 = store.create(tool="b", args={}, category="read",
                            decision="reject", reason="", status=REJECTED)
        pendings = [r["id"] for r in store.list(PENDING)]
        rejects = [r["id"] for r in store.list(REJECTED)]
        assert rid2 not in pendings and rid2 in rejects


# --- Gate intégré à AgentCore -----------------------------------------------------------

WRITE_CALL = ('{"tool": "write_file", '
              '"args": {"filename": "notes.txt", "content": "hello"}}')


class TestAgentCoreGate:
    def test_auto_approved_tool_runs_immediately(self, tmp_path, sandbox_root):
        llm = ScriptedLLM([
            '{"tool": "add", "args": {"a": 12, "b": 30}}',
            "Le résultat de l'addition est 42.",
        ])
        runner = AgentRunner(_make_agent(llm, ApprovalStore(str(tmp_path / "g.db"))))

        result = runner.ask_detailed("combien font 12 et 30 ?")

        agent = runner.agent
        assert agent.awaiting_request_id is None
        assert agent.rejected_request_id is None
        assert agent.last_approval.decision == Decision.AUTO_APPROVE
        assert "42" in result.answer                   # l'outil a tourné

    def test_approval_flow_blocks_executes_only_after_human_ok(
        self, tmp_path, sandbox_root
    ):
        llm = ScriptedLLM([WRITE_CALL])
        store = ApprovalStore(str(tmp_path / "g.db"))
        runner = AgentRunner(_make_agent(llm, store))

        # 1er passage : le run S'ARRÊTE en attente, sans rien écrire.
        blocked = runner.ask_detailed("écris hello dans notes.txt")
        agent = runner.agent
        request_id = agent.awaiting_request_id
        assert request_id, "le run doit s'arrêter sur une demande approve"
        assert "[En attente de validation]" in blocked.answer
        assert not (sandbox_root / "notes.txt").exists()
        assert store.get(request_id)["status"] == PENDING

        # Validation humaine puis REPRISE : exécution exactement une fois.
        assert store.approve(request_id)["status"] == APPROVED
        final = runner.ask_detailed("écris hello dans notes.txt",
                                    resume_request_id=request_id)
        content = (sandbox_root / "notes.txt").read_text(encoding="utf-8")
        assert content == "hello"
        assert "En attente" not in final.answer

    def test_rejected_action_never_runs(self, tmp_path, sandbox_root):
        llm = ScriptedLLM(['{"tool": "run_command", '
                           '"args": {"command": ["powershell.exe", "-c", "dir"]}}'])
        runner = AgentRunner(_make_agent(llm, ApprovalStore(str(tmp_path / "g.db"))))

        result = runner.ask_detailed("liste le dossier avec powershell")

        agent = runner.agent
        assert agent.awaiting_request_id is None
        assert agent.rejected_request_id, "un reject doit tracer un refus"
        assert "[Action refusée" in result.answer
        assert store_of(runner).get(agent.rejected_request_id)["decision"] == "reject"

    def test_last_approval_carries_structured_json(self, tmp_path, sandbox_root):
        llm = ScriptedLLM([WRITE_CALL])
        runner = AgentRunner(_make_agent(llm, ApprovalStore(str(tmp_path / "g.db"))))
        runner.ask_detailed("écris hello")

        data = runner.agent.last_approval.to_dict()
        assert data["decision"] == "approve"
        assert data["timestamp"].endswith("Z")


def store_of(runner):
    return runner.agent._get_store()


# --- Endpoints HTTP ---------------------------------------------------------------------

class TestApprovalsApi:
    def test_list_approvals_returns_seeded_pending(self, tmp_path):
        store = reset_approval_store(str(tmp_path / "api.db"))
        rid = store.create(tool="write_file", args={"path": "x.txt"},
                           category="write", decision="approve",
                           reason="validation humaine")

        resp = client.get("/api/agent/approvals?status=pending", headers=HEADERS)

        assert resp.status_code == 200
        assert rid in [row["id"] for row in resp.json()["approvals"]]

    def test_approve_endpoint_flips_status_and_keeps_trace(self, tmp_path):
        store = reset_approval_store(str(tmp_path / "api2.db"))
        rid = store.create(tool="append_file", args={}, category="write",
                           decision="approve", reason="")

        resp = client.post(f"/api/agent/approvals/{rid}/approve", headers=HEADERS)

        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == APPROVED
        assert body["approval"]["id"] == rid
        assert body["approval"]["decided_at"].endswith("Z")   # trace horodatée

    def test_reject_endpoint_marks_refusal(self, tmp_path):
        store = reset_approval_store(str(tmp_path / "api3.db"))
        rid = store.create(tool="run_python", args={}, category="exec",
                           decision="reject", reason="", status=PENDING)

        resp = client.post(f"/api/agent/approvals/{rid}/reject", headers=HEADERS)

        assert resp.status_code == 200
        assert resp.json()["status"] == REJECTED
        assert store.get(rid)["status"] == REJECTED

    def test_unknown_request_id_is_a_client_error(self, tmp_path):
        reset_approval_store(str(tmp_path / "api4.db"))
        resp = client.post("/api/agent/approvals/inconnu/approve", headers=HEADERS)
        assert resp.status_code >= 400
