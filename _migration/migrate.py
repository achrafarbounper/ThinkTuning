# -*- coding: utf-8 -*-
"""Migration : requalification de app/legacy/core/* vers les couches cibles.

- 15 stores/services de persistance -> app/infrastructure/persistence/
- 20 services applicatifs (runners/caches/registres) -> app/application/
- 1 module de DTOs partagés (models) -> app/domain/entities/  (exception :
  importé par les ports du domaine, il doit rester sous app/domain)
- suppression de app/legacy (2 __init__.py) et réécriture de tous les
  imports/références dans le repo (py + md).
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys

sys.stdout = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "migrate_report.txt"),
                  "w", encoding="utf-8", buffering=1)

ROOT = r"d:\workspace\ThinkTuning"
BACKEND = os.path.join(ROOT, "backend")
SRC = os.path.join(BACKEND, "app", "legacy", "core")
SKIP_DIRS = {".git", "_migration", "venv", "node_modules", "__pycache__", ".mypy_cache",
             ".pytest_cache", ".ruff_cache", "experiments", "data", "thinktuning.egg-info",
             "outputs", ".venv"}

PERSISTENCE = [
    "agent_settings", "annotation_store", "approval_store", "audit_store", "flow_store",
    "intent_store", "job_store", "mcp_client_store", "model_head_check", "model_versioning",
    "run_store", "secrets_redact", "session_store", "store_crypto", "training_events",
]
APPLICATION = [
    "agent_cache", "classifier_monitoring", "classifier_registry", "cycle_runner",
    "dynamic_batcher", "feature_flags", "inference_executor", "intent_trainer", "job_logs",
    "model_activation", "model_sanity", "model_signing", "model_warmup", "onnx_exporter",
    "pipeline_runner", "prediction_result_cache", "predictor_cache", "scheduler",
    "trainer_runner", "training_gate",
]
DOMAIN = ["models"]

LAYER = {}
for _m in PERSISTENCE:
    LAYER[_m] = ("app.infrastructure.persistence", "app/infrastructure/persistence")
for _m in APPLICATION:
    LAYER[_m] = ("app.application", "app/application")
for _m in DOMAIN:
    LAYER[_m] = ("app.domain.entities", "app/domain/entities")

TARGET_DIRS = {
    "persistence": os.path.join(BACKEND, "app", "infrastructure", "persistence"),
    "application": os.path.join(BACKEND, "app", "application"),
    "domain": os.path.join(BACKEND, "app", "domain", "entities"),
}


def dotted(mod: str) -> str:
    return f"{LAYER[mod][0]}.{mod}"


def slashed(mod: str) -> str:
    return f"{LAYER[mod][1]}/{mod}"


def git(*args: str, cwd: str = BACKEND) -> None:
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def main() -> None:
    modules = PERSISTENCE + APPLICATION + DOMAIN
    assert len(modules) == 36, f"attendu 36 modules, trouvé {len(modules)}"

    # --- Étape 1 : git mv (historique préservé) -------------------------------
    print("== Étape 1 : git mv ==")
    for mod in modules:
        layer = "domain" if mod in DOMAIN else ("persistence" if mod in PERSISTENCE else "application")
        dest_dir = TARGET_DIRS[layer]
        assert os.path.isdir(dest_dir), f"dossier cible manquant : {dest_dir}"
        src_rel = os.path.join("app", "legacy", "core", f"{mod}.py")
        dst_rel = os.path.relpath(dest_dir, BACKEND)
        src_abs = os.path.join(SRC, f"{mod}.py")
        dst_abs = os.path.join(dest_dir, f"{mod}.py")
        if os.path.isfile(dst_abs) and not os.path.isfile(src_abs):
            print(f"  skip {mod}.py (déjà déplacé)")
            continue
        git("mv", src_rel.replace(os.sep, "/"), f"{dst_rel.replace(os.sep, '/')}/{mod}.py")
        print(f"  moved {mod}.py -> {layer}")

    # --- Étape 2 : suppression du paquet app/legacy ---------------------------
    print("== Étape 2 : suppression app/legacy ==")
    legacy_root = os.path.join(BACKEND, "app", "legacy")
    if os.path.isdir(legacy_root):
        git("rm", "-q", "app/legacy/core/__init__.py", "app/legacy/__init__.py")
        # Nettoyage du résidu non tracké (__pycache__).
        shutil.rmtree(legacy_root, ignore_errors=True)
        print("  app/legacy supprimé (git rm + rmtree __pycache__)")
    else:
        print("  app/legacy déjà supprimé")

    # --- Étape 3 : réécriture des imports / références ------------------------
    print("== Étape 3 : réécriture des références ==")
    pkg_re_paren = re.compile(r"(?P<indent>[ \t]*)from app\.legacy\.core import \((?P<body>[^)]*)\)",
                              flags=re.DOTALL)
    pkg_re_line = re.compile(r"(?P<indent>[ \t]*)from app\.legacy\.core import (?P<body>[^(\n]+)")
    dotted_re = re.compile(r"\bapp\.legacy\.core\.(\w+)\b")
    slash_re = re.compile(r"app/legacy/core/(\w+)")

    stats = {"files": 0, "pkg": 0, "dotted": 0, "slash": 0}

    def rewrite(text: str, fname: str) -> str:
        def pkg_sub(m: "re.Match[str]") -> str:
            indent = m.group("indent")
            body = m.group("body")
            raw_targets = body.replace("\n", " ").split(",")
            targets: list[str] = []
            for chunk in raw_targets:
                if chunk.strip():
                    targets.append(chunk.strip())
            groups: dict[str, list[str]] = {}
            for t in targets:
                parts = t.split(" as ")
                name = parts[0].strip()
                if name not in LAYER:
                    # Docstring / pseudo-code (« ... ») : ligne laissée telle
                    # quelle — ressortira dans les leftovers à traiter à la main.
                    return m.group(0)
                alias = f" as {parts[1].strip()}" if len(parts) > 1 else ""
                groups.setdefault(LAYER[name][0], []).append(name + alias)
            stats["pkg"] += 1
            return "\n".join(f"{indent}from {pkg} import {', '.join(syms)}"
                             for pkg, syms in groups.items())

        text = pkg_re_paren.sub(pkg_sub, text)
        text = pkg_re_line.sub(pkg_sub, text)
        text, n = dotted_re.subn(lambda m: dotted(m.group(1)) if m.group(1) in LAYER else m.group(0), text)
        stats["dotted"] += n
        text, n = slash_re.subn(lambda m: slashed(m.group(1)) if m.group(1) in LAYER else m.group(0), text)
        stats["slash"] += n
        return text

    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith((".py", ".md")):
                continue
            p = os.path.join(dirpath, f)
            raw = open(p, encoding="utf-8", errors="surrogateescape").read()
            new = rewrite(raw, os.path.relpath(p, ROOT))
            if new != raw:
                open(p, "w", encoding="utf-8", errors="surrogateescape", newline="").write(new)
                stats["files"] += 1

    print(f"  fichiers modifiés : {stats['files']}")
    print(f"  imports paquet réécrits : {stats['pkg']}")
    print(f"  refs pointées réécrites : {stats['dotted']}")
    print(f"  refs slash réécrites    : {stats['slash']}")

    # --- Étape 4 : reste à traiter manuellement -------------------------------
    print("== Étape 4 : occurrences 'app.legacy' / 'app/legacy' restantes ==")
    leftovers = []
    for dirpath, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        for f in files:
            if not f.endswith((".py", ".md", ".toml", ".yml", ".yaml", ".cfg", ".json", ".txt")):
                continue
            p = os.path.join(dirpath, f)
            try:
                txt = open(p, encoding="utf-8", errors="ignore").read()
            except Exception:
                continue
            for i, line in enumerate(txt.splitlines(), 1):
                if "app.legacy" in line or "app/legacy" in line or "app\\legacy" in line:
                    leftovers.append(f"{os.path.relpath(p, ROOT)}:{i}: {line.strip()[:120]}")
    for line_ref in leftovers:
        print("  " + line_ref)
    print(f"  => {len(leftovers)} occurrences restantes")


if __name__ == "__main__":
    main()

