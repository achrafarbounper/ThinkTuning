# -*- coding: utf-8 -*-
"""Liste détaillée du lint résiduel (ruff check app) — full + tri."""
import subprocess
import sys

RUFF = r"d:\workspace\ThinkTuning\backend\venv\Scripts\python.exe"
CUR = r"d:\workspace\ThinkTuning\backend"
sys.stdout = open(r"d:\workspace\ThinkTuning\_migration\lint_detail.txt", "w",
                  encoding="utf-8", buffering=1)
p = subprocess.run([RUFF, "-m", "ruff", "check", "app", "--output-format", "concise",
                    "--no-cache"],
                   cwd=CUR, capture_output=True, text=True, encoding="utf-8",
                   errors="replace")
print(p.stdout)
if p.stderr.strip():
    print("STDERR:", p.stderr[:2000])
print("rc =", p.returncode)
