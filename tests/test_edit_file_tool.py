"""Tests offline de l'outil edit_file de l'agent IA (ia/tools/file_tools.py).

Couvre le comportement « vibecoding » :
    - remplacement exact unique / multiple / replace_all ;
    - tolérance aux fins de ligne Windows (write_file écrit \n -> \r\n) ;
    - erreurs auto-correctibles (introuvable, ambigu, validations) ;
    - cohérence du registre central (TOOLS / REQUIRED_ARGS).

Lance avec : pytest tests/test_edit_file_tool.py -v
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IA_DIR = os.path.join(PROJECT_ROOT, "ia")
for _p in (PROJECT_ROOT, IA_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import pytest

from tools.file_tools import edit_file, write_file
from tools.system_tools import read_file
from tools.tool_registry import REQUIRED_ARGS as REGISTRY_REQUIRED_ARGS
from tools.tool_registry import TOOLS as REGISTRY_TOOLS


@pytest.fixture()
def sandbox_root(tmp_path, monkeypatch):
    """Redirige la racine de la sandbox vers un dossier temporaire."""
    monkeypatch.setenv("AGENT_SANDBOX_ROOT", str(tmp_path))
    return tmp_path


# --- Remplacements ------------------------------------------------------------

def _normalize(text: str) -> str:
    """Rend une assertion insensible à la convention de fin de ligne : sous
    Windows, write_file traduit \n -> \r\n et read_file le restitue tel quel."""
    return text.replace("\r\n", "\n")


def test_edit_file_roundtrip_single_occurrence(sandbox_root):
    write_file("notes/app.py", "def saluer():\n    return 'bonjour'\n")
    message = edit_file("notes/app.py", "return 'bonjour'", "return 'salut'")
    assert "modifié" in message and "(1 remplacement)" in message
    assert _normalize(read_file("notes/app.py")) == (
        "def saluer():\n    return 'salut'\n"
    )


def test_edit_file_multiline_tolerates_windows_line_endings(sandbox_root):
    # write_file écrit en mode texte : sous Windows les \n deviennent \r\n
    # sur disque. edit_file doit rester utilisable sans que le modèle ait à
    # connaître la convention de fin de ligne réelle du fichier.
    write_file("multi.txt", "ligne un\nligne deux\nligne trois\n")
    message = edit_file("multi.txt", "ligne un\nligne deux", "UN\nDEUX")
    assert "(1 remplacement)" in message
    lines = _normalize(read_file("multi.txt")).splitlines()
    assert lines[0] == "UN" and lines[1] == "DEUX"
    assert "ligne trois" in read_file("multi.txt")


def test_edit_file_multiple_occurrences_require_replace_all(sandbox_root):
    original = "x = alpha\ny = alpha\n"
    write_file("dup.txt", original)
    with pytest.raises(ValueError, match="occurrences.*replace_all"):
        edit_file("dup.txt", "alpha", "beta")
    assert _normalize(read_file("dup.txt")) == original  # inchangé sur erreur

    message = edit_file("dup.txt", "alpha", "beta", replace_all=True)
    assert "(2 remplacements)" in message
    assert _normalize(read_file("dup.txt")) == "x = beta\ny = beta\n"


def test_edit_file_unknown_text_raises_actionable_error(sandbox_root):
    write_file("cible.txt", "contenu initial\n")
    with pytest.raises(ValueError, match="read_file"):
        edit_file("cible.txt", "texte inexistant", "peu importe")
    assert "contenu initial" in read_file("cible.txt")


# --- Validations ----------------------------------------------------------------

def test_edit_file_rejects_empty_or_identical_texts(sandbox_root):
    write_file("ok.txt", "hello")
    with pytest.raises(ValueError, match="NON VIDE"):
        edit_file("ok.txt", "", "x")
    with pytest.raises(ValueError, match="identiques"):
        edit_file("ok.txt", "hello", "hello")


def test_edit_file_missing_directory_or_oversized(sandbox_root):
    with pytest.raises(FileNotFoundError):
        edit_file("absent.txt", "a", "b")

    make_dir_d = sandbox_root / "dossier"
    make_dir_d.mkdir()
    with pytest.raises(IsADirectoryError):
        edit_file("dossier", "a", "b")

    write_file("big.bin", "a" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="volumineux"):
        edit_file("big.bin", "a", "b")


def test_edit_file_stays_inside_sandbox(sandbox_root):
    outside = sandbox_root.parent / "hors_sandbox.txt"
    with pytest.raises(PermissionError, match="hors sandbox"):
        edit_file(str(outside), "a", "b")


# --- Cohérence registre -----------------------------------------------------------

def test_edit_file_is_registered_with_required_args():
    assert REGISTRY_TOOLS["edit_file"].__name__ == "edit_file"
    assert REGISTRY_REQUIRED_ARGS["edit_file"] == ["path", "old_text", "new_text"]