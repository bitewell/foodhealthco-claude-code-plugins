"""Tests for ndo_run.resolve_input — the function that decides what to upload
and produces the Spaces key passed to manage.py."""
from __future__ import annotations

import pytest

from ndo_run import resolve_input


# ---------------------------------------------------------------------------
# source / vendor / none modes: no upload regardless of --ids/--csv
# ---------------------------------------------------------------------------


def test_source_mode_returns_none(make_args):
    args = make_args(source="nielsen")
    spaces_key, count = resolve_input(args, {"input": "source"})
    assert spaces_key is None and count is None


def test_vendor_mode_returns_none(make_args):
    args = make_args(vendor="kroger")
    spaces_key, count = resolve_input(args, {"input": "vendor"})
    assert spaces_key is None and count is None


def test_no_input_mode_returns_none(make_args):
    args = make_args()
    spaces_key, count = resolve_input(args, {"input": "none"})
    assert spaces_key is None and count is None


# ---------------------------------------------------------------------------
# file mode: requires --ids / --csv / --spaces-key
# ---------------------------------------------------------------------------


def test_file_mode_requires_input(make_args, spec_with_id_schema):
    args = make_args(ids=None, csv=None, spaces_key=None)
    spec = {**spec_with_id_schema, "input": "file"}
    with pytest.raises(SystemExit) as exc:
        resolve_input(args, spec)
    assert "requires a file input" in str(exc.value)


def test_spaces_key_skips_upload(make_args, spec_with_id_schema):
    args = make_args(spaces_key="ops-skill/existing.csv")
    spec = {**spec_with_id_schema, "input": "file"}
    spaces_key, count = resolve_input(args, spec)
    assert spaces_key == "ops-skill/existing.csv"
    # row count unknown for caller-provided key
    assert count is None


def test_ids_dry_run_uses_placeholder_key(make_args, spec_with_id_schema):
    args = make_args(ids="1,2,3", dry_run=True)
    spec = {**spec_with_id_schema, "input": "file"}
    spaces_key, count = resolve_input(args, spec)
    assert spaces_key == "ops-skill/DRYRUN-backfill_tags.csv"
    assert count == 3


def test_csv_dry_run_validates_and_returns_placeholder(make_args, tmp_csv, spec_with_id_schema):
    csv_path = tmp_csv(header=["product_id"], rows=[[1], [2], [3]])
    args = make_args(csv=str(csv_path), dry_run=True)
    spec = {**spec_with_id_schema, "input": "file"}
    spaces_key, count = resolve_input(args, spec)
    assert spaces_key == "ops-skill/DRYRUN-backfill_tags.csv"
    assert count == 3


# ---------------------------------------------------------------------------
# file_or_source mode: accepts either path
# ---------------------------------------------------------------------------


def test_file_or_source_needs_one(make_args, spec_with_id_schema):
    args = make_args()
    spec = {**spec_with_id_schema, "input": "file_or_source"}
    with pytest.raises(SystemExit) as exc:
        resolve_input(args, spec)
    assert "requires --source OR a file input" in str(exc.value)


def test_file_or_source_with_source(make_args, spec_with_id_schema):
    args = make_args(source="nielsen")
    spec = {**spec_with_id_schema, "input": "file_or_source"}
    spaces_key, count = resolve_input(args, spec)
    assert spaces_key is None  # source path — no upload


# ---------------------------------------------------------------------------
# Negative: conflicting inputs
# ---------------------------------------------------------------------------


def test_rejects_ids_and_csv(make_args, tmp_csv, spec_with_id_schema):
    csv_path = tmp_csv(header=["product_id"], rows=[[1]])
    args = make_args(ids="1,2", csv=str(csv_path))
    spec = {**spec_with_id_schema, "input": "file"}
    with pytest.raises(SystemExit) as exc:
        resolve_input(args, spec)
    assert "pass only one of" in str(exc.value)


def test_rejects_ids_for_schema_requiring_extra_columns(make_args, spec_with_required_cols):
    """--ids only writes `product_id`. approve_scores needs `fhs` too — bug fix from this session."""
    args = make_args(ids="1,2,3", dry_run=True)
    with pytest.raises(SystemExit) as exc:
        resolve_input(args, spec_with_required_cols)
    assert "missing required columns" in str(exc.value)
    assert "fhs" in str(exc.value)


def test_rejects_csv_missing_required(make_args, tmp_csv, spec_with_required_cols):
    # Has product_id but missing fhs
    csv_path = tmp_csv(header=["product_id"], rows=[[1]])
    args = make_args(csv=str(csv_path))
    with pytest.raises(SystemExit) as exc:
        resolve_input(args, spec_with_required_cols)
    assert "missing required columns" in str(exc.value)


def test_rejects_nonexistent_csv_path(make_args, spec_with_id_schema):
    args = make_args(csv="/tmp/__does_not_exist__.csv")
    spec = {**spec_with_id_schema, "input": "file"}
    with pytest.raises(SystemExit) as exc:
        resolve_input(args, spec)
    assert "does not exist" in str(exc.value)
