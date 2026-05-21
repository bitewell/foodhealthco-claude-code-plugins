"""Tests for ndo_run.validate_csv_schema."""
from __future__ import annotations

import pytest

from ndo_run import validate_csv_schema


def test_accepts_csv_with_id_column(tmp_csv):
    spec = {"csv_schema": {"id_columns": ["product_id"]}}
    path = tmp_csv(header=["product_id"], rows=[[1], [2]])
    # Should not raise / exit
    validate_csv_schema(path, spec)


def test_accepts_csv_with_alternate_id_column(tmp_csv):
    spec = {"csv_schema": {"id_columns": ["product_id", "id"]}}
    path = tmp_csv(header=["id"], rows=[[1]])
    validate_csv_schema(path, spec)


def test_rejects_missing_required_column(tmp_csv):
    spec = {
        "csv_schema": {
            "required_columns": ["fhs"],
            "id_columns": ["product_id"],
        }
    }
    path = tmp_csv(header=["product_id"], rows=[[1]])
    with pytest.raises(SystemExit) as exc:
        validate_csv_schema(path, spec)
    assert "missing required columns" in str(exc.value)
    assert "fhs" in str(exc.value)


def test_rejects_unrecognized_id_column(tmp_csv):
    spec = {"csv_schema": {"id_columns": ["id"]}}  # only `id` accepted
    path = tmp_csv(header=["product_id"], rows=[[1]])  # but file has `product_id`
    with pytest.raises(SystemExit) as exc:
        validate_csv_schema(path, spec)
    assert "no recognized product id column" in str(exc.value)


def test_rejects_empty_csv(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("")
    spec = {"csv_schema": {"id_columns": ["product_id"]}}
    with pytest.raises(SystemExit) as exc:
        validate_csv_schema(path, spec)
    assert "empty" in str(exc.value)


def test_no_schema_is_a_no_op(tmp_csv):
    spec = {}  # no csv_schema → permissive
    path = tmp_csv(header=["anything"], rows=[["x"]])
    validate_csv_schema(path, spec)  # should not raise


def test_required_columns_alongside_id_columns(tmp_csv):
    spec = {
        "csv_schema": {
            "required_columns": ["fhs", "product_id"],
            "id_columns": ["product_id"],
        }
    }
    # Has both — passes
    path = tmp_csv(header=["product_id", "fhs"], rows=[[1, 42.5]])
    validate_csv_schema(path, spec)
