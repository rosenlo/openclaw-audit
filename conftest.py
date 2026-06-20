"""Pytest bootstrap: ensure the repository root is importable.

The audit logic lives in the ``openclaw_audit`` package at the repo root.
Adding the rootdir to ``sys.path`` here lets the tests do plain
``import openclaw_audit`` instead of the previous importlib path-loading
trick that was needed when everything lived in ``openclaw-audit.py``
(a file name Python cannot import because of the hyphen).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
