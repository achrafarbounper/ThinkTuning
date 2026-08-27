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
import time
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


# --- Outils Internet (recherche web + fetch + read, HTTP simulé) ----------------------

_PAGE_HTML = """
<html>
  <head><title>Page de test</title><style>body { color: red; }</style></head>
  <body>
    <h1>Titre principal</h1>
    <p>Premier paragraphe utile.</p>
    <script>console.log('secret');</script>
    <p>Deuxième &amp; dernier paragraphe.</p>
  </body>
</html>
"""

_LITE_HTML = """
<html><body><table>
<tr><td>&nbsp;1.&nbsp;</td></tr>
<tr><td class="result-link"><a rel="nofollow" class="result-link"
      href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F&amp;rut=sig">Python 3 docs</a></td></tr>
<tr><td class="result-snippet">Documentation officielle.</td></tr>
<tr><td class="result-link"><a rel="nofollow" class="result-link"
      href="https://realpython.com/">Real Python</a></td></tr>
<tr><td class="result-snippet">Tutoriels Python.</td></tr>
</table></body></html>
"""


class _FakeHtmlResponse(_FakeResponse):
    def __init__(self, text, status=200, url="http://example.test/x"):
        super().__init__(status=status, text=text, url=url)
        self.headers = {"content-type": "text/html; charset=utf-8"}


def test_web_search_parses_duckduckgo_results(monkeypatch):
    from tools import web_tools

    seen = {}

    def fake_post(url, data=None, headers=None, timeout=None):
        seen.update(url=url, data=data)
        return _FakeHtmlResponse(_LITE_HTML, url="https://lite.duckduckgo.com/lite/")

    monkeypatch.setattr(web_tools.requests, "post", fake_post)
    result = web_tools.web_search("python tutorial")

    assert seen["data"] == {"q": "python tutorial"}   # requête envoyée en POST
    assert result["engine"] == "duckduckgo-lite" and result["result_count"] == 2
    premier, second = result["results"]
    assert premier["url"] == "https://docs.python.org/3/"   # lien uddg « déballé »
    assert premier["title"] == "Python 3 docs"
    assert premier["snippet"] == "Documentation officielle."
    assert second["url"] == "https://realpython.com/"


def test_web_search_max_results_and_empty_query(monkeypatch):
    from tools import web_tools

    monkeypatch.setattr(
        web_tools.requests, "post", lambda *a, **k: _FakeHtmlResponse(_LITE_HTML)
    )
    result = web_tools.web_search("python", max_results=1)
    assert result["result_count"] == 1 and result["truncated"] is True
    with pytest.raises(ValueError, match="vide"):
        web_tools.web_search("   ")


def test_web_search_reports_http_error_without_raising(monkeypatch):
    from tools import web_tools

    monkeypatch.setattr(
        web_tools.requests, "post",
        lambda *a, **k: _FakeHtmlResponse("<html></html>", status=403),
    )
    result = web_tools.web_search("test")
    assert result["result_count"] == 0 and "403" in result["error"]


def test_web_search_filters_duckduckgo_ad_links(monkeypatch):
    """Les annonces (URL finale restée sur duckduckgo.com/y.js) sont exclues."""
    from tools import web_tools

    html = (
        "<html><body><table>"
        '<tr><td class="result-link"><a class="result-link" '
        'href="https://duckduckgo.com/y.js?ad_domain=shop.example">Super promo</a></td></tr>'
        '<tr><td class="result-snippet">Publicité.</td></tr>'
        '<tr><td class="result-link"><a class="result-link" '
        'href="https://docs.python.org/">Python docs</a></td></tr>'
        '<tr><td class="result-snippet">Doc officielle.</td></tr>'
        "</table></body></html>"
    )
    monkeypatch.setattr(
        web_tools.requests, "post", lambda *a, **k: _FakeHtmlResponse(html)
    )
    result = web_tools.web_search("achat")
    assert result["result_count"] == 1
    assert result["results"][0]["url"] == "https://docs.python.org/"
    assert result["results"][0]["snippet"] == "Doc officielle."


def test_web_search_blocks_private_hosts_like_http(monkeypatch):
    """Même politique SSRF que http_get : endpoint privé refusé si flag actif."""
    from tools import web_tools

    monkeypatch.setattr(web_tools, "SEARCH_ENDPOINT", "http://127.0.0.1:9999/lite/")
    monkeypatch.setenv("AGENT_BLOCK_PRIVATE_HOSTS", "1")
    with pytest.raises(PermissionError, match="privé"):
        web_tools.web_search("test")


def test_web_read_extracts_readable_text(monkeypatch):
    from tools import web_tools

    monkeypatch.setattr(
        web_tools.requests, "get", lambda *a, **k: _FakeHtmlResponse(_PAGE_HTML)
    )
    result = web_tools.web_read("http://example.test/article")
    assert result["status"] == 200 and result["title"] == "Page de test"
    assert "Titre principal" in result["text"]
    assert "Premier paragraphe utile." in result["text"]
    assert "Deuxième & dernier" in result["text"]      # entités décodées
    assert "console.log" not in result["text"]         # scripts supprimés
    assert "color: red" not in result["text"]          # styles supprimés


def test_web_fetch_returns_structured_page(monkeypatch):
    from tools import web_tools

    seen = {}

    def fake_get(url, headers=None, timeout=None):
        seen.update(headers=headers)
        return _FakeHtmlResponse(_PAGE_HTML)

    monkeypatch.setattr(web_tools.requests, "get", fake_get)
    result = web_tools.web_fetch(
        "http://example.test/page", headers={"X-Custom": "1"}, max_chars=200
    )
    assert seen["headers"]["X-Custom"] == "1"           # en-têtes fusionnés
    assert "User-Agent" in seen["headers"]              # UA navigateur ajouté
    assert result["status"] == 200 and result["title"] == "Page de test"
    assert "<h1>" in result["body"]                     # corps BRUT
    assert "tronqué" in result["body"]                  # plafond max_chars appliqué


def test_web_fetch_and_read_block_bad_scheme():
    from tools.web_tools import web_fetch, web_read

    with pytest.raises(ValueError, match="Schéma interdit"):
        web_fetch("ftp://example.test/f")
    with pytest.raises(ValueError, match="Schéma interdit"):
        web_read("file:///etc/passwd")


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
        "web_search", "web_fetch", "web_read",
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
    monkeypatch.setenv("API_KEY", "test-key")
    from core import agent_cache  # noqa: F401  (insère ia/ dans sys.path)
    from api import app as api_app

    headers = {"X-API-Key": "test-key"}
    with TestClient(api_app) as client:
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
            json={"tool": "read_file", "args": {"path": "api.txt"}}, headers=headers,
        )
        assert read.status_code == 200 and "via-api" in read.json()["result"]

        gpu = client.post(
            "/api/agent/tools/run", json={"tool": "gpu_info", "args": {}}, headers=headers,
        )
        assert gpu.status_code == 200
        assert isinstance(gpu.json()["result"]["devices"], list)

        listing = client.post(
            "/api/agent/tools/run",
            json={"tool": "list_dir", "args": {"path": "."}}, headers=headers,
        )
        assert listing.status_code == 200
        names = [e["path"] for e in listing.json()["result"]["entries"]]
        assert "api.txt" in names


# --- Outils recherche contenu / calculatrice / horodatage (ia/tools/search_tools.py,
# --- ia/tools/calc_tools.py) -------------------------------------------------------------

def test_search_in_files_matches_content_with_line_numbers(sandbox_root):
    from tools.file_tools import write_file
    from tools.search_tools import search_in_files

    write_file("src/train.log", "epoch 1 done\nepoch 2 done\nloss=0.3\n")
    write_file("src/readme.md", "aucune correspondance ici\n")

    result = search_in_files(r"epoch \d+ done")
    assert result["scanned_files"] == 2
    assert result["match_count"] == 2
    assert result["matches"][0]["path"] == "src/train.log"
    assert result["matches"][0]["line_number"] == 1
    assert result["matches"][1]["line_number"] == 2


def test_search_in_files_skips_excluded_dirs_and_binaries(sandbox_root):
    from tools.file_tools import write_file
    from tools.search_tools import search_in_files

    # Fixtures dans des dossiers exclus : créées directement (pas via write_file,
    # dont le garde-fou interdit d'écrire sous .git/... — c'est volontaire).
    (sandbox_root / "venv/lib").mkdir(parents=True, exist_ok=True)
    (sandbox_root / "venv/lib/secret.py").write_text("TOKEN = 'abc'", encoding="utf-8")
    (sandbox_root / ".git/hooks").mkdir(parents=True, exist_ok=True)
    (sandbox_root / ".git/hooks/pre-commit").write_text("TOKEN = 'abc'", encoding="utf-8")
    (sandbox_root / "binaire.dat").write_bytes(b"ok\x00binary")
    write_file("normal.txt", "TOKEN = 'abc'")
    result = search_in_files(r"TOKEN")
    assert result["scanned_files"] == 1  # seul normal.txt est balayé
    assert [m["path"] for m in result["matches"]] == ["normal.txt"]


def test_search_in_files_invalid_regex_and_max_results(sandbox_root):
    from tools.file_tools import write_file
    from tools.search_tools import search_in_files

    write_file("multi.txt", "hit\nhit\nhit\n")
    with pytest.raises(ValueError, match="Regex invalide"):
        search_in_files("(non_fermee")

    capped = search_in_files("hit", max_results=2)
    assert capped["match_count"] == 2 and capped["truncated"] is True


def test_tail_file_returns_last_lines_only(sandbox_root):
    from tools.file_tools import write_file
    from tools.search_tools import tail_file

    write_file("long.log", "".join(f"ligne {i}\n" for i in range(1, 201)))
    out = tail_file("long.log", lines=5)
    assert out.splitlines()[-1] == "ligne 200"
    assert len(out.splitlines()) == 5
    assert "ligne 1\n" not in out
    # Fichier plus court que la demande : tout le contenu, sans marqueur.
    write_file("court.log", "a\nb\n")
    assert tail_file("court.log", lines=50) == "a\nb"


def test_append_file_creates_parents_and_concatenates(sandbox_root):
    from tools.file_tools import write_file
    from tools.search_tools import append_file
    from tools.system_tools import read_file

    append_file("logs/app.log", "ligne 1\n")
    append_file("logs/app.log", "ligne 2\n")
    assert read_file("logs/app.log") == "ligne 1\nligne 2\n"
    # write_file écraserait : on vérifie la différence de contrat.
    write_file("logs/app.log", "écrasé")
    assert read_file("logs/app.log") == "écrasé"


def test_now_returns_parseable_iso_timestamp(sandbox_root):
    from datetime import datetime

    from tools.search_tools import now

    stamp = now()
    parsed = datetime.fromisoformat(stamp)
    assert parsed.year >= 2026
    local = now(utc=False)
    datetime.fromisoformat(local)  # ne lève pas


def test_calc_evaluates_arithmetic_safely(sandbox_root):
    from tools.calc_tools import calc

    assert calc("(3 + 4) * 2")["result"] == 14
    assert calc("2 ** 10")["result"] == 1024
    assert calc("17 // 5")["result"] == 3
    assert calc("-1.5 * 8")["result"] == -12
    # Aucun code arbitraire ne doit passer.
    for hostile in (
        "__import__('os').system('dir')",
        "open('secret.txt')",
        "x + 1",
        "'a' * 3",
        "2 ** 99999999",
    ):
        with pytest.raises(ValueError):
            calc(hostile)
    with pytest.raises(ValueError, match="Division par zéro"):
        calc("1 / 0")


# --- Outils métier (ia/tools/ml_tools.py) ------------------------------------------------

@pytest.fixture()
def jobs_db(sandbox_root):
    """Base experiments/jobs.db minimale, même schéma que core/job_store.py."""
    db_path = sandbox_root / "experiments" / "jobs.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    payloads = [
        ("j1", {"job_id": "j1", "status": "completed", "step": "done",
                "started_at": 1.0, "finished_at": 2.0, "error": None,
                "model_path": "experiments/models/v1",
                "request": {"epochs": 2}}, 100.0),
        ("j2", {"job_id": "j2", "status": "running", "step": "training",
                "started_at": 3.0, "finished_at": None, "error": None,
                "model_path": None}, 200.0),
    ]
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE jobs (job_id TEXT PRIMARY KEY, payload TEXT NOT NULL,"
        " updated_at REAL NOT NULL)"
    )
    for job_id, payload, updated_at in payloads:
        conn.execute(
            "INSERT INTO jobs VALUES (?, ?, ?)", (job_id, json.dumps(payload), updated_at)
        )
    conn.commit()
    conn.close()
    return db_path


def test_job_list_orders_desc_and_filters_status(jobs_db):
    from tools.ml_tools import job_list

    listing = job_list()
    assert listing["job_count"] == 2
    assert [job["job_id"] for job in listing["jobs"]] == ["j2", "j1"]  # updated_at desc
    assert listing["truncated"] is False

    completed = job_list(status="completed")
    assert [job["job_id"] for job in completed["jobs"]] == ["j1"]

    capped = job_list(limit=1)
    assert capped["job_count"] == 1 and capped["truncated"] is True


def test_job_list_rejects_unknown_status_and_missing_db(sandbox_root):
    from tools.ml_tools import job_list

    with pytest.raises(ValueError, match="Statut inconnu"):
        job_list(status="en_pause")

    empty = job_list()  # pas de jobs.db dans cette sandbox
    assert empty["job_count"] == 0 and empty["jobs"] == []


def test_job_get_returns_full_payload(jobs_db):
    from tools.ml_tools import job_get

    payload = job_get("j1")
    assert payload["status"] == "completed"
    assert payload["request"] == {"epochs": 2}  # champs non compacts conservés

    with pytest.raises(ValueError, match="Job introuvable"):
        job_get("inconnu")


def test_job_store_connection_is_read_only(jobs_db):
    from tools.ml_tools import _connect_readonly

    conn = _connect_readonly(jobs_db)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO jobs VALUES ('x', '{}', 0)"
            )
    finally:
        conn.close()


class _FakePredictor:
    def predict(self, texts):
        return [
            {"text": text, "sentiment": "positive", "confidence": 0.9} for text in texts
        ]


def test_predict_sentiment_validates_and_delegates(sandbox_root, monkeypatch):
    from tools import ml_tools

    monkeypatch.setattr(ml_tools, "_get_predictor", lambda: _FakePredictor())

    result = ml_tools.predict_sentiment(["super film", "nul"])
    assert result["count"] == 2
    assert result["predictions"][1]["text"] == "nul"
    # Une chaîne seule est acceptée et enveloppée.
    assert ml_tools.predict_sentiment("top")["count"] == 1

    with pytest.raises(ValueError, match="liste non vide"):
        ml_tools.predict_sentiment([])
    too_many = [f"texte {index}" for index in range(21)]
    with pytest.raises(ValueError, match="Maximum 20"):
        ml_tools.predict_sentiment(too_many)
    with pytest.raises(ValueError, match="chaîne non vide"):
        ml_tools.predict_sentiment(["ok", "   "])


def test_dataset_stats_profiles_csv_and_rejects_other_formats(sandbox_root):
    from tools.file_tools import write_file
    from tools.ml_tools import dataset_stats

    csv_content = (
        "text,label,lang_code\n"
        "j'adore,positive,fr\n"
        "j'adore,positive,fr\n"  # doublon volontaire
        "bof,,fr\n"              # label manquant
        "great,positive,en\n"
    )
    write_file("data/train.csv", csv_content)

    stats = dataset_stats("data/train.csv")
    assert stats["row_count"] == 4
    assert stats["label_counts"] == {"positive": 3}  # la valeur manquante est dans « missing »
    assert stats["lang_code_counts"] == {"fr": 3, "en": 1}
    assert stats["duplicate_text_rows"] == 1
    assert stats["missing"]["label"] == 1

    write_file("notes.txt", "pas un dataset")
    with pytest.raises(ValueError, match="Format non supporté"):
        dataset_stats("notes.txt")


def test_model_versions_flags_latest_as_active(sandbox_root):
    from tools.ml_tools import model_versions

    models_root = sandbox_root / "experiments" / "models"
    (models_root / "20260101T000000Z").mkdir(parents=True)
    (models_root / "20260101T000000Z" / "model.safetensors").write_bytes(b"x")
    time.sleep(0.02)  # mtimes distincts pour un tri déterministe
    (models_root / "20260201T000000Z").mkdir(parents=True)
    (models_root / "20260201T000000Z" / "pytorch_model.bin").write_bytes(b"x")
    # Dossier sans poids : ignoré.
    (models_root / "brouillon").mkdir()

    result = model_versions()
    assert result["version_count"] == 2
    names = [v["name"] for v in result["versions"]]
    assert names == ["20260201T000000Z", "20260101T000000Z"]
    assert result["versions"][0]["active"] is True
    assert result["versions"][1]["active"] is False


# --- Outils exploitation (ia/tools/ops_tools.py) ---------------------------------------------

class _FakeStreamResponse:
    def __init__(self, chunks, status_code=200):
        self._chunks = chunks
        self.status_code = status_code

    def iter_content(self, chunk_size):
        yield from self._chunks

    def close(self):
        pass


def test_download_file_streams_into_sandbox(sandbox_root, monkeypatch):
    import requests as requests_module
    from tools.ops_tools import download_file

    monkeypatch.setattr(
        requests_module, "get",
        lambda url, stream=True, timeout=None: _FakeStreamResponse([b"hello ", b"world"]),
    )
    result = download_file("https://example.com/data.bin", "downloads/data.bin", max_mb=1)
    assert result["bytes_written"] == 11
    assert (sandbox_root / "downloads" / "data.bin").read_bytes() == b"hello world"


def test_download_file_http_error_leaves_no_partial(sandbox_root, monkeypatch):
    import requests as requests_module
    from tools.ops_tools import download_file

    monkeypatch.setattr(
        requests_module, "get",
        lambda url, stream=True, timeout=None: _FakeStreamResponse([b"x"], status_code=404),
    )
    with pytest.raises(RuntimeError, match="HTTP 404"):
        download_file("https://example.com/missing.bin", "missing.bin")
    assert not (sandbox_root / "missing.bin").exists()


def test_download_file_oversize_aborts_and_cleans(sandbox_root, monkeypatch):
    import requests as requests_module
    from tools.ops_tools import download_file

    big_chunks = [b"A" * (64 * 1024) for _ in range(20)]  # ~1,25 Mo au total
    monkeypatch.setattr(
        requests_module, "get",
        lambda url, stream=True, timeout=None: _FakeStreamResponse(big_chunks),
    )
    with pytest.raises(ValueError, match="trop volumineux"):
        download_file("https://example.com/big.bin", "big.bin", max_mb=1)
    assert not (sandbox_root / "big.bin").exists()


def test_download_file_rejects_non_http_scheme(sandbox_root):
    from tools.ops_tools import download_file

    with pytest.raises(ValueError, match="http/https"):
        download_file("ftp://example.com/f.bin", "f.bin")


def test_env_info_reports_versions_without_secrets(sandbox_root):
    from tools.ops_tools import env_info

    info = env_info()
    assert info["cpu_count"] >= 1
    assert "torch" in info["packages"]
    assert not any("KEY" in str(key).upper() for key in info)


def test_disk_usage_totals_and_children_sizes(sandbox_root):
    from tools.file_tools import write_file
    from tools.ops_tools import disk_usage

    write_file("gros/donnees.txt", "x" * 1000)
    usage = disk_usage()
    assert usage["free_gb"] > 0 and usage["total_gb"] > 0
    children = {entry["name"]: entry for entry in usage["children"]}
    assert children["gros"]["size_bytes"] == 1000


def test_zip_then_unzip_roundtrip(sandbox_root):
    from tools.file_tools import write_file
    from tools.ops_tools import unzip_file, zip_path
    from tools.system_tools import read_file

    write_file("projet/a/f1.txt", "un")
    write_file("projet/a/sub/f2.txt", "deux")
    # Fixture sous .git créée directement (le garde-fou de write_file interdit
    # volontairement d'écrire sous .git).
    (sandbox_root / ".git").mkdir(parents=True, exist_ok=True)
    (sandbox_root / ".git/interne.txt").write_text("jamais archive", encoding="utf-8")

    archived = zip_path("projet", "sauvegarde.zip")
    assert archived["file_count"] == 2  # .git exclu

    extracted = unzip_file("sauvegarde.zip", "restaure")
    assert extracted["extracted_files"] == 2
    assert read_file("restaure/projet/a/sub/f2.txt") == "deux"


def test_unzip_blocks_zip_slip_entries(sandbox_root):
    import zipfile as zipfile_module
    from tools.ops_tools import unzip_file

    evil = sandbox_root / "evil.zip"
    with zipfile_module.ZipFile(evil, "w") as archive:
        archive.writestr("../evil.txt", "boom")
    with pytest.raises(PermissionError, match="dangereuse"):
        unzip_file("evil.zip", "out")
    assert not (sandbox_root.parent / "evil.txt").exists()

    with zipfile_module.ZipFile(evil, "w") as archive:
        archive.writestr("/absolu.txt", "boom")
    with pytest.raises(PermissionError, match="dangereuse"):
        unzip_file("evil.zip", "out")


def test_git_wrappers_return_structured_results_without_raising(sandbox_root, monkeypatch):
    from tools import ops_tools

    def fake_run_subprocess(argv, **kwargs):
        if argv[2] == "status":
            return 0, "## main\n M fichier.py\n", ""
        return 128, "", "fatal: not a git repository"

    monkeypatch.setattr(ops_tools, "run_subprocess", fake_run_subprocess)

    status = ops_tools.git_status()
    assert status["returncode"] == 0
    assert "main" in status["stdout"]

    log = ops_tools.git_log(limit=5)
    assert log["returncode"] == 128  # pas d'exception : l'agent lit stderr
    assert "not a git repository" in log["stderr"]


def test_git_diff_appends_sandbox_relative_path(sandbox_root, monkeypatch):
    from tools import ops_tools
    from tools.file_tools import write_file

    write_file("src/a.py", "print('ok')\n")
    captured = {}

    def fake_run_subprocess(argv, **kwargs):
        captured["argv"] = argv
        return 0, "", ""

    monkeypatch.setattr(ops_tools, "run_subprocess", fake_run_subprocess)
    ops_tools.git_diff(path="src/a.py", staged=True)
    assert captured["argv"][:3] == ["git", "--no-pager", "diff"]
    assert "--cached" in captured["argv"]
    assert captured["argv"][-1] == "src/a.py"


def test_docker_stats_parses_json_lines(sandbox_root, monkeypatch):
    from tools import ops_tools

    lines = [
        json.dumps({"Name": "api", "CPUPerc": "0.15%", "MemUsage": "50MiB / 1GiB"}),
        json.dumps({"Name": "dashboard", "CPUPerc": "1.20%", "MemUsage": "120MiB / 1GiB"}),
    ]

    def fake_run_subprocess(argv, **kwargs):
        assert argv[1] == "stats" and "--no-stream" in argv
        return 0, "\n".join(lines) + "\n", ""

    monkeypatch.setattr(ops_tools, "run_subprocess", fake_run_subprocess)
    stats = ops_tools.docker_stats()
    assert [conteneur["Name"] for conteneur in stats] == ["api", "dashboard"]

    def failing_run_subprocess(argv, **kwargs):
        return 1, "", "docker daemon down"

    monkeypatch.setattr(ops_tools, "run_subprocess", failing_run_subprocess)
    with pytest.raises(RuntimeError, match="stats"):
        ops_tools.docker_stats()


# --- Cohérence du registre central --------------------------------------------------------------

def test_registry_is_consistent_between_tools_and_required_args():
    from tools.tool_registry import REQUIRED_ARGS, TOOL_META, TOOLS

    assert set(TOOLS) == set(REQUIRED_ARGS)
    for name, func in TOOLS.items():
        assert callable(func), name
    # Quelques nouveaux outils bien enregistrés avec leurs arguments requis.
    assert REQUIRED_ARGS["search_in_files"] == ["pattern"]
    assert REQUIRED_ARGS["predict_sentiment"] == ["texts"]
    assert REQUIRED_ARGS["download_file"] == ["url", "filename"]
    assert REQUIRED_ARGS["job_get"] == ["job_id"]
    assert REQUIRED_ARGS["now"] == []
    assert REQUIRED_ARGS["web_search"] == ["query"]
    assert REQUIRED_ARGS["web_fetch"] == ["url"]
    assert REQUIRED_ARGS["web_read"] == ["url"]
    assert len(TOOLS) >= 35

    # Cohérence du registre déclaratif (tools_config.json) : chaque outil
    # exécutable a une entrée JSON, et REQUIRED_ARGS est exactement celui du JSON.
    assert set(TOOL_META) == set(TOOLS)
    for name in TOOLS:
        meta = TOOL_META[name]
        assert meta["name"] == name
        assert meta["required_args"] == REQUIRED_ARGS[name]


def test_json_manifest_exposes_description_and_parameters():
    from tools.tool_registry import TOOL_META

    # Le JSON est la source déclarative : les champs attendus y figurent.
    assert TOOL_META["calc"]["description"]
    assert TOOL_META["calc"]["parameters"]["expression"]["required"] is True
    assert set(TOOL_META["calc"]["parameters"]) == {"expression"}
    # Un outil avec paramètres optionnels documentés dans le manifeste.
    read_file = TOOL_META["read_file"]
    assert "max_bytes" in read_file["parameters"]
    assert read_file["parameters"]["max_bytes"]["required"] is False


# --- Nouveaux outils fichiers (file_tools) -------------------------------------------------------

def test_write_file_is_atomic_and_guards_git_and_dir(sandbox_root):
    from tools.file_tools import write_file

    msg = write_file("notes/a.txt", "bonjour")
    assert "écrit" in msg and "octets" in msg
    # Écraser un fichier existant -> verbe « écrasé », contenu remplacé.
    msg2 = write_file("notes/a.txt", "nouveau")
    assert "écrasé" in msg2
    assert (sandbox_root / "notes/a.txt").read_text(encoding="utf-8") == "nouveau"
    # Écriture sous .git refusée (garde-fou de sécurité).
    with pytest.raises(PermissionError, match=".git"):
        write_file(".git/x.txt", "ok")
    # Content non-str -> TypeError clair.
    with pytest.raises(TypeError, match="str"):
        write_file("n.txt", 123)


def test_file_info_and_checksum_and_head_and_count(sandbox_root):
    from tools.file_tools import (
        file_checksum,
        file_info,
        head_file,
        count_lines,
        write_file,
    )

    write_file("logs/app.log", "ligne 1\nligne 2\nligne 3\n")
    info = file_info("logs/app.log")
    assert info["type"] == "file" and info["lines"] == 3 and info["size_bytes"] > 0
    assert info["encoding"] == "utf-8"

    checksum = file_checksum("logs/app.log", algo="sha256")
    assert len(checksum["hexdigest"]) == 64 and checksum["algorithm"] == "sha256"
    assert file_checksum("logs/app.log", algo="md5")["hexdigest"]

    head = head_file("logs/app.log", max_lines=2)
    assert head["returned_lines"] == 2 and head["truncated"] is True
    assert head["lines"] == ["ligne 1", "ligne 2"]

    counts = count_lines("logs/app.log")
    assert counts["lines"] == 3 and counts["words"] == 6


def test_touch_and_json_roundtrip(sandbox_root):
    from tools.file_tools import read_json, touch, write_file, write_json

    assert "créé" in touch("empty.txt")
    assert (sandbox_root / "empty.txt").exists()

    write_json("config.json", {"lr": 0.01, "epochs": 3, "tags": ["a"]})
    out = read_json("config.json")
    assert out["data"] == {"lr": 0.01, "epochs": 3, "tags": ["a"]}

    write_file("bad.json", "{ pas du json }")
    with pytest.raises(ValueError, match="JSON invalide"):
        read_json("bad.json")


def test_find_duplicates_and_split_and_dedupe(sandbox_root):
    from tools.file_tools import dedupe_lines, find_duplicates, split_file, write_file
    from tools.system_tools import read_file

    write_file("a/one/f.txt", "A")
    write_file("a/two/f.txt", "A")
    write_file("a/unique.txt", "B")
    dups = find_duplicates("a")
    assert dups["duplicate_groups"] == 1 and dups["duplicate_files"] == 2

    write_file("big.log", "\n".join(f"ligne {i}" for i in range(1, 11)) + "\n")
    res = split_file("big.log", max_lines=4)
    assert res["part_count"] == 3 and len(res["parts"]) == 3

    write_file("dup.txt", "x\ny\nx\nz\n")
    dedup = dedupe_lines("dup.txt", keep="first")
    assert dedup["removed_duplicates"] == 1
    assert read_file("dup.txt") == "x\ny\nz\n"
    assert (sandbox_root / "dup.txt.bak").exists()



# --- Nouveaux outils ML : start_training / train_model ------------------------------------------

class _FakeJobStore(dict):
    """Job store mémoire : mêmes usages (setitem / get) que PersistentJobStore."""


def _patch_ml_training(monkeypatch, runner):
    """Remplace job store et runner d'entraînement par des doublures offline."""
    from tools import ml_tools

    store = _FakeJobStore()
    monkeypatch.setattr(ml_tools, "_get_job_store", lambda: store)
    monkeypatch.setattr(ml_tools, "_get_trainer_runner", lambda: runner)
    return store


def _finish_job(store, job_id, status, model_path=None, error=None):
    from core.models import JobStatus

    job = store.get(job_id)
    job.status = JobStatus(status)
    job.step = "done" if status == "completed" else status
    job.model_path = model_path
    job.error = error
    job.finished_at = time.time()
    store[job_id] = job


def _wait_terminal(store, job_id, deadline_seconds=5.0):
    from tools import ml_tools

    limit = time.time() + deadline_seconds
    while time.time() < limit:
        job = store.get(job_id)
        if job.status.value in ml_tools.TERMINAL_JOB_STATUSES:
            return job
        time.sleep(0.02)
    raise AssertionError("le job n'a pas atteint un statut terminal à temps")


def test_start_training_launches_background_job(sandbox_root, monkeypatch):
    from tools import ml_tools

    seen = {}

    def fake_run_training(job_id, req):
        seen["job_id"], seen["req"] = job_id, req
        time.sleep(0.05)  # laisse l'appelant lire le statut initial
        _finish_job(ml_tools._get_job_store(), job_id, "completed",
                    model_path="experiments/models/abc")

    store = _patch_ml_training(monkeypatch, fake_run_training)

    result = ml_tools.start_training(epochs=1, batch_size=4)

    assert result["status"] in ("pending", "running")
    assert "job_get" in result["message"]
    job = _wait_terminal(store, result["job_id"])
    assert job.status.value == "completed"
    assert seen["job_id"] == result["job_id"]
    assert seen["req"].epochs == 1 and seen["req"].batch_size == 4


def test_start_training_refuses_when_training_already_running(sandbox_root, monkeypatch):
    from core.models import JobStatus, TrainJob
    from tools import ml_tools

    store = _patch_ml_training(monkeypatch, lambda job_id, req: None)
    store["en_cours"] = TrainJob(job_id="en_cours", status=JobStatus.RUNNING)

    with pytest.raises(ValueError, match="déjà en cours"):
        ml_tools.start_training(epochs=1)
    assert list(store) == ["en_cours"]  # aucun nouveau job créé


def test_train_model_waits_until_completion(sandbox_root, monkeypatch):
    from tools import ml_tools

    def fake_run_training(job_id, req):
        time.sleep(0.1)
        _finish_job(ml_tools._get_job_store(), job_id, "completed",
                    model_path="experiments/models/v9")

    store = _patch_ml_training(monkeypatch, fake_run_training)

    result = ml_tools.train_model(wait_timeout=5, epochs=1)

    assert result["status"] == "completed"
    assert result["model_path"] == "experiments/models/v9"
    assert result["timed_out"] is False
    assert "v9" in result["message"]


def test_train_model_reports_failure(sandbox_root, monkeypatch):
    from tools import ml_tools

    def failing_run_training(job_id, req):
        _finish_job(ml_tools._get_job_store(), job_id, "failed",
                    error="CUDA out of memory")

    _patch_ml_training(monkeypatch, failing_run_training)

    result = ml_tools.train_model(wait_timeout=5)

    assert result["status"] == "failed"
    assert result["error"] == "CUDA out of memory"


def test_train_model_times_out_and_returns_tracking_payload(sandbox_root, monkeypatch):
    from tools import ml_tools

    # runner « coincé » : le job reste pending/running bien au-delà du timeout
    _patch_ml_training(monkeypatch, lambda job_id, req: time.sleep(1.0))

    result = ml_tools.train_model(wait_timeout=0.3)

    assert result["timed_out"] is True
    assert result["status"] in ("pending", "running")
    assert "job_get" in result["message"]


def test_train_tools_validate_params_and_corrections_path(sandbox_root, monkeypatch):
    from tools import ml_tools

    _patch_ml_training(monkeypatch, lambda job_id, req: None)

    with pytest.raises(ValueError, match="inconnu"):
        ml_tools.start_training(hyper_inconnu=1)
    with pytest.raises(ValueError, match="Device invalide"):
        ml_tools.train_model(wait_timeout=1, device="tpu")
    with pytest.raises(FileNotFoundError):
        ml_tools.start_training(local_corrections_path="corrections.csv")
    with pytest.raises(ValueError, match="class_augment_weights"):
        ml_tools.start_training(class_augment_weights={"additionalProp1": 1.0})


def test_train_tools_are_registered_with_empty_required_args():
    from tools.tool_registry import REQUIRED_ARGS, TOOLS

    assert REQUIRED_ARGS["start_training"] == []
    assert REQUIRED_ARGS["train_model"] == []
    assert callable(TOOLS["start_training"])
    assert callable(TOOLS["train_model"])



# --- Nouveaux outils ML : cancel_training / stop_training ----------------------------------------

def _patch_ml_canceller(monkeypatch, store):
    """Doublure offline de core.trainer_runner.cancel_training."""
    from tools import ml_tools

    def fake_cancel(job_id):
        _finish_job(store, job_id, "cancelled", error="Training cancelled by user")
        return store.get(job_id)

    monkeypatch.setattr(ml_tools, "_get_training_canceller", lambda: fake_cancel)


def test_cancel_training_stops_running_job(sandbox_root, monkeypatch):
    from core.models import JobStatus, TrainJob
    from tools import ml_tools

    store = _FakeJobStore()
    store["j-run"] = TrainJob(job_id="j-run", status=JobStatus.RUNNING)
    monkeypatch.setattr(ml_tools, "_get_job_store", lambda: store)
    _patch_ml_canceller(monkeypatch, store)

    result = ml_tools.cancel_training("j-run")

    assert result["job_id"] == "j-run"
    assert result["status"] == "cancelled"
    assert "job_get" in result["message"]
    assert store.get("j-run").status.value == "cancelled"


def test_cancel_training_accepts_pending_and_stop_is_alias(sandbox_root, monkeypatch):
    from core.models import JobStatus, TrainJob
    from tools import ml_tools

    store = _FakeJobStore()
    store["j-pending"] = TrainJob(job_id="j-pending", status=JobStatus.PENDING)
    monkeypatch.setattr(ml_tools, "_get_job_store", lambda: store)
    _patch_ml_canceller(monkeypatch, store)

    result = ml_tools.stop_training("j-pending")

    assert result["status"] == "cancelled"  # stop_training == cancel_training


def test_cancel_training_rejects_unknown_and_finished_jobs(sandbox_root, monkeypatch):
    from core.models import JobStatus, TrainJob
    from tools import ml_tools

    store = _FakeJobStore()
    store["j-done"] = TrainJob(job_id="j-done", status=JobStatus.COMPLETED)
    monkeypatch.setattr(ml_tools, "_get_job_store", lambda: store)
    _patch_ml_canceller(monkeypatch, store)

    with pytest.raises(ValueError, match="Job introuvable"):
        ml_tools.cancel_training("inconnu")
    with pytest.raises(ValueError, match="rien \u00e0 annuler"):
        ml_tools.cancel_training("j-done")
    with pytest.raises(ValueError, match="cha\u00eene non vide"):
        ml_tools.cancel_training("   ")


def test_cancel_tools_are_registered_with_job_id_required():
    from tools.tool_registry import REQUIRED_ARGS, TOOLS

    assert REQUIRED_ARGS["cancel_training"] == ["job_id"]
    assert REQUIRED_ARGS["stop_training"] == ["job_id"]
    assert callable(TOOLS["cancel_training"])
    assert callable(TOOLS["stop_training"])
