# -*- coding: utf-8 -*-
"""Corrige la ligne mypy exclude du pyproject.toml (backslashes TOML)
et la ligne 38 de l'arbre ARCHITECTURE.md (par numéro de ligne)."""
import io

# --- pyproject.toml --------------------------------------------------------
path = r"d:\workspace\ThinkTuning\backend\pyproject.toml"
with io.open(path, encoding="utf-8") as fh:
    lines = fh.readlines()

out = []
for line in lines:
    if line.strip().startswith("exclude = [") and "legacy" in line:
        line = 'exclude = ["venv", ".agent_tmp", "experiments", "outputs", "src", "ia", "app/api"]\n'
    out.append(line)

with io.open(path, "w", encoding="utf-8", newline="") as fh:
    fh.writelines(out)
print("pyproject.toml mypy exclude corrigé")

with io.open(path, encoding="utf-8") as fh:
    txt = fh.read()
for i, l in enumerate(txt.splitlines(), 1):
    if "legacy" in l:
        print(f"RESTE {i}: {l}")
print("pyproject: OK" if "legacy" not in txt else "pyproject: !! reste des mentions legacy")

# --- ARCHITECTURE.md ligne 38 ----------------------------------------------
apath = r"d:\workspace\ThinkTuning\ARCHITECTURE.md"
with io.open(apath, encoding="utf-8") as fh:
    alines = fh.readlines()
print("repr ligne 38:", repr(alines[37]))
if "legacy/core" in alines[37]:
    alines[37] = alines[37].replace("legacy (ia/, legacy/core)", "legacy (ia/, src/)")
    with io.open(apath, "w", encoding="utf-8", newline="") as fh:
        fh.writelines(alines)
    print("ARCHITECTURE.md ligne 38 corrigée")
else:
    print("ligne 38 déjà propre")
