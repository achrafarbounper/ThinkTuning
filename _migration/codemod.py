# -*- coding: utf-8 -*-
"""Codemod lint résiduel : B904 (AST) + E501 (tokenize, prose seulement)."""
import ast
import io
import re
import sys

sys.stdout = open(r"d:\workspace\ThinkTuning\_migration\codemod_report.txt", "w",
                  encoding="utf-8", buffering=1)
ROOT = r"d:\workspace\ThinkTuning\backend"

B904_FILES = {
    "app\\application\\agent_cache.py",
    "app\\infrastructure\\persistence\\annotation_store.py",
    "app\\infrastructure\\persistence\\model_versioning.py",
}

# (fichier, ligne) depuis lint_detail.txt
import os

E501 = {}
detail = open(r"d:\workspace\ThinkTuning\_migration\lint_detail.txt", encoding="utf-8").read()
for m in re.finditer(r"^(.+?):(\d+):\d+: (E501|B904|E402)", detail, flags=re.MULTILINE):
    f, ln, code = m.group(1), int(m.group(2)), m.group(3)
    if code == "E501":
        E501.setdefault(os.path.join(ROOT, f), []).append(ln)


class V(ast.NodeVisitor):
    def __init__(self):
        self.handler = None
        self.raises = []

    def visit_ExceptHandler(self, node):
        prev = self.handler
        self.handler = node.name
        for st in node.body:
            self.visit(st)
        self.handler = prev

    def visit_Try(self, node):
        for st in node.body:
            self.visit(st)
        for h in node.handlers:
            self.visit(h)
        for st in node.orelse:
            self.visit(st)
        for st in node.finalbody:
            self.visit(st)

    def visit_Raise(self, node):
        if node.cause is None and self.handler and node.end_lineno and node.end_col_offset:
            self.raises.append((node.end_lineno, node.end_col_offset, self.handler))


def fix_b904(path: str) -> int:
    src = io.open(path, encoding="utf-8").read()
    tree = ast.parse(src)
    v = V()
    v.visit(tree)
    lines = src.splitlines(keepends=True)
    for end, ecol, hname in sorted(set(v.raises), reverse=True):
        line = lines[end - 1]
        stripped = line.rstrip("\r\n")
        eol = line[len(stripped):]
        suffix = f" from {hname}" if hname else " from None"
        if stripped[:ecol].endswith(("None", "exc")):
            continue
        lines[end - 1] = stripped[:ecol] + suffix + eol
    io.open(path, "w", encoding="utf-8", newline="").write("".join(lines))
    return len(set(v.raises))


def wrap(text, indent, first=True):
    words = text.split()
    out, cur = [], ""
    for w in words:
        cand = (cur + " " + w) if cur else w
        if len(indent + cand) > 99 and cur:
            out.append(cur)
            cur = w
        else:
            cur = cand
    if cur:
        out.append(cur)
    return "\n".join(indent + l for l in out)


def fix_e501(path, flagged):
    import tokenize as tk
    with io.open(path, "r", encoding="utf-8", newline="") as fh:
        toks = list(tk.generate_tokens(fh.readline))
    src = io.open(path, encoding="utf-8").read()
    lines = src.splitlines(keepends=True)
    fixed, manual = 0, []
    for ln in sorted(set(flagged), reverse=True):
        line = lines[ln - 1]
        stripped = line.rstrip("\r\n")
        eol = line[len(stripped):]
        indent = stripped[:len(stripped) - len(stripped.lstrip())]
        content = stripped.lstrip()
        in_str = any(t.start[0] <= ln <= t.end[0] and t.start[0] < ln < t.end[0]
                     for t in toks if t.type == tk.STRING)
        is_comment = any(t.start[0] == ln for t in toks if t.type == tk.COMMENT)
        if in_str:
            lines[ln - 1] = wrap(content, indent) + eol
            fixed += 1
        elif is_comment:
            wrapped = wrap("# " + content.lstrip("# ").strip(), indent)
            lines[ln - 1] = wrapped + eol
            fixed += 1
        else:
            manual.append(f"{path}:{ln}: {stripped[:120]}")
    io.open(path, "w", encoding="utf-8", newline="").write("".join(lines))
    return fixed, manual


total = 0
for rel in sorted(B904_FILES):
    n = fix_b904(os.path.join(ROOT, rel))
    total += n
    print(f"B904 fixed {n}: {rel}")
print(f"B904 total: {total}\n")

manual_all = []
for path, lns in sorted(E501.items()):
    # les B904 viennent d'ajouter des suffixes : les lignes E501 des mêmes
    # fichiers peuvent avoir bougé ? Non : suffixes en fin de ligne, mêmes n°.
    f, manual = fix_e501(path, lns)
    print(f"E501 fixed {f}: {os.path.relpath(path, ROOT)}")
    manual_all.extend(manual)
print("\nMANUEL:")
for m in manual_all:
    print("  " + m)
