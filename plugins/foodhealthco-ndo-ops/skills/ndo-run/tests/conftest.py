"""Pytest config + shared fixtures for ndo-run skill tests.

These tests are tier-1 only: pure-function tests of the runner's logic with
no DB or network access. They exercise:
  * resolve_input (CSV building, schema validation, --ids/--csv/--spaces-key)
  * extract_ids_for_preflight + extract_id_aux_pairs_for_preflight
  * validate_csv_schema
  * catalog.yaml integrity

Run with:
    pytest plugins/foodhealthco-ndo-ops/skills/ndo-run/tests/
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import pytest

# Make the scripts package importable without installation
TESTS_DIR = Path(__file__).resolve().parent
SKILL_DIR = TESTS_DIR.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))


@pytest.fixture
def make_args():
    """Factory for argparse-namespace-like objects used by resolve_input/preflight.

    Pass any subset; defaults reflect a minimal valid invocation.
    """

    def _make(**overrides):
        defaults = {
            "command": "backfill_tags",
            "ids": None,
            "csv": None,
            "spaces_key": None,
            "source": None,
            "vendor": None,
            "target": "dev",
            "db": "ndo",
            "sync": True,
            "dry_run": True,  # tests use dry-run to avoid Spaces uploads
            "force": False,
            "summary_out": None,
            "no_preflight": True,  # tests don't hit DB
            "no_postflight": True,
            "extra": [],
        }
        defaults.update(overrides)
        return SimpleNamespace(**defaults)

    return _make


@pytest.fixture
def spec_with_id_schema():
    """A catalog spec for a command that takes an id-only CSV (like backfill_tags)."""
    return {
        "input": "file_or_source",
        "csv_schema": {
            "id_columns": [
                "product_id", "id", "product_match_id",
                "gtin", "upc", "int_upc", "int_gtin",
            ],
        },
    }


@pytest.fixture
def spec_with_required_cols():
    """A catalog spec for a command that requires extra columns (like approve_scores)."""
    return {
        "input": "file",
        "csv_schema": {
            "required_columns": ["fhs", "product_id"],
            "id_columns": ["product_id"],
        },
    }


@pytest.fixture
def tmp_csv(tmp_path):
    """Factory for a CSV with given header + rows in a tmp file."""

    def _make(header: list[str], rows: list[list], name: str = "input.csv") -> Path:
        import csv

        path = tmp_path / name
        with open(path, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow(header)
            for r in rows:
                w.writerow(r)
        return path

    return _make
