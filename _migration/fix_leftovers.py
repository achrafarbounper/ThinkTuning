# -*- coding: utf-8 -*-
"""Fini les faux-positifs : imports `from app.legacy.core import agent_cache`
suivis d'un commentaire `# noqa ...` (le commentaire cassait le parseur)."""
from __future__ import annotations

import os
import re
import sys

sys.stdout = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fix_report.txt"),
                  "w", encoding="utf-8", buffering=1)

ROOT = r"d:\workspace\ThinkTuning"
SKIP_DIRS = {".git", "_migration", "venv", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "experiments", "data", "thinktuning.egg-info",
             "outputs", ".venv"}

# Forme exacte restante : `from app.legacy.core import <mod>` avec commentaire.
pat = re.compile(r"from app\.legacy\.core import (\w+)")

for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in files:
        if not f.endswith(".py"):
            continue
        p = os.path.join(dirpath, f)
        raw = open(p, encoding="utf-8", errors="surrogateescape").read()

        def sub(m: "re.Match[str]") -> str:
            mod = m.group(1)
            if mod in ("agent_settings", "annotation_store", "approval_store", "audit_store",
                       "flow_store", "intent_store", "job_store", "mcp_client_store",
                       "model_head_check", "model_versioning", "run_store", "secrets_redact",
                       "session_store", "store_crypto", "training_events"):
                return f"from app.infrastructure.persistence import {mod}"
            return f"from app.application import {mod}"

        new = pat.sub(sub, raw)
        if new != raw:
            open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(new)
            print(f"fixed: {os.path.relpath(p, ROOT)}")

# openapi.json : descriptions générées mentionnant le chemin legacy.
oapi = os.path.join(ROOT, "backend", "openapi.json")
raw = open(oapi, encoding="utf-8").read()
new = raw.replace("app/legacy/core/session_store", "app/infrastructure/persistence/session_store")
if new != raw:
    open(oapi, "w", encoding="utf-8", newline="").write(new)
    print("fixed: backend/openapi.json (2 descriptions)")

# Rapport final des restes.
print("\n--- restes ---")
for dirpath, dirs, files in os.walk(ROOT):
    dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
    for f in files:
        if not f.endswith((".py", ".md", ".toml", ".json", ".yml", ".yaml")):
            continue
        p = os.path.join(dirpath, f)
        txt = open(p, encoding="utf-8", errors="ignore").read()
        for i, line in enumerate(txt.splitlines(), 1):
            if "app.legacy" in line or "app/legacy" in line:
                print(f"{os.path.relpath(p, ROOT)}:{i}: {line.strip()[:120]}")
