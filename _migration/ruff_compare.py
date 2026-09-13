# -*- coding: utf-8 -*-
"""Comparaison ruff : baseline (HEAD) vs courant — avec stderr et total."""
import subprocess
import sys

RUFF = r"d:\workspace\ThinkTuning\backend\venv\Scripts\python.exe"
CUR = r"d:\workspace\ThinkTuning\backend"
BASE = r"d:\workspace\ThinkTuning\_migration\baseline\backend"
sys.stdout = open(r"d:\workspace\ThinkTuning\_migration\ruff_compare2.txt", "w",
                  encoding="utf-8", buffering=1)


def run(cwd: str, label: str) -> None:
    p = subprocess.run([RUFF, "-m", "ruff", "check", "app", "--statistics", "--no-cache"],
                       cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")
    print(f"===== {label} (rc={p.returncode}) =====")
    if p.stderr.strip():
        print("STDERR:", p.stderr[:2000])
    print(p.stdout)


run(CUR, "courant")
run(BASE, "baseline")
