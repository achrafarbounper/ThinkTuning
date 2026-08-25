"""Tests offline des nouveaux outils de l'agent IA (ia/tools/*).

Aucun réseau réel, aucun démon requis :
    - HTTP simulé (monkeypatch de requests.get/post) ;
    - Docker simulé (monkeypatch de run_subprocess) ;
    - SQLite réelle mais dans un tmp_path ;
    - PostgreSQL simulé (module psycopg2 factice injecté dans sys.modules).

Lance avec : pytest tests/test_agent_tools.py -v
"""

import json
import os
import sqlite3
import sys
import types

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IA_DIR = os.path.join(PROJECT_ROOT, "ia")
for _p in (PROJECT_ROOT, IA_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Config API avant tout import de l'app : clé de l'API principale (routes
# /api/agent/*), cohérent avec tests/test_agent_api.py et test_api_ai_chat.py.
os.environ.setdefault("API_KEY", "test-key")

import pytest

from tools import sandbox
from tools.docker_tools import docker_exec, docker_logs, docker_ps
from tools.system_tools import (
    copy_path,
    find_file,
    list_dir,
    make_dir,
    move_path,
    read_file,
    remove_path,
)


@pytest.fixture()
def sandbox_root(tmp_path, monkeypatch):
    """Redirige la racine de la sandbox vers un dossier temporaire."""
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    return tmp_path


# --- Sandbox : confinement des chemins --------------------------------------------

def test_safe_resolve_accepts_relative_inside_root(sandbox_root):
    resolved = sandbox.safe_resolve("sous_dossier/fichier.txt")
    assert sandbox_root in resolved.parents


def test_safe_resolve_blocks_parent_traversal(sandbox_root):
    with pytest.raises(PermissionError, match="hors sandbox"):
        sandbox.safe_resolve("../secret.txt")


def test_safe_resolve_blocks_absolute_outside_root(sandbox_root):
    outside = sandbox_root.parent / "hors_sandbox.txt"
    with pytest.raises(PermissionError, match="hors sandbox"):
        sandbox.safe_resolve(str(outside))


# --- Outils fichiers ---------------------------------------------------------------

def test_write_list_read_roundtrip(sandbox_root):
    from tools.file_tools import write_file

    message = write_file("notes/a.txt", "bonjour agent")
    assert "écrit" in message
    entries = list_dir("notes")
    assert entries["entry_count"] == 1
    assert entries["entries"][0]["path"] == "a.txt"
    assert read_file("notes/a.txt") == "bonjour agent"


def test_copy_move_and_remove(sandbox_root):
    from tools.file_tools import write_file

    write_file("d1/f.txt", "contenu")
    copy_path("d1/f.txt", "d2/copie.txt")
    move_path("d2/copie.txt", "d2/renomme.txt")
    assert read_file("d2/renomme.txt") == "contenu"
    remove_path("d2/renomme.txt")
    with pytest.raises(FileNotFoundError):
        read_file("d2/renomme.txt")


def test_remove_non_empty_dir_requires_recursive_flag(sandbox_root):
    from tools.file_tools import write_file

    write_file("dossier/f.txt", "x")
    with pytest.raises(ValueError, match="recursive"):
        remove_path("dossier")
    remove_path("dossier", recursive=True)
    assert not (sandbox_root / "dossier").exists()


def test_remove_blocks_git_and_root_itself(sandbox_root):
    make_dir(".git/objects")
    with pytest.raises(PermissionError):
        remove_path(".git")
    with pytest.raises(PermissionError, match="racine"):
        remove_path(".")


def test_read_file_truncates_large_content(sandbox_root):
    from tools.file_tools import write_file

    write_file("gros.txt", "x" * 200_000)
    out = read_file("gros.txt", max_bytes=100)
    assert out.startswith("x" * 100)
    assert "tronqué" in out


def test_find_file_by_regex_returns_relative_paths(sandbox_root):
    """find_file localise un fichier n'importe où sous la racine via une regex."""
    from tools.file_tools import write_file

    write_file("configs/default.yaml", "cle: valeur")
    write_file("configs/other.yaml", "autre: valeur")

    result = find_file(r"default\.yaml$")
    assert result["match_count"] == 1
    assert result["truncated"] is False
    assert result["matches"][0] == {"path": "configs/default.yaml", "type": "file"}
    # Le chemin retourné est directement exploitable par read_file.
    assert read_file(result["matches"][0]["path"]) == "cle: valeur"


def test_find_file_anchored_name_matches_at_any_depth(sandbox_root):
    from tools.file_tools import write_file

    write_file("a/b/profond.yaml", "x")
    result = find_file(r"^profond\.yaml$")  # ancré sur le NOM du fichier
    assert [m["path"] for m in result["matches"]] == ["a/b/profond.yaml"]


def test_find_file_invalid_regex_raises_value_error(sandbox_root):
    with pytest.raises(ValueError, match="Regex invalide"):
        find_file("(non_fermee")


def test_find_file_max_results_and_truncated_flag(sandbox_root):
    from tools.file_tools import write_file

    for i in range(5):
        write_file(f"lot/fichier_{i}.txt", "x")
    result = find_file(r"fichier_\d+\.txt", max_results=2)
    assert result["match_count"] == 2
    assert result["truncated"] is True


def test_find_file_unknown_base_dir_raises_file_not_found(sandbox_root):
    with pytest.raises(FileNotFoundError):
        find_file(".*", path="dossier_inconnu")


def test_read_file_error_message_suggests_find_file(sandbox_root):
    with pytest.raises(FileNotFoundError, match="find_file"):
        read_file("configs/default.yaml")  # n'existe PAS dans cette sandbox


# --- Outils réseau (HTTP simulé) -----------------------------------------------------

class _FakeResponse:
    def __init__(self, status=200, text="ok", url="http://example.test/x"):
        self.status_code = status
        self.reason = "OK" if status == 200 else "Err"
        self.url = url
        self.headers = {"content-type": "text/plain"}
        self.text = text


def test_http_get_returns_structured_result(monkeypatch):
    from tools import network_tools

    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(url=url, headers=headers, timeout=timeout)
        return _FakeResponse(text="contenu")

    monkeypatch.setattr(network_tools.requests, "get", fake_get)
    result = network_tools.http_get("http://example.test/x", headers={"A": "b"}, timeout=7)
    assert result["status"] == 200 and result["body"] == "contenu"
    assert seen["headers"] == {"A": "b"} and seen["timeout"] == 7.0


def test_http_post_json_payload(monkeypatch):
    from tools import network_tools

    captured = {}

    def fake_post(url, headers=None, timeout=None, json=None, data=None):
        captured.update(json=json, data=data)
        return _FakeResponse(status=201)

    monkeypatch.setattr(network_tools.requests, "post", fake_post)
    result = network_tools.http_post("https://api.example.test/v1", json_payload={"x": 1})
    assert result["status"] == 201 and captured["json"] == {"x": 1} and captured["data"] is None
    with pytest.raises(ValueError, match="data.*OU|OU.*data"):
        network_tools.http_post("https://api.example.test", data="a", json_payload={})


def test_http_blocks_bad_scheme_and_private_hosts(sandbox_root, monkeypatch):
    from tools.network_tools import http_get

    with pytest.raises(ValueError, match="Schéma interdit"):
        http_get("ftp://example.test/f")
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "1")
    with pytest.raises(PermissionError, match="privé"):
        http_get("http://192.168.1.1/admin")  # IP privée -> résolution hors-ligne


# --- Outils Docker (sous-processus simulé) ---------------------------------------------

def test_docker_ps_parses_json_lines(monkeypatch):
    from tools import docker_tools

    calls = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        return 0, '{"Names": "web"}\n{"Names": "db"}\n', ""

    monkeypatch.setattr(docker_tools, "run_subprocess", fake_run)
    containers = docker_ps(False)
    assert [c["Names"] for c in containers] == ["web", "db"]
    assert calls[0][:2] == ["docker", "ps"]


def test_docker_logs_combines_stdout_stderr(monkeypatch):
    from tools import docker_tools

    monkeypatch.setattr(
        docker_tools, "run_subprocess",
        lambda argv, **k: (0, "ligne stdout", "ligne stderr"),
    )
    out = docker_logs("web", tail=5)
    assert "ligne stdout" in out and "ligne stderr" in out


def test_docker_exec_reports_returncode(monkeypatch):
    from tools import docker_tools

    argv_seen = []

    def fake_run(argv, **kwargs):
        argv_seen.append([str(a) for a in argv])
        return 3, "", "boom"

    monkeypatch.setattr(docker_tools, "run_subprocess", fake_run)
    result = docker_exec("web", "ls -la /app")
    assert result["returncode"] == 3 and "boom" in result["stderr"]
    # argv se termine par [container, "sh", "-c", command]
    assert argv_seen[0][-3:-1] == ["sh", "-c"]
    assert argv_seen[0][0] == "docker" and "ls -la /app" in argv_seen[0][-1]


def test_docker_failure_raises_clean_error(monkeypatch):
    from tools import docker_tools

    monkeypatch.setattr(
        docker_tools, "run_subprocess",
        lambda argv, **k: (_ for _ in ()).throw(RuntimeError("Exécutable introuvable : docker")),
    )
    with pytest.raises(RuntimeError, match="[Ii]ntrouvable"):
        docker_ps()


def test_docker_real_cli_skipped_by_default():
    pytest.skip("Test d'intégration : lancez avec AGENT_TEST_DOCKER=1 et le démon Docker actif.")


# --- Outil GPU -------------------------------------------------------------------------

def test_gpu_info_structure_without_hardware(monkeypatch):
    from tools.gpu_tools import gpu_info

    monkeypatch.setattr("tools.gpu_tools._query_nvidia_smi", lambda: {})
    info = gpu_info()
    assert {"torch_available", "cuda_available", "devices", "source"} <= set(info)
    if not info["cuda_available"]:
        assert info["device_count"] == 0 and "message" in info


def test_gpu_info_merges_nvidia_smi_stats(monkeypatch):
    from tools.gpu_tools import gpu_info

    fake_smi = {
        0: {
            "smi_name": "Fake GPU",
            "utilization_percent": 42,
            "memory_used_mb": 1024,
            "memory_total_mb": 8192,
        }
    }
    monkeypatch.setattr("tools.gpu_tools._query_nvidia_smi", lambda: fake_smi)
    info = gpu_info()
    device = info["devices"][0]
    assert device["utilization_percent"] == 42
    assert device["memory_used_mb"] == 1024
    assert "nvidia-smi" in info["source"]


# --- Outils bases de données ---------------------------------------------------------

def _make_sqlite_db(path):
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE items (id INTEGER, name TEXT)")
    conn.execute("INSERT INTO items VALUES (1, 'alpha'), (2, 'beta')")
    conn.commit()
    conn.close()


def test_sqlite_readonly_select(sandbox_root):
    from tools.database_tools import sqlite_query

    db = sandbox_root / "data.db"
    _make_sqlite_db(db)
    result = sqlite_query("data.db", "SELECT id, name FROM items ORDER BY id")
    assert result["columns"] == ["id", "name"]
    assert result["rows"] == [[1, "alpha"], [2, "beta"]]
    assert result["truncated"] is False


def test_sqlite_readonly_blocks_writes(sandbox_root):
    from tools.database_tools import sqlite_query

    db = sandbox_root / "data.db"
    _make_sqlite_db(db)
    with pytest.raises(PermissionError, match="lecture seule"):
        sqlite_query("data.db", "DELETE FROM items")
    assert sqlite_query("data.db", "SELECT COUNT(*) FROM items")["rows"] == [[2]]


def test_sqlite_write_mode_allowed_when_explicit(sandbox_root):
    from tools.database_tools import sqlite_query

    sqlite_query("new.db", "CREATE TABLE t (x INTEGER)", readonly=False)
    result = sqlite_query("new.db", "INSERT INTO t VALUES (7)", readonly=False)
    assert result["rows_affected"] == 1


def test_sqlite_blocks_escape_and_max_rows(sandbox_root):
    from tools.database_tools import sqlite_query

    with pytest.raises(PermissionError, match="hors sandbox"):
        sqlite_query("../outside.db", "SELECT 1")

    db = sandbox_root / "many.db"
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE n (v INTEGER)")
    conn.executemany("INSERT INTO n VALUES (?)", [(i,) for i in range(10)])
    conn.commit()
    conn.close()
    result = sqlite_query("many.db", "SELECT v FROM n", max_rows=3)
    assert result["row_count"] == 3 and result["truncated"] is True


class _FakeCursor:
    description = (("col", None, None, None, None, None, None),)

    def execute(self, query, *args):
        if not query.startswith("SET"):
            self.last_query = query

    def fetchmany(self, size):
        return [("value",)]

    def close(self):
        pass

    def __enter__(self):
        return self  # les curseurs psycopg2 sont utilisables en context manager

    def __exit__(self, *exc_info):
        self.close()
        return False


class _FakeConnection:
    def __init__(self, *args, **kwargs):
        pass

    def cursor(self):
        return _FakeCursor()

    def commit(self):
        pass

    def close(self):
        pass


@pytest.fixture()
def fake_psycopg2(monkeypatch):
    """Injecte un module psycopg2 factice (aucun serveur requis)."""
    module = types.ModuleType("psycopg2")
    module.connect = lambda *a, **k: _FakeConnection()
    extras = types.ModuleType("psycopg2.extras")
    module.extras = extras
    monkeypatch.setitem(sys.modules, "postgres", None)  # neutralise tout vrai driver
    monkeypatch.setitem(sys.modules, "psycopg2", module)
    monkeypatch.setitem(sys.modules, "psycopg2.extras", extras)
    return module


def test_postgres_requires_dsn(monkeypatch, fake_psycopg2):
    from tools.database_tools import postgres_query

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    with pytest.raises(RuntimeError, match="AGENT_PG_DSN"):
        postgres_query("SELECT 1")


def test_postgres_select_ok(monkeypatch, fake_psycopg2):
    from tools.database_tools import postgres_query

    monkeypatch.delenv("AGENT_PG_DSN", raising=False)
    result = postgres_query("SELECT col FROM t", dsn="postgresql://u:p@h:5432/db")
    assert result["columns"] == ["col"] and result["rows"] == [["value"]]


def test_postgres_readonly_guard(monkeypatch, fake_psycopg2):
    from tools.database_tools import postgres_query

    for bad in ("DROP TABLE users", "WITH x AS (SELECT 1) DELETE FROM users"):
        with pytest.raises(PermissionError):
            postgres_query(bad, dsn="postgresql://u:p@h/db")


# --- Outils d'exécution ----------------------------------------------------------------

def test_run_command_allows_git_version():
    from tools.shell_tools import run_command

    result = run_command(["git", "--version"], timeout=15)
    assert result["returncode"] == 0
    assert "git version" in result["stdout"].lower()


def test_run_command_blocks_shell_and_string_arg():
    from tools.shell_tools import run_command

    with pytest.raises(PermissionError, match="[Bb]inaire interdit"):
        run_command(["powershell.exe", "-Command", "echo hi"])
    with pytest.raises(PermissionError):
        run_command(["cmd", "/c", "echo hi"])
    with pytest.raises(ValueError, match="LISTE"):
        run_command("git --version")  # chaîne refusée : injection impossible


def test_run_python_executes_and_cleans_up(sandbox_root):
    from tools.shell_tools import run_python

    result = run_python("print('hello-from-agent')")
    assert result["returncode"] == 0 and "hello-from-agent" in result["stdout"]
    assert list((sandbox_root / ".agent_tmp").glob("*.py")) == []  # nettoyé


def test_run_python_timeout_kills_process(sandbox_root):
    from tools.shell_tools import run_python

    with pytest.raises(RuntimeError, match="[Tt]imeout"):
        run_python("import time; time.sleep(30)", timeout=1)


# --- Registre et agent -------------------------------------------------------------------

def test_registry_is_consistent():
    from tools.tool_registry import REQUIRED_ARGS, TOOLS

    assert set(TOOLS) == set(REQUIRED_ARGS)
    for name, fn in TOOLS.items():
        assert callable(fn), name
        assert isinstance(REQUIRED_ARGS[name], list)


def test_expected_tool_names_registered():
    from tools.tool_registry import TOOLS

    expected = {
        "add", "write_file",
        "list_dir", "read_file", "find_file", "make_dir", "copy_path", "move_path", "remove_path",
        "run_command", "run_python",
        "http_get", "http_post",
        "docker_ps", "docker_logs", "docker_exec",
        "gpu_info",
        "sqlite_query", "postgres_query",
    }
    assert expected <= set(TOOLS)


def test_agent_core_reports_tool_error_to_llm(sandbox_root):
    """Une erreur d'exécution ne crash pas l'agent : elle repart au LLM pour correction."""
    from ia.agent.agent_core import AgentCore

    class ScriptedLLM:
        def __init__(self):
            self.prompts = []

        def call(self, messages):
            self.prompts.append(messages[-1]["content"])
            if len(self.prompts) == 1:
                return '{"tool": "read_file", "args": {"path": "../../etc/passwd"}}'
            return "Explication finale."

    core = AgentCore(ScriptedLLM())
    answer = core.run("lis le fichier secret hors sandbox")
    assert answer.startswith("Explication finale.")
    # La trace d'auto-correction est toujours visible pour l'utilisateur.
    assert "[auto-correction]" in answer
    # Le 2e prompt est le message d'auto-correction, qui contient l'erreur
    # détaillée ET la piste find_file pour les chemins.
    assert "ERREUR pendant 'read_file'" in core.llm.prompts[1]
    assert "find_file" in core.llm.prompts[1]


def test_agent_core_self_corrects_missing_args(sandbox_root):
    """Reproduit le bug réel : un appel JSON sans argument obligatoire.

    Avant : mort immédiate sur « Arguments manquants pour … ».
    Maintenant : l'erreur repart au LLM qui renvoie un appel corrigé, exécuté.
    """
    from ia.agent.agent_core import AgentCore
    from tools.file_tools import write_file

    class ScriptedLLM:
        def __init__(self):
            self.prompts = []

        def call(self, messages):
            self.prompts.append(messages[-1]["content"])
            if len(self.prompts) == 1:
                return '{"tool": "find_file", "args": {}}'  # pattern manquant !
            if len(self.prompts) == 2:
                return '{"tool": "find_file", "args": {"pattern": "cible"}}'
            return "Voici ce que j'ai trouvé."

    write_file("configs/cible.yaml", "contenu")

    core = AgentCore(ScriptedLLM())
    answer = core.run("trouve le fichier cible")
    # La réponse finale est bien l'explication du LLM, enrichie de la trace
    # d'auto-correction pour que l'utilisateur voie ce qui s'est passé.
    assert answer.startswith("Voici ce que j'ai trouvé.")
    assert "[auto-correction]" in answer
    assert "Arguments manquants pour find_file" in answer
    assert "Arguments manquants pour find_file" in core.llm.prompts[1]
    assert "'pattern'" in core.llm.prompts[1]
def test_agent_core_accepts_flat_tool_call(sandbox_root):
    """Reproduit le bug réel : arguments au niveau du bloc au lieu de « args ».

    {"tool": "read_file", "path": "..."} déclenchait « Arguments manquants pour
    read_file : ['path'] » EN BOUCLE jusqu'à épuisement du budget (3 messages
    d'auto-correction observés). Désormais l'appel à plat est compris du 1er coup.
    """
    from ia.agent.agent_core import AgentCore
    from tools.file_tools import write_file

    write_file("configs/default.yaml", "cle: valeur")

    class ScriptedLLM:
        def __init__(self):
            self.prompts = []

        def call(self, messages):
            self.prompts.append(messages[-1]["content"])
            if len(self.prompts) == 1:
                return '{"tool": "read_file", "path": "configs/default.yaml"}'
            return "Voici le contenu du fichier."

    core = AgentCore(ScriptedLLM())
    answer = core.run("affiche configs/default.yaml")
    # Aucune auto-correction nécessaire : l'appel à plat est exécuté directement.
    assert answer.startswith("Voici le contenu du fichier.")
    assert "[auto-correction]" not in answer
    assert "cle: valeur" in core.llm.prompts[1]


def test_agent_core_accepts_scalar_args_for_single_required_arg(sandbox_root):
    """Quand le tool n'a qu'un argument obligatoire, la valeur brute est acceptée :
    {"tool": "read_file", "args": "chemin"} au lieu de {"args": {"path": ...}}."""
    from ia.agent.agent_core import AgentCore
    from tools.file_tools import write_file

    write_file("configs/default.yaml", "cle: valeur")

    class ScriptedLLM:
        def __init__(self):
            self.prompts = []

        def call(self, messages):
            self.prompts.append(messages[-1]["content"])
            if len(self.prompts) == 1:
                return '{"tool": "read_file", "args": "configs/default.yaml"}'
            return "Contenu affiché."

    core = AgentCore(ScriptedLLM())
    answer = core.run("affiche configs/default.yaml")
    assert answer.startswith("Contenu affiché.")
    assert "[auto-correction]" not in answer


def test_missing_args_error_shows_received_args(sandbox_root):
    """Le message d'auto-correction montre CE QUI A ÉTÉ REÇU et le format attendu.

    Sans cela, le modèle croit avoir fourni l'argument (fourni sous un mauvais
    nom ou mauvais niveau) et répète indéfiniment la même erreur.
    """
    from ia.agent.agent_core import AgentCore

    class ScriptedLLM:
        def __init__(self):
            self.prompts = []

        def call(self, messages):
            self.prompts.append(messages[-1]["content"])
            if len(self.prompts) == 1:
                # Mauvais nom d'argument ("filename" au lieu de "path").
                return '{"tool": "read_file", "args": {"filename": "a.txt"}}'
            return "Corrigé."

    core = AgentCore(ScriptedLLM())
    answer = core.run("lis a.txt")
    prompt_correction = core.llm.prompts[1]
    assert "Arguments manquants pour read_file" in prompt_correction
    assert "filename" in prompt_correction          # ce qui a été reçu
    assert "'path'" in prompt_correction             # ce qui est attendu
    assert '"args"' in prompt_correction             # le format imbriqué rappelé
    assert "[auto-correction]" in answer


def test_agent_core_chains_find_then_read(sandbox_root):
    """Flux multi-étapes : find_file PUIS read_file avec le chemin trouvé.

    Après chaque succès, le LLM reçoit le résultat et peut enchaîner au lieu
    de devoir conclure immédiatement.
    """
    from ia.agent.agent_core import AgentCore
    from tools.file_tools import write_file

    write_file("configs/secret.yaml", "la réponse")

    class ScriptedLLM:
        def __init__(self):
            self.prompts = []

        def call(self, messages):
            self.prompts.append(messages[-1]["content"])
            if len(self.prompts) == 1:
                return '{"tool": "find_file", "args": {"pattern": "secret"}}'
            if len(self.prompts) == 2:
                # Le modèle construit l'appel suivant à partir du résultat reçu.
                assert "configs/secret.yaml" in self.prompts[1]
                return '{"tool": "read_file", "args": {"path": "configs/secret.yaml"}}'
            return "J'ai trouvé et lu configs/secret.yaml."

    core = AgentCore(ScriptedLLM())
    answer = core.run("lis le fichier secret")
    assert answer.startswith("J'ai trouvé et lu configs/secret.yaml.")
    # Le 2e prompt contient bien le résultat de find_file injecté par l'agent.
    assert "configs/secret.yaml" in core.llm.prompts[1]


def test_json_parser_repairs_unescaped_windows_backslashes():
    """Les LLM écrivent souvent D:\\dossier\\fichier sans doubler les backslashes :
    le JSON strict échoue et le bloc était jeté silencieusement."""
    from ia.agent.json_parser import extract_json_blocks

    raw = r'{"tool": "read_file", "args": {"path": "D:\ThinkTuning\configs\default.yaml"}}'
    blocks = extract_json_blocks(raw)
    assert blocks == [
        {"tool": "read_file", "args": {"path": "D:\\ThinkTuning\\configs\\default.yaml"}}
    ]


def test_json_parser_tolerates_trailing_commas():
    from ia.agent.json_parser import extract_json_blocks

    raw = '{"tool": "add", "args": {"a": 12, "b": 30,}}'
    assert extract_json_blocks(raw) == [{"tool": "add", "args": {"a": 12, "b": 30}}]


def test_json_parser_still_drops_garbage():
    from ia.agent.json_parser import extract_json_blocks

    assert extract_json_blocks("pas de json du tout") == []
    assert extract_json_blocks('{"tool": "add",') == []  # jamais fermé


# --- Intégration API (/api/agent/tools/run avec les nouveaux outils) ------------------------

def test_api_runs_new_tools_end_to_end(tmp_path, monkeypatch):
    """Le serveur autonome ia/api_server.py a été remplacé par les routes
    /api/agent/* du package api : on vérifie les mêmes outils via l'app principale."""
    os.environ.setdefault("API_KEY", "test-key")

    from fastapi.testclient import TestClient

    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    from core import agent_cache  # noqa: F401  (insère ia/ dans sys.path)
    from api import app

    headers = {"X-API-Key": os.environ.get("API_KEY", "test-key")}
    with TestClient(app) as client:
        status = client.get("/api/agent/status")
        assert status.status_code == 200
        assert "gpu_info" in status.json()["tools"]

        created = client.post(
            "/api/agent/tools/run",
            json={"tool": "write_file", "args": {"filename": "api.txt", "content": "via-api"}},
            headers=headers,
        )
        assert created.status_code == 200

        read = client.post(
            "/api/agent/tools/run",
            json={"tool": "read_file", "args": {"path": "api.txt"}},
            headers=headers,
        )
        assert read.status_code == 200 and "via-api" in read.json()["result"]

        gpu = client.post(
            "/api/agent/tools/run", json={"tool": "gpu_info", "args": {}}, headers=headers,
        )
        assert gpu.status_code == 200
        assert isinstance(gpu.json()["result"]["devices"], list)

        listing = client.post(
            "/api/agent/tools/run", json={"tool": "list_dir", "args": {"path": "."}}, headers=headers,
        )
        assert listing.status_code == 200
        names = [e["path"] for e in listing.json()["result"]["entries"]]
        assert "api.txt" in names