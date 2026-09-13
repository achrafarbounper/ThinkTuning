# -*- coding: utf-8 -*-
"""Scan temporaire (migration app.legacy) — à supprimer après usage."""
from __future__ import annotations

import os
import re
import sys

# Rapport écrit directement en UTF-8 (contournement des pipes PowerShell).
sys.stdout = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "scan_report.txt"),
                  "w", encoding="utf-8", buffering=1)

ROOT = r"d:\workspace\ThinkTuning"
LEGACY = os.path.join(ROOT, "backend", "app", "legacy", "core")
SKIP_DIRS = {".git", "node_modules", "venv", ".venv", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "experiments", "data", "thinktuning.egg-info"}

print("=" * 80)
print("PART 1 — legacy modules: size + first docstring + top-level imports")
print("=" * 80)
for fname in sorted(os.listdir(LEGACY)):
    if not fname.endswith(".py"):
        continue
    path = os.path.join(LEGACY, fname)
    with open(path, encoding="utf-8", errors="replace") as fh:
        text = fh.read()
    lines = text.splitlines()
    head = " ".join(line.strip() for line in lines[:12])[:260]
    size = len(lines)
    imports = re.findall(r"^(?:from|import) .+$", text, flags=re.MULTILINE)
    top_imports = [i for i in imports if not i.startswith(("from .", "from app", "import app"))][:8]
    internal = [i for i in imports if i.startswith(("from .", "from app.legacy", "import app.legacy"))]
    print(f"\n### {fname} ({size} lines)")
    print(f"    DOC: {head}")
    if internal:
        print(f"    INTERNAL/LEGACY IMPORTS:")
        for i in internal[:10]:
            print(f"      {i}")
    if top_imports:
        print(f"    EXTERNAL IMPORTS (sample): {top_imports}")

print()
print("=" * 80)
print("PART 2 — all files referencing 'legacy' (code refs)")
print("=" * 80)
counts = {}
forms = {}
pat = re.compile(r"app[./\\]legacy(?:[./\\]core)?(?:[./\\]\w+)?")
for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in files:
        p = os.path.join(dirpath, f)
        ext = os.path.splitext(f)[1]
        if ext not in (".py", ".ts", ".tsx", ".md", ".yml", ".yaml", ".toml", ".cfg", ".txt", ".json"):
            continue
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        n = txt.count("app.legacy") + txt.count("app/legacy") + txt.count("app\\legacy")
        if n:
            rel = os.path.relpath(p, ROOT)
            counts[rel] = n
            for m in pat.findall(txt):
                forms[m] = forms.get(m, 0) + 1

print(f"{len(counts)} files referencing legacy\n")
py_files = 0
for k, v in sorted(counts.items(), key=lambda x: -x[1]):
    is_py = k.endswith(".py")
    if is_py:
        py_files += 1
    print(f"{v:4d} {'PY ' if is_py else '   '} {k}")
print(f"\n=> {py_files} .py files, {len(counts) - py_files} non-py files")

print()
print("=" * 80)
print("PART 3 — distinct legacy reference forms (frequency)")
print("=" * 80)
for k, v in sorted(forms.items(), key=lambda x: -x[1]):
    print(f"{v:5d}  {k}")

print()
print("=" * 80)
print("PART 4 — py files with 'from app.legacy' import statements (module -> symbols)")
print("=" * 80)
imp_pat = re.compile(r"from app\.legacy\.core\.(\w+) import ([^(\n]+)", flags=re.MULTILINE)
multi_pat = re.compile(r"from app\.legacy\.core\.(\w+) import \(([^)]+)\)", flags=re.DOTALL)
pkg_pat = re.compile(r"from app\.legacy(\.core)? import [^\n(]+", flags=re.MULTILINE)
symbols = {}
for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in files:
        if not f.endswith(".py"):
            continue
        p = os.path.join(dirpath, f)
        if os.sep + "app" + os.sep + "legacy" + os.sep in p:
            continue
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in multi_pat.finditer(txt):
            mod, body = m.group(1), m.group(2)
            syms = [s.strip().rstrip(",") for s in body.replace("\n", " ").split(",") if s.strip()]
            symbols.setdefault(mod, set()).update(syms)
        for m in imp_pat.finditer(txt):
            syms = [s.strip().rstrip(",") for s in m.group(2).replace("\\", " ").replace("(", "").split(",") if s.strip()]
            symbols.setdefault(m.group(1), set()).update(syms)
for mod in sorted(symbols):
    print(f"  {mod}: {sorted(symbols[mod])}")
print(f"\nPackage-level imports (from app.legacy[.core] import ...):")
for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in files:
        if not f.endswith(".py"):
            continue
        p = os.path.join(dirpath, f)
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for m in pkg_pat.finditer(txt):
            rel = os.path.relpath(p, ROOT)
            print(f"  {rel}: {m.group(0)[:90]}")
