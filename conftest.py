"""Pytest bootstrap for ThinkTuning.

Ensures the project root is importable no matter HOW pytest is launched
(`pytest.exe`, `python -m pytest`, VS Code Test Explorer, ...) or from
which working directory. Without this, top-level imports used across the
codebase (`api`, `core`, `src.*`) only resolve when the CWD happens to be
the repository root.
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)
