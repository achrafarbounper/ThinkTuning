"""Tests P1 — confinement agent (points 7→9 du plan) : défense en profondeur
fichiers, docker_exec argv-list + allowlist, run_python isolé (env minimal +
scan AST). Aucun réseau ni daemon réel : subprocess/daemon mockés.

Couvre :
    - ``app/domain/security.py`` : source unique des cibles sensibles ;
    - ``ia/tools/sandbox.ensure_writable_target`` / ``safe_resolve(for_write=)`` ;
    - outils d'écriture branchés (write_file, append_file, remove_path) ;
    - ``ia/tools/docker_tools.docker_exec`` : refus chaîne, allowlist conteneurs ;
    - ``ia/tools/shell_tools.run_python`` : env minimal sans secrets + scan AST.

Lance avec : pytest tests/test_security_tools.py -v
"""

import json

import pytest

from app.domain.security import DENIED_EXTENSIONS, DENIED_PATH_PARTS, classify_path_risk
from ia.tools import docker_tools, file_tools, sandbox, search_tools, shell_tools, system_tools

# --- app/domain/security.py ------------------------------------------------------


def test_classify_path_risk_sensitive() -> None:
    assert classify_path_risk(".env")
    assert classify_path_risk("data/.env")
    assert classify_path_risk("data\\venv\\lib")  # séparateur Windows
    assert classify_path_risk(".git/config")
    assert classify_path_risk("certs/server.key")
    assert classify_path_risk("certs/server.pem")
    assert classify_path_risk("id_rsa")
    assert classify_path_risk("id_ed25519")
    assert classify_path_risk("secrets/keystore.p12")


def test_classify_path_risk_benign() -> None:
    assert not classify_path_risk("data/dataset.csv")
    assert not classify_path_risk("src/model/trainer.py")
    assert not classify_path_risk("outputs/run_1/metrics.json")


def test_constants_re_exported_by_policy() -> None:
    """Zéro régression P1 : la policy ré-exporte la source unique (déplacement)."""
    from app.agent.policies.sandbox_policy import (  # noqa: PLC0415
        DENIED_EXTENSIONS as POLICY_EXTS,
    )
    from app.agent.policies.sandbox_policy import (
        DENIED_PATH_PARTS as POLICY_PARTS,
    )

    assert POLICY_PARTS == DENIED_PATH_PARTS
    assert POLICY_EXTS == DENIED_EXTENSIONS


# --- sandbox : ensure_writable_target / safe_resolve(for_write=True) -----------


def test_ensure_writable_target_blocks_sensitive(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    (tmp_path / "data").mkdir()
    (tmp_path / "certs").mkdir()
    (tmp_path / ".env").write_text("x=1", encoding="utf-8")
    (tmp_path / "certs" / "server.key").write_text("k", encoding="utf-8")

    with pytest.raises(PermissionError):
        sandbox.ensure_writable_target(tmp_path)  # racine
    with pytest.raises(PermissionError):
        sandbox.ensure_writable_target(tmp_path / ".git" / "config")
    with pytest.raises(PermissionError):
        sandbox.ensure_writable_target(tmp_path / ".env")
    with pytest.raises(PermissionError):
        sandbox.ensure_writable_target(tmp_path / "certs" / "server.key")
    with pytest.raises(PermissionError):
        sandbox.ensure_writable_target(tmp_path / "data" / "id_rsa")

    # Cible banale acceptée (les parents ne sont PAS créés ici : rôle du caller).
    sandbox.ensure_writable_target(tmp_path / "data" / "out.csv")


def test_safe_resolve_for_write_flag(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text("API_KEY=x", encoding="utf-8")

    # Lecture (for_write=False) : tolérée — même politique que la policy
    # (deny-write uniquement, cf. docs/SECURITY_DIAGNOSTIC.md).
    assert sandbox.safe_resolve(".env", must_exist=True) == (tmp_path / ".env").resolve()

    # Écriture : refusée physiquement, même sans passer par la policy.
    with pytest.raises(PermissionError):
        sandbox.safe_resolve(".env", for_write=True)

    # Écriture banale acceptée.
    ok = sandbox.safe_resolve("outputs/out.txt", for_write=True)
    assert ok == (tmp_path / "outputs" / "out.txt").resolve()


def test_write_tools_block_sensitive_targets(tmp_path, monkeypatch) -> None:
    """Câblage physique : write_file / append_file / remove_path refusent .env."""
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    (tmp_path / ".env").write_text("API_KEY=x", encoding="utf-8")

    with pytest.raises(PermissionError):
        file_tools.write_file(".env", "API_KEY=overridden")
    with pytest.raises(PermissionError):
        search_tools.append_file(".env", "\nMALICIOUS=1")
    with pytest.raises(PermissionError):
        system_tools.remove_path(".env")

    # Le .env original est intact.
    assert (tmp_path / ".env").read_text(encoding="utf-8") == "API_KEY=x"

    # Écriture banale OK (création des parents incluse).
    assert "écrit" in file_tools.write_file("outputs/run/metrics.json", "{}")


# --- docker_exec : argv-list + allowlist ---------------------------------------


def test_docker_exec_rejects_string_command() -> None:
    # Une CHAÎNE (ex: `sh -c "ls; rm -rf /"`) est rejetée : plus jamais de
    # shell implicite délégué au conteneur.
    with pytest.raises(ValueError):
        docker_tools.docker_exec("thinktuning-app", "ls -la")


def test_docker_exec_rejects_container_outside_allowlist(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DOCKER_ALLOWED_CONTAINERS", "thinktuning-app")
    # Inventaire daemon injoignable -> l'allowlist CSV reste l'arbitre unique.
    monkeypatch.setattr(docker_tools, "_docker_inventory", lambda: {})

    with pytest.raises(PermissionError):
        docker_tools.docker_exec("db-postgres", ["echo", "hi"])


def test_docker_exec_fail_closed_when_allowlist_empty(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DOCKER_ALLOWED_CONTAINERS", " ")
    with pytest.raises(PermissionError):
        docker_tools.docker_exec("thinktuning-app", ["echo", "hi"])


def test_docker_exec_accepts_allowlisted_container_and_builds_argv(monkeypatch) -> None:
    monkeypatch.setenv("AGENT_DOCKER_ALLOWED_CONTAINERS", "thinktuning-app")
    calls: list[list] = []

    def _fake_run(argv, **kwargs):  # noqa: ARG001
        calls.append(argv)
        return (0, "ok", "")

    monkeypatch.setattr(docker_tools, "run_subprocess", _fake_run)

    result = docker_tools.docker_exec(
        "thinktuning-app", ["ls", "-la"], workdir="/app", user="root"
    )
    assert calls == [
        ["docker", "exec", "--workdir", "/app", "--user", "root",
         "thinktuning-app", "ls", "-la"]
    ]
    assert result["returncode"] == 0
    assert "sh" not in calls[0] and "-c" not in calls[0]  # jamais de shell


def test_docker_exec_resolves_name_via_daemon_inventory(monkeypatch) -> None:
    """Défense simple : un conteneur réel (nom/ID) hors CSV mais visible par
    `docker ps` est accepté — pas de blocage d'un conteneur légitime."""
    monkeypatch.setenv("AGENT_DOCKER_ALLOWED_CONTAINERS", "thinktuning-app")
    monkeypatch.setattr(
        docker_tools,
        "_docker_inventory",
        lambda: {"worker-1": "abc123def456", "thinktuning-app": "def000"},
    )
    calls: list[list] = []

    def _fake_run(argv, **kwargs):  # noqa: ARG001
        calls.append(argv)
        return (0, "", "")

    monkeypatch.setattr(docker_tools, "run_subprocess", _fake_run)

    docker_tools.docker_exec("worker-1", ["uptime"])
    assert calls == [["docker", "exec", "worker-1", "uptime"]]


# --- run_python : env minimal + scan AST ------------------------------------------


def test_run_python_minimal_env_no_secrets(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("API_KEY", "super-secret-admin")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-secret")
    monkeypatch.setenv("HF_TOKEN", "hf_secret")
    monkeypatch.setenv("AGENT_PG_DSN", "postgresql://u:p@h/db")
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))

    result = shell_tools.run_python(
        "import json, os\nprint(json.dumps(sorted(os.environ.keys())))"
    )
    assert result["returncode"] == 0, result["stderr"]
    keys = json.loads(result["stdout"].strip())
    assert "API_KEY" not in keys
    assert "OPENROUTER_API_KEY" not in keys
    assert "HF_TOKEN" not in keys
    assert "AGENT_PG_DSN" not in keys
    assert "PYTHONDONTWRITEBYTECODE" in keys
    assert "PYTHONUNBUFFERED" in keys


@pytest.mark.parametrize(
    "code",
    [
        "import socket\ns = socket.socket()",
        "import urllib.request\nurllib.request.urlopen('http://x')",
        "import requests\nrequests.get('http://x')",
        "import httpx\nhttpx.get('http://x')",
        "import subprocess\nsubprocess.run(['ls'])",
        "import pty\npty.spawn(['sh'])",
        "import os\nos.system('ls')",
        "import os\nos.popen('ls')",
        "import os\nos.execv('/bin/sh', ['sh'])",
        "from os import system\nsystem('ls')",
        "eval('1+1')",
        "exec('x = 1')",
        "compile('x', '<s>', 'exec')",
        "import importlib\nimportlib.import_module('socket')",
    ],
)
def test_run_python_rejects_unsafe_snippets(code: str, monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    with pytest.raises(PermissionError):
        shell_tools.run_python(code)


def test_run_python_allows_legitimate_os_usage(monkeypatch, tmp_path) -> None:
    """`os` reste autorisé HORS exec* : trop de snippets légitimes en dépendent."""
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    result = shell_tools.run_python(
        "import math, os\n"
        "p = os.path.join('data', 'x.csv')\n"
        "print(math.factorial(4), p)"
    )
    assert result["returncode"] == 0, result["stderr"]
    assert "24" in result["stdout"]
    assert "x.csv" in result["stdout"]
