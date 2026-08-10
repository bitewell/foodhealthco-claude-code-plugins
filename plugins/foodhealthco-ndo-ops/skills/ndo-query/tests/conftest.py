"""Shared pytest setup for ndo-query skill tests.

Adds the sibling `scripts/` directory to `sys.path` so test modules can
`import ndo_query` the same way the runner is executed directly.
"""
import sys
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))
