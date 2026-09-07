# project/tests/test_sandbox_policy.py
"""Tests de la policy de sandbox décisionnelle (pure, sans I/O)."""

from app.agent.policies.sandbox_policy import (
    classify_path_risk,
    classify_tool,
    decide,
    decide_action,
    is_private_host,
)
from app.domain.entities.plan import Action, ActionCategory, Decision

# --- Classification -----------------------------------------------------------


def test_classify_known_tools() -> None:
    assert classify_tool("read_file") is ActionCategory.READ
    assert classify_tool("write_file") is ActionCategory.WRITE
    assert classify_tool("remove_path") is ActionCategory.DELETE
    assert classify_tool("run_command") is ActionCategory.EXEC
    assert classify_tool("web_search") is ActionCategory.NETWORK
    assert classify_tool("docker_stats") is ActionCategory.SYSTEM
    assert classify_tool("outil_inconnu") is ActionCategory.UNKNOWN


def test_classify_path_risk() -> None:
    assert classify_path_risk(".git/config")
    assert classify_path_risk("data\\venv\\lib")  # séparateur Windows
    assert classify_path_risk(".env")
    assert classify_path_risk("certs/server.key")
    assert classify_path_risk("id_rsa")
    assert not classify_path_risk("data/dataset.csv")
    assert not classify_path_risk("src/model/trainer.py")


def test_is_private_host() -> None:
    assert is_private_host("http://127.0.0.1:8000/api")
    assert is_private_host("http://192.168.1.184:11434/api/chat")
    assert is_private_host("http://localhost/x")
    assert is_private_host("http://10.0.0.5/")
    assert not is_private_host("https://openrouter.ai/api/v1/chat/completions")
    assert is_private_host("not-an-url")


# --- Décisions ------------------------------------------------------------------


def test_reads_auto_approved() -> None:
    assert decide("read_file", {"path": "data/x.csv"}) is Decision.AUTO_APPROVE
    assert decide("list_dir", {"path": "data"}) is Decision.AUTO_APPROVE
    assert decide("git_status", {}) is Decision.AUTO_APPROVE


def test_writes_require_human_approval() -> None:
    assert decide("write_file", {"path": "outputs/x.txt"}) is Decision.APPROVE
    assert decide("remove_path", {"path": "outputs/x.txt"}) is Decision.APPROVE
    assert decide("run_command", {"command": ["git", "--version"]}) is Decision.APPROVE


def test_unknown_tool_requires_approval() -> None:
    assert decide("outil_bizarre", {"x": 1}) is Decision.APPROVE


def test_hard_reject_sensitive_paths_on_write() -> None:
    assert decide("write_file", {"path": ".env"}) is Decision.REJECT
    assert decide("remove_path", {"path": ".git/config"}) is Decision.REJECT
    assert decide("write_file", {"path": "certs/server.key"}) is Decision.REJECT


def test_hard_reject_mutating_sql() -> None:
    assert decide("sqlite_query", {"query": "SELECT count(*) FROM t"}) is Decision.AUTO_APPROVE
    assert decide("postgres_query", {"query": "WITH x AS (SELECT 1) SELECT * FROM x"}) is Decision.AUTO_APPROVE
    assert decide("sqlite_query", {"query": "DELETE FROM t"}) is Decision.REJECT
    assert decide("postgres_query", {"query": "DROP TABLE users"}) is Decision.REJECT
    assert decide("postgres_query", {"query": "INSERT INTO t VALUES (1)"}) is Decision.REJECT


def test_hard_reject_private_host_post() -> None:
    assert decide("http_post", {"url": "http://127.0.0.1:8000/run"}) is Decision.REJECT
    assert decide("http_post", {"url": "https://api.exemple.com/x"}) is Decision.APPROVE
    # GET vers un hôte privé : catégorie network lisible, auto-apprové
    assert decide("http_get", {"url": "http://127.0.0.1:8000/health"}) is Decision.AUTO_APPROVE


def test_decide_action_entity() -> None:
    action = Action(tool="write_file", args={"path": ".env"}, category=ActionCategory.WRITE)
    assert decide_action(action) is Decision.REJECT
    action_ok = Action(tool="read_file", args={"path": "a.txt"}, category=ActionCategory.READ)
    assert decide_action(action_ok) is Decision.AUTO_APPROVE
