"""Tests P1 SEC — masquage des secrets (``core/secrets_redact.py``) et son
câblage dans run_store (prompt, événements d'outils, résumé, erreur), la
policy d'approbation (``_summary``) et le redact d'audit_store.

Lance avec : pytest tests/test_secrets_redact.py -v
"""

import json
import os

from core import run_store
from core.audit_store import redact
from core.secrets_redact import redact_secrets

# --- Primitives -------------------------------------------------------------------


def test_redacts_postgres_dsn() -> None:
    text = "connecte-toi à postgresql://admin:S3cret@db:5432/think ok"
    out = redact_secrets(text)
    assert "S3cret" not in out
    assert "postgresql://***@" in out
    assert "think ok" in out  # le contexte lisible est conservé


def test_redacts_bearer_token() -> None:
    out = redact_secrets("Authorization: Bearer sk-or-v1-abc123def.ghi")
    assert "sk-or-v1-abc123def" not in out
    assert "***" in out  # Bearer masqué (et la paire authorization: aussi)


def test_redacts_key_value_pair() -> None:
    out = redact_secrets("config: API_KEY=sk-abc123 et HF_TOKEN=hf_xyz789")
    assert "sk-abc123" not in out
    assert "hf_xyz789" not in out
    assert "API_KEY=***" in out
    assert "HF_TOKEN=***" in out


def test_redacts_bare_sensitive_word() -> None:
    out = redact_secrets("la variable OPENROUTER_API_KEY contient le secret")
    assert "OPENROUTER_API_KEY" not in out
    assert "***" in out


def test_does_not_touch_benign_words() -> None:
    text = "keyboard, tokenization, keys of a dict, monkeypatch, username"
    assert redact_secrets(text) == text


def test_redacts_recursively_and_preserves_scalars() -> None:
    value = {
        "args": {"dsn": "postgresql://u:p@h/db", "n": 3, "ok": True, "none": None},
        "lines": ["API_KEY=zzz", "safe"],
    }
    out = redact_secrets(value)
    # Les identifiants sont masqués, l'HÔTE est conservé (contexte lisible).
    assert out["args"]["dsn"] == "postgresql://***@h/db"
    assert "u:p@" not in out["args"]["dsn"]
    assert out["args"]["n"] == 3
    assert out["args"]["ok"] is True
    assert out["args"]["none"] is None
    assert "zzz" not in out["lines"][0]
    assert out["lines"][1] == "safe"


# --- Câblage run_store -------------------------------------------------------------


def _fresh_store(tmp_path) -> run_store.RunStore:
    return run_store.reset_run_store(str(tmp_path / "runs.db"))


def test_start_run_redacts_prompt(tmp_path) -> None:
    store = _fresh_store(tmp_path)
    row = store.start_run(
        "utilise postgresql://admin:S3cret@db:5432/x stp", model="m", source="test"
    )
    assert "S3cret" not in row["prompt"]
    assert "postgresql://***@" in row["prompt"]


def test_finish_run_redacts_summary_and_error(tmp_path) -> None:
    store = _fresh_store(tmp_path)
    run_id = store.start_run("prompt banal")["id"]
    row = store.finish_run(
        run_id,
        run_store.COMPLETED,
        answer_summary="réponse avec API_KEY=sk-secret987 dedans",
        error="échec vers postgresql://u:P4ssw0rd@host/db",
    )
    assert "sk-secret987" not in row["answer_summary"]
    assert "P4ssw0rd" not in row["error"]


def test_append_tool_event_redacts_args(tmp_path) -> None:
    store = _fresh_store(tmp_path)
    run_id = store.start_run("prompt")["id"]
    store.append_tool_event(
        run_id,
        {"event": "tool_start", "tool": "http_get", "args": {"url": "https://x",
         "headers": {"Authorization": "Bearer tok123456"}}},
    )
    events = store.get(run_id)["tools"]
    serialized = json.dumps(events, ensure_ascii=False)
    assert "tok123456" not in serialized
    assert "Bearer ***" in serialized


# --- Câblage approvals._summary / audit redact ------------------------------------


def test_approval_summary_redacts_value_secrets() -> None:
    from ia.agent.approvals import _summary

    out = _summary({"path": "x", "dsn": "postgresql://u:p@h/db", "n": 2})
    assert "u:p@" not in out["dsn"]
    # _summary stringifiait déjà les scalaires AVANT P1 (comportement inchangé).
    assert out["n"] == "2"


def test_audit_redact_value_secrets_still_truncates() -> None:
    out = redact({"detail": "token=abcdef123456", "key": "already-keyed"})
    assert "abcdef123456" not in out["detail"]
    assert out["key"] == "[REDACTED]"  # masquage PAR CLÉ intact


def test_env_keys_are_never_leaked_by_redaction_of_environ() -> None:
    """Ceinture-bretelles : même si un événement contient os.environ, les
    clés sensibles connues sont masquées dans le texte."""
    sample = f"API_KEY={os.getenv('API_KEY', 'x')} HOME=/home/u"
    out = redact_secrets(sample)
    assert "API_KEY=" not in out.replace("API_KEY=***", "")
