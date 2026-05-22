"""Shared pytest setup for ndo-run skill tests.

Adds the sibling `scripts/` directory to `sys.path` so test modules can
`import preflight`, `import postflight`, etc., the same way `ndo_run.py`
imports them at runtime.
"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
