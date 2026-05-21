"""Tests for the preflight extractors that read IDs from CLI args / CSV
without touching DO Spaces or the DB."""
from __future__ import annotations

import pytest

from preflight import (
    extract_id_aux_pairs_for_preflight,
    extract_ids_for_preflight,
)


# ---------------------------------------------------------------------------
# extract_ids_for_preflight
# ---------------------------------------------------------------------------


class TestExtractIds:
    def test_source_mode_returns_none(self, make_args):
        args = make_args(source="nielsen")
        ids, reason = extract_ids_for_preflight(args, {"input": "source"})
        assert ids is None
        assert "doesn't use per-product input" in reason

    def test_vendor_mode_returns_none(self, make_args):
        args = make_args(vendor="kroger")
        ids, reason = extract_ids_for_preflight(args, {"input": "vendor"})
        assert ids is None

    def test_spaces_key_returns_none(self, make_args):
        args = make_args(spaces_key="ops-skill/foo.csv")
        ids, reason = extract_ids_for_preflight(args, {"input": "file"})
        assert ids is None
        assert "--spaces-key" in reason

    def test_ids_flag_parses_to_ints(self, make_args):
        args = make_args(ids=" 1, 2 ,3 ,, ")
        ids, reason = extract_ids_for_preflight(args, {"input": "file"})
        assert ids == [1, 2, 3]
        assert reason is None

    def test_ids_flag_rejects_non_integer(self, make_args):
        args = make_args(ids="1,foo,3")
        ids, reason = extract_ids_for_preflight(args, {"input": "file"})
        assert ids is None
        assert "non-integer" in reason

    def test_csv_with_product_id_column(self, make_args, tmp_csv):
        path = tmp_csv(header=["product_id"], rows=[[1], [2], [3]])
        args = make_args(csv=str(path))
        ids, reason = extract_ids_for_preflight(args, {"input": "file"})
        assert ids == [1, 2, 3]

    def test_csv_with_id_column_alias(self, make_args, tmp_csv):
        path = tmp_csv(header=["id"], rows=[[7], [8]])
        args = make_args(csv=str(path))
        spec = {"csv_schema": {"id_columns": ["product_id", "id"]}}
        ids, reason = extract_ids_for_preflight(args, spec)
        assert ids == [7, 8]

    def test_csv_skips_blank_rows(self, make_args, tmp_csv):
        path = tmp_csv(header=["product_id"], rows=[[1], [""], [2]])
        args = make_args(csv=str(path))
        ids, reason = extract_ids_for_preflight(args, {"input": "file"})
        assert ids == [1, 2]

    def test_csv_strips_thousands_separator(self, make_args, tmp_csv):
        """Some sheets export `1,234,567` style ids."""
        path = tmp_csv(header=["product_id"], rows=[["1,000"], ["2,500"]])
        args = make_args(csv=str(path))
        ids, reason = extract_ids_for_preflight(args, {"input": "file"})
        assert ids == [1000, 2500]

    def test_csv_rejects_non_integer_ids(self, make_args, tmp_csv):
        # gtin/upc strings can be non-integer (e.g. leading zeros stripped to "00077900003660")
        # but truly non-numeric values are rejected
        path = tmp_csv(header=["product_id"], rows=[["abc"]])
        args = make_args(csv=str(path))
        ids, reason = extract_ids_for_preflight(args, {"input": "file"})
        assert ids is None
        assert "non-integer" in reason

    def test_no_input_returns_none(self, make_args):
        args = make_args()
        ids, reason = extract_ids_for_preflight(args, {"input": "file"})
        assert ids is None


# ---------------------------------------------------------------------------
# extract_id_aux_pairs_for_preflight (for approve_scores etc.)
# ---------------------------------------------------------------------------


class TestExtractIdAuxPairs:
    def test_ids_flag_rejected(self, make_args):
        """--ids can't carry aux columns; must use --csv with all cols."""
        args = make_args(ids="1,2")
        pairs, reason = extract_id_aux_pairs_for_preflight(args, {}, ["fhs"])
        assert pairs is None
        assert "--ids cannot carry aux columns" in reason

    def test_csv_with_product_id_and_fhs(self, make_args, tmp_csv):
        path = tmp_csv(header=["product_id", "fhs"], rows=[[1, 42.5], [2, 50.0]])
        args = make_args(csv=str(path))
        pairs, reason = extract_id_aux_pairs_for_preflight(args, {}, ["fhs"])
        assert reason is None
        assert pairs == [(1, "42.5"), (2, "50.0")]

    def test_missing_aux_column_rejected(self, make_args, tmp_csv):
        path = tmp_csv(header=["product_id"], rows=[[1]])
        args = make_args(csv=str(path))
        pairs, reason = extract_id_aux_pairs_for_preflight(args, {}, ["fhs"])
        assert pairs is None
        assert "missing required columns" in reason
        assert "fhs" in reason

    def test_missing_id_column_rejected(self, make_args, tmp_csv):
        path = tmp_csv(header=["fhs"], rows=[[42.5]])
        args = make_args(csv=str(path))
        pairs, reason = extract_id_aux_pairs_for_preflight(args, {}, ["fhs"])
        assert pairs is None
        assert "product_id" in reason or "id" in reason
