# -*- coding: utf-8 -*-
"""Audit final : toute référence résiduelle à app.legacy / app/legacy."""
import os
import sys

sys.stdout = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit_report.txt"),
                  "w", encoding="utf-8", buffering=1)

ROOT = r"d:\workspace\ThinkTuning"
SKIP_DIRS = {".git", "_migration", "venv", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "experiments", "data", "thinktuning.egg-info",
             "outputs", ".venv"}

py_hits, other_hits = [], []
for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in files:
        ext = os.path.splitext(f)[1]
        if ext not in (".py", ".md", ".toml", ".json", ".yml", ".yaml", ".cfg", ".txt"):
            continue
        p = os.path.join(dirpath, f)
        rel = os.path.relpath(p, ROOT)
        try:
            txt = open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for i, line in enumerate(txt.splitlines(), 1):
            if "app.legacy" in line or "app/legacy" in line or "app\\legacy" in line:
                (py_hits if ext == ".py" else other_hits).append(f"{rel}:{i}: {line.strip()[:130]}")

print("=== .py (doit être VIDE) ===")
for h in py_hits:
    print("  " + h)
print("=== autres (docs/config — à vérifier) ===")
for h in other_hits:
    print("  " + h)
print(f"\npy={len(py_hits)}  autres={len(other_hits)}")
